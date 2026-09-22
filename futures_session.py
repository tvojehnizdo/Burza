from __future__ import annotations

import argparse
import json
import time
from typing import Any

from futures_autopilot import (
    LOOP_SEC,
    MAX_CONSECUTIVE_ERRORS,
    MAX_SESSION_DRAWDOWN_PCT,
    SESSION_CAPITAL_USD,
    _acquire_pid_lock,
    _close_position,
    _drawdown_guard,
    _load_state,
    _log,
    _manage_positions,
    _portfolio_snapshot,
    _position_rows_map,
    _release_pid_lock,
    _save_state,
)
from futures_canary import (
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
    MAX_PORTFOLIO_NOTIONAL_USD,
    MAX_NOTIONAL_USD,
    execute,
    execute_candidate,
    plan_specific_candidate,
    private_plan,
    public_scan,
)
from futures_pairs import scan_pairs
from futures_private import save_policy

SESSION_DURATION_SEC = 60 * 60
SESSION_MAX_ENTRIES = 10
REENTRY_COOLDOWN_SEC = 300
GLOBAL_ENTRY_BASE_SEC = 90
GLOBAL_ENTRY_STRONG_SEC = 45
GLOBAL_ENTRY_ELITE_SEC = 20
PAIR_LIVE_ENABLED = False


def _session_policy_disarm() -> None:
    save_policy({"live_execution": False})


def _entry_allowed(now_ms: int, state: dict[str, Any]) -> tuple[bool, str]:
    deadline = int(state.get("session_deadline_ts_ms") or 0)
    entries = int(state.get("session_entry_count") or 0)
    halt_reason = str(state.get("entry_halt_reason") or "")
    if halt_reason:
        return False, halt_reason
    if deadline and now_ms >= deadline:
        return False, "SESSION_TIME_LIMIT_REACHED"
    if entries >= SESSION_MAX_ENTRIES:
        return False, "SESSION_ENTRY_LIMIT_REACHED"
    return True, "ENTRY_WINDOW_OPEN"


PAIR_SCAN_INTERVAL_SEC = 60


def _cooldown_active(state: dict[str, Any], symbol: str, now_ms: int) -> bool:
    cooldowns = state.get("symbol_cooldowns") or {}
    try:
        return now_ms < int(cooldowns.get(str(symbol).upper()) or 0)
    except Exception:
        return False


def _set_symbol_cooldown(state: dict[str, Any], symbol: str, now_ms: int) -> None:
    cooldowns = state.setdefault("symbol_cooldowns", {})
    cooldowns[str(symbol).upper()] = int(now_ms + REENTRY_COOLDOWN_SEC * 1000)


def _global_entry_wait_sec(candidate: dict[str, Any]) -> int:
    tier = str(candidate.get("quality_tier") or "BASE").upper()
    if tier == "ELITE":
        return GLOBAL_ENTRY_ELITE_SEC
    if tier == "STRONG":
        return GLOBAL_ENTRY_STRONG_SEC
    return GLOBAL_ENTRY_BASE_SEC


def _global_entry_ready(state: dict[str, Any], candidate: dict[str, Any], now_ms: int) -> tuple[bool, int]:
    wait_sec = _global_entry_wait_sec(candidate)
    last_ms = int(state.get("last_any_entry_ts_ms") or 0)
    if last_ms <= 0:
        return True, wait_sec
    return now_ms - last_ms >= wait_sec * 1000, wait_sec


def _apply_exit_cooldowns(state: dict[str, Any], exits: list[dict[str, Any]], now_ms: int) -> None:
    for row in exits:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            _set_symbol_cooldown(state, symbol, now_ms)


def _rollback_pair_leg(client: Any, state: dict[str, Any], symbol: str, reason: str) -> dict[str, Any]:
    row = _position_rows_map(client.open_positions()).get(symbol.upper())
    if row is None:
        return {"ok": True, "reason": "PAIR_ROLLBACK_NOT_NEEDED", "symbol": symbol}
    return _close_position(client, state, row, reason, 0.0)


def _try_pair_entry(state: dict[str, Any]) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    if not PAIR_LIVE_ENABLED:
        last_pair_scan = int(state.get("last_pair_scan_ts_ms") or 0)
        if now_ms - last_pair_scan < PAIR_SCAN_INTERVAL_SEC * 1000:
            return {"ok": True, "reason": "PAIR_SHADOW_COOLDOWN"}
        state["last_pair_scan_ts_ms"] = now_ms
        try:
            scan = public_scan()
            symbols = [str(x).upper() for x in scan.get("symbols", []) if x]
            pairs = scan_pairs(symbols) if len(symbols) >= 2 else {"top": []}
            top = (pairs.get("top") or [])[:5]
            _log({
                "event": "PAIR_SHADOW_SCAN",
                "live_execution": False,
                "candidate_count": len(pairs.get("top") or []),
                "top": top,
            })
            return {
                "ok": True,
                "reason": "PAIR_SHADOW_ONLY",
                "candidate_count": len(pairs.get("top") or []),
                "top": top,
            }
        except Exception as exc:
            _log({"event": "PAIR_SHADOW_ERROR", "error": f"{type(exc).__name__}: {exc}"})
            return {"ok": True, "reason": "PAIR_SHADOW_ERROR"}

    if int(state.get("session_entry_count") or 0) > SESSION_MAX_ENTRIES - 2:
        return {"ok": True, "reason": "PAIR_ENTRY_BUDGET_FULL"}

    from futures_private import client_from_env
    client = client_from_env()
    snap = _portfolio_snapshot(client)
    if int(snap.get("open_position_count") or 0) > MAX_OPEN_POSITIONS - 2:
        return {"ok": True, "reason": "PAIR_NEEDS_TWO_FREE_SLOTS"}

    last_pair_scan = int(state.get("last_pair_scan_ts_ms") or 0)
    if now_ms - last_pair_scan < PAIR_SCAN_INTERVAL_SEC * 1000:
        return {"ok": True, "reason": "PAIR_SCAN_COOLDOWN"}
    state["last_pair_scan_ts_ms"] = now_ms

    scan = public_scan()
    symbols = [str(x).upper() for x in scan.get("symbols", []) if x]
    if len(symbols) < 2:
        return {"ok": True, "reason": "PAIR_UNIVERSE_TOO_SMALL"}

    pairs = scan_pairs(symbols)
    for pair in pairs.get("top", []):
        long_symbol = str(pair.get("long_symbol") or "").upper()
        short_symbol = str(pair.get("short_symbol") or "").upper()
        if not long_symbol or not short_symbol or long_symbol == short_symbol:
            continue

        long_plan = plan_specific_candidate(long_symbol, "buy", pair)
        short_plan = plan_specific_candidate(short_symbol, "sell", pair)
        if not long_plan.get("ready") or not short_plan.get("ready"):
            continue

        first = execute_candidate(dict(long_plan["candidate"]))
        if not (first.get("ok") and first.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION"):
            if first.get("actual_order_submitted"):
                state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 1
                state["stats"]["execution_aborts"] = int(state.get("stats", {}).get("execution_aborts", 0)) + 1
                state["entry_halt_reason"] = "LIVE_ABORT_CIRCUIT_BREAKER"
                _log({"event": "PAIR_FIRST_LEG_LIVE_ABORT_HALT", "pair": pair, "first": first})
                return {"ok": False, "reason": "LIVE_ABORT_CIRCUIT_BREAKER", "pair": pair}
            continue

        _manage_positions(client, state)

        # Re-plan the second leg against the account after leg 1 is genuinely open.
        short_plan = plan_specific_candidate(short_symbol, "sell", pair)
        if not short_plan.get("ready"):
            rollback = _rollback_pair_leg(client, state, long_symbol, "PAIR_SECOND_LEG_NOT_READY")
            state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 1
            state["stats"]["auto_entries"] = int(state.get("stats", {}).get("auto_entries", 0)) + 1
            state["entry_halt_reason"] = "PAIR_SECOND_LEG_NOT_READY_CIRCUIT_BREAKER"
            _log({"event": "PAIR_ENTRY_ROLLBACK", "pair": pair, "first": first, "rollback": rollback, "entry_halted": True})
            return {"ok": False, "reason": "PAIR_SECOND_LEG_NOT_READY_CIRCUIT_BREAKER", "pair": pair}

        second = execute_candidate(dict(short_plan["candidate"]))
        if not (second.get("ok") and second.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION"):
            rollback = _rollback_pair_leg(client, state, long_symbol, "PAIR_SECOND_LEG_FAILED")
            actual_entries = 1 + (1 if second.get("actual_order_submitted") else 0)
            state["session_entry_count"] = int(state.get("session_entry_count") or 0) + actual_entries
            state["stats"]["auto_entries"] = int(state.get("stats", {}).get("auto_entries", 0)) + actual_entries
            if second.get("actual_order_submitted"):
                state["stats"]["execution_aborts"] = int(state.get("stats", {}).get("execution_aborts", 0)) + 1
            state["entry_halt_reason"] = "PAIR_EXECUTION_FAILURE_CIRCUIT_BREAKER"
            _log({"event": "PAIR_ENTRY_ROLLBACK", "pair": pair, "first": first, "second": second, "rollback": rollback, "entry_halted": True})
            return {"ok": False, "reason": "PAIR_EXECUTION_FAILURE_CIRCUIT_BREAKER", "pair": pair}

        _manage_positions(client, state)
        state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 2
        state["stats"]["auto_entries"] = int(state.get("stats", {}).get("auto_entries", 0)) + 2
        _log({
            "event": "BOUNDED_SESSION_PAIR_ENTRY",
            "pair": pair,
            "long_result": first,
            "short_result": second,
            "session_entry_count": state["session_entry_count"],
        })
        return {
            "ok": True,
            "reason": "PAIR_AUTO_ENTRY_OPENED",
            "long_symbol": long_symbol,
            "short_symbol": short_symbol,
            "pair_mode": pair.get("mode"),
            "session_entry_count": state["session_entry_count"],
        }

    return {"ok": True, "reason": "NO_EXECUTABLE_PAIR"}


def _try_entry(state: dict[str, Any]) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    allowed, reason = _entry_allowed(now_ms, state)
    if not allowed:
        return {"ok": True, "reason": reason}

    pair_result = _try_pair_entry(state)
    if pair_result.get("reason") == "PAIR_AUTO_ENTRY_OPENED":
        return pair_result
    if state.get("entry_halt_reason"):
        return {
            "ok": False,
            "reason": str(state["entry_halt_reason"]),
            "pair_reason": pair_result.get("reason"),
        }

    plan = private_plan()
    if not plan.get("ready"):
        return {
            "ok": True,
            "reason": str(plan.get("reason") or pair_result.get("reason") or "NO_ENTRY"),
            "candidate": (plan.get("public_scan") or {}).get("candidate"),
            "pair_reason": pair_result.get("reason"),
        }

    choices = []
    if plan.get("candidate"):
        choices.append(plan["candidate"])
    choices.extend(plan.get("alternatives") or [])
    selected = None
    for candidate in choices:
        symbol = str(candidate.get("symbol") or "").upper()
        if symbol and not _cooldown_active(state, symbol, now_ms):
            selected = candidate
            break

    if selected is None:
        return {
            "ok": True,
            "reason": "ALL_SIGNALS_IN_REENTRY_COOLDOWN",
            "pair_reason": pair_result.get("reason"),
        }

    global_ready, global_wait_sec = _global_entry_ready(state, selected, now_ms)
    if not global_ready:
        return {
            "ok": True,
            "reason": "GLOBAL_ENTRY_PACING",
            "quality_tier": selected.get("quality_tier"),
            "quality_score": selected.get("quality_score"),
            "required_wait_sec": global_wait_sec,
            "pair_reason": pair_result.get("reason"),
        }

    result = execute_candidate(dict(selected))
    if result.get("ok") and result.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION":
        state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 1
        state["stats"]["auto_entries"] = int(state.get("stats", {}).get("auto_entries", 0)) + 1
        symbol = str((result.get("candidate") or {}).get("symbol") or "").upper()
        _set_symbol_cooldown(state, symbol, now_ms)
        state["last_any_entry_ts_ms"] = now_ms
        _log({
            "event": "BOUNDED_SESSION_AUTO_ENTRY",
            "symbol": symbol,
            "session_entry_count": state["session_entry_count"],
            "result": result,
            "pair_reason": pair_result.get("reason"),
            "reentry_cooldown_sec": REENTRY_COOLDOWN_SEC,
            "global_entry_wait_sec": _global_entry_wait_sec(selected),
            "quality_tier": selected.get("quality_tier"),
            "quality_score": selected.get("quality_score"),
        })
        return {
            "ok": True,
            "reason": "AUTO_ENTRY_OPENED",
            "symbol": symbol,
            "session_entry_count": state["session_entry_count"],
        }

    if result.get("reason") == "FUTURES_CANARY_ABORTED":
        state["stats"]["execution_aborts"] = int(state.get("stats", {}).get("execution_aborts", 0)) + 1
        if result.get("actual_order_submitted"):
            state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 1
            state["entry_halt_reason"] = "LIVE_ABORT_CIRCUIT_BREAKER"
            _log({"event": "BOUNDED_SESSION_LIVE_ABORT_HALT", "result": result, "pair_reason": pair_result.get("reason")})
            return {"ok": False, "reason": "LIVE_ABORT_CIRCUIT_BREAKER"}

    _log({"event": "BOUNDED_SESSION_ENTRY_NOT_OPENED", "result": result, "pair_reason": pair_result.get("reason")})
    return {"ok": False, "reason": str(result.get("reason") or "ENTRY_FAILED")}


def run_session() -> None:
    state = _load_state()
    _session_policy_disarm()

    from futures_private import client_from_env

    client = client_from_env()
    initial = _portfolio_snapshot(client)
    now_ms = int(time.time() * 1000)

    state["session_start_equity"] = float(initial["equity_usd"])
    state["session_start_ts_ms"] = now_ms
    state["session_deadline_ts_ms"] = now_ms + SESSION_DURATION_SEC * 1000
    state["session_entry_count"] = 0
    state["symbol_cooldowns"] = {}
    state["last_any_entry_ts_ms"] = 0
    state.pop("entry_halt_reason", None)
    state["session_capital_usd"] = min(SESSION_CAPITAL_USD, float(initial["equity_usd"]))
    _save_state(state)

    _log({
        "event": "BOUNDED_SESSION_START",
        "session_start_equity": state["session_start_equity"],
        "session_capital_usd": state["session_capital_usd"],
        "session_duration_sec": SESSION_DURATION_SEC,
        "session_max_entries": SESSION_MAX_ENTRIES,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_trade_notional_usd": MAX_NOTIONAL_USD,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        "max_session_drawdown_pct": MAX_SESSION_DRAWDOWN_PCT,
    })

    print(
        "FUTURES BOUNDED LIVE SESSION: RUNNING | "
        f"{SESSION_DURATION_SEC // 60} min | max {SESSION_MAX_ENTRIES} entries | "
        f"max {MAX_OPEN_POSITIONS} open | max USD {MAX_NOTIONAL_USD:.2f}/trade | "
        f"portfolio max USD {MAX_PORTFOLIO_NOTIONAL_USD:.2f} | "
        f"capital budget USD {state['session_capital_usd']:.2f} | "
        f"kill-switch {MAX_SESSION_DRAWDOWN_PCT:.0f}%"
    )

    errors = 0
    entry_window_closed_reason: str | None = None

    while True:
        try:
            guard = _drawdown_guard(client, state)
            if guard is not None:
                _save_state(state)
                print(
                    "SESSION STOP: 50% allocated-capital loss budget reached; "
                    "flatten attempted."
                )
                return

            exits = _manage_positions(client, state)
            now_ms = int(time.time() * 1000)
            _apply_exit_cooldowns(state, exits, now_ms)
            snap = _portfolio_snapshot(client)

            now_ms = int(time.time() * 1000)
            allowed, reason = _entry_allowed(now_ms, state)

            if allowed:
                entry = _try_entry(state)
            else:
                entry_window_closed_reason = reason
                entry = {"ok": True, "reason": reason}

            snap = _portfolio_snapshot(client)
            _save_state(state)

            deadline = int(state.get("session_deadline_ts_ms") or 0)
            remaining_sec = max(0, (deadline - now_ms) // 1000) if deadline else 0

            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"equity=USD {snap['equity_usd']:.4f} | "
                f"open={snap['open_position_count']}/{MAX_OPEN_POSITIONS} | "
                f"notional=USD {snap['portfolio_notional_usd']:.4f} | "
                f"entries={state.get('session_entry_count', 0)}/{SESSION_MAX_ENTRIES} | "
                f"remaining={remaining_sec}s | entry={entry.get('reason')} | exits={len(exits)}"
            )

            if entry_window_closed_reason and int(snap["open_position_count"]) == 0:
                print(
                    f"SESSION COMPLETE: {entry_window_closed_reason}; "
                    "all positions closed."
                )
                return

            errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            errors += 1
            _log({
                "event": "BOUNDED_SESSION_LOOP_ERROR",
                "error": f"{type(exc).__name__}: {exc}",
                "count": errors,
            })
            print(
                f"SESSION ERROR {errors}/{MAX_CONSECUTIVE_ERRORS}: "
                f"{type(exc).__name__}: {exc}"
            )
            if errors >= MAX_CONSECUTIVE_ERRORS:
                _session_policy_disarm()
                print(
                    "SESSION CIRCUIT BREAKER: new entries stopped. "
                    "Exchange STOP/TP remain active."
                )
                return

        time.sleep(LOOP_SEC)


def status() -> dict[str, Any]:
    state = _load_state()
    return {
        "ok": True,
        "mode": "BOUNDED_LIVE_SESSION",
        "duration_sec": SESSION_DURATION_SEC,
        "max_entries": SESSION_MAX_ENTRIES,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_trade_notional_usd": MAX_NOTIONAL_USD,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        "session_capital_usd": SESSION_CAPITAL_USD,
        "max_session_drawdown_pct": MAX_SESSION_DRAWDOWN_PCT,
        "reentry_cooldown_sec": REENTRY_COOLDOWN_SEC,
        "global_entry_pacing_sec": {
            "BASE": GLOBAL_ENTRY_BASE_SEC,
            "STRONG": GLOBAL_ENTRY_STRONG_SEC,
            "ELITE": GLOBAL_ENTRY_ELITE_SEC,
        },
        "simple_pair_live_enabled": PAIR_LIVE_ENABLED,
        "state": state,
    }


def selftest() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    checks = {
        "duration_60m": SESSION_DURATION_SEC == 3600,
        "max_entries_10": SESSION_MAX_ENTRIES == 10,
        "capital_22": SESSION_CAPITAL_USD == 22.0,
        "drawdown_50": MAX_SESSION_DRAWDOWN_PCT == 50.0,
        "max_open_4": MAX_OPEN_POSITIONS == 4,
        "adaptive_pacing": (
            GLOBAL_ENTRY_ELITE_SEC < GLOBAL_ENTRY_STRONG_SEC < GLOBAL_ENTRY_BASE_SEC
        ),
        "window_open": _entry_allowed(
            now_ms,
            {"session_deadline_ts_ms": now_ms + 60_000, "session_entry_count": 0},
        ) == (True, "ENTRY_WINDOW_OPEN"),
        "time_closed": _entry_allowed(
            now_ms,
            {"session_deadline_ts_ms": now_ms - 1, "session_entry_count": 0},
        ) == (False, "SESSION_TIME_LIMIT_REACHED"),
        "count_closed": _entry_allowed(
            now_ms,
            {"session_deadline_ts_ms": now_ms + 60_000, "session_entry_count": 10},
        ) == (False, "SESSION_ENTRY_LIMIT_REACHED"),
    }
    return {"ok": all(checks.values()), "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args()

    if args.selftest:
        result = selftest()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result["ok"]:
            raise SystemExit(2)
        return

    if args.status:
        print(json.dumps(status(), indent=2, ensure_ascii=False, default=str))
        return

    if args.run:
        if args.confirm != "RUN-BOUNDED-LIVE-SESSION":
            raise SystemExit("Session requires --confirm RUN-BOUNDED-LIVE-SESSION")
        _acquire_pid_lock()
        try:
            run_session()
        finally:
            _session_policy_disarm()
            _release_pid_lock()
        return

    ap.print_help()


if __name__ == "__main__":
    main()

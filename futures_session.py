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
    _drawdown_guard,
    _load_state,
    _log,
    _manage_positions,
    _portfolio_snapshot,
    _release_pid_lock,
    _save_state,
)
from futures_canary import (
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
    MAX_PORTFOLIO_NOTIONAL_USD,
    MAX_NOTIONAL_USD,
    execute,
    private_plan,
)
from futures_private import save_policy

SESSION_DURATION_SEC = 60 * 60
SESSION_MAX_ENTRIES = 10


def _session_policy_disarm() -> None:
    save_policy({"live_execution": False})


def _entry_allowed(now_ms: int, state: dict[str, Any]) -> tuple[bool, str]:
    deadline = int(state.get("session_deadline_ts_ms") or 0)
    entries = int(state.get("session_entry_count") or 0)
    if deadline and now_ms >= deadline:
        return False, "SESSION_TIME_LIMIT_REACHED"
    if entries >= SESSION_MAX_ENTRIES:
        return False, "SESSION_ENTRY_LIMIT_REACHED"
    return True, "ENTRY_WINDOW_OPEN"


def _try_entry(state: dict[str, Any]) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    allowed, reason = _entry_allowed(now_ms, state)
    if not allowed:
        return {"ok": True, "reason": reason}

    plan = private_plan()
    if not plan.get("ready"):
        return {
            "ok": True,
            "reason": str(plan.get("reason") or "NO_ENTRY"),
            "candidate": (plan.get("public_scan") or {}).get("candidate"),
        }

    result = execute()
    if result.get("ok") and result.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION":
        state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 1
        state["stats"]["auto_entries"] = int(state.get("stats", {}).get("auto_entries", 0)) + 1
        symbol = str((result.get("candidate") or {}).get("symbol") or "").upper()
        _log({
            "event": "BOUNDED_SESSION_AUTO_ENTRY",
            "symbol": symbol,
            "session_entry_count": state["session_entry_count"],
            "result": result,
        })
        return {
            "ok": True,
            "reason": "AUTO_ENTRY_OPENED",
            "symbol": symbol,
            "session_entry_count": state["session_entry_count"],
        }

    if result.get("reason") == "FUTURES_CANARY_ABORTED":
        state["stats"]["execution_aborts"] = int(state.get("stats", {}).get("execution_aborts", 0)) + 1

    _log({"event": "BOUNDED_SESSION_ENTRY_NOT_OPENED", "result": result})
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

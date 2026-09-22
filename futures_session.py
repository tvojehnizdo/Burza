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
    ROUND_TRIP_TAKER_COST_BPS,
    LIVE_MIN_QUALITY_SCORE,
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
from futures_shadow_learning import observe_plan as shadow_observe_plan, resolve_due as shadow_resolve_due
from futures_setup_engine import (
    BREAKOUT_BUFFER_PCT,
    CONFIRM_LOOKBACK_MIN,
    MIN_MOVE_FROM_ANCHOR_PCT,
    PROBE_EXPIRY_SEC,
    PROBE_LOOKBACK_MIN,
    breakout_invalidated,
    compatible_direction,
    fixed_range_reversal,
    latest_completed_close,
    new_setup_from_signal,
    snapshot as setup_snapshot,
)

SESSION_DURATION_SEC = 60 * 60
SESSION_MAX_ENTRIES = 2
SETUP_ENGINE_ENABLED = True
REENTRY_COOLDOWN_SEC = 300
GLOBAL_ENTRY_BASE_SEC = 90
GLOBAL_ENTRY_STRONG_SEC = 45
GLOBAL_ENTRY_ELITE_SEC = 20
LOSS_STREAK_PAUSE_AFTER = 2
LOSS_STREAK_HALT_AFTER = 3
LOSS_STREAK_PAUSE_SEC = 600
SESSION_SOFT_LOSS_PCT = 1.5
PAIR_LIVE_ENABLED = False
PAIR_SHADOW_SCAN_DURING_LIVE = False
SETUP_SCAN_CANDIDATES = 10
LIVE_REQUIRE_FRESH_MICRO_CONFIRM = True


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


def _apply_loss_feedback(
    state: dict[str, Any],
    exits: list[dict[str, Any]],
    current_equity: float,
    now_ms: int,
) -> None:
    streak = int(state.get("consecutive_net_losses") or 0)

    for row in exits:
        if bool(row.get("pnl_unknown")):
            _log({
                "event": "SESSION_EXIT_FEEDBACK_SKIPPED",
                "symbol": row.get("symbol"),
                "exit_reason": row.get("exit_reason"),
                "reason": "PNL_UNKNOWN",
            })
            continue
        gross_bps = float(row.get("pnl_bps_before_close") or 0.0)
        approx_net_bps = gross_bps - ROUND_TRIP_TAKER_COST_BPS
        if approx_net_bps <= 0:
            streak += 1
        else:
            streak = 0

        _log({
            "event": "SESSION_EXIT_FEEDBACK",
            "symbol": row.get("symbol"),
            "exit_reason": row.get("exit_reason"),
            "gross_bps_before_close": gross_bps,
            "approx_net_bps_after_modeled_cost": approx_net_bps,
            "consecutive_net_losses": streak,
        })

        if streak >= LOSS_STREAK_HALT_AFTER:
            state["entry_halt_reason"] = "LOSS_STREAK_CIRCUIT_BREAKER"
        elif streak >= LOSS_STREAK_PAUSE_AFTER:
            state["entry_pause_until_ts_ms"] = max(
                int(state.get("entry_pause_until_ts_ms") or 0),
                now_ms + LOSS_STREAK_PAUSE_SEC * 1000,
            )

    state["consecutive_net_losses"] = streak

    start_equity = float(state.get("session_start_equity") or 0.0)
    if start_equity > 0 and current_equity > 0:
        drawdown_pct = max(0.0, (start_equity - current_equity) / start_equity * 100.0)
        state["session_equity_drawdown_pct"] = drawdown_pct
        if drawdown_pct >= SESSION_SOFT_LOSS_PCT:
            state["entry_halt_reason"] = "SESSION_SOFT_LOSS_CIRCUIT_BREAKER"


def _temporary_entry_pause(state: dict[str, Any], now_ms: int) -> tuple[bool, int]:
    until = int(state.get("entry_pause_until_ts_ms") or 0)
    if until > now_ms:
        return True, max(0, (until - now_ms + 999) // 1000)
    return False, 0


def _rollback_pair_leg(client: Any, state: dict[str, Any], symbol: str, reason: str) -> dict[str, Any]:
    row = _position_rows_map(client.open_positions()).get(symbol.upper())
    if row is None:
        return {"ok": True, "reason": "PAIR_ROLLBACK_NOT_NEEDED", "symbol": symbol}
    return _close_position(client, state, row, reason, 0.0)


def _try_pair_entry(state: dict[str, Any]) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    if not PAIR_LIVE_ENABLED and not PAIR_SHADOW_SCAN_DURING_LIVE:
        return {"ok": True, "reason": "PAIR_LIVE_AND_SESSION_SHADOW_DISABLED"}
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


def _candidate_choices(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(plan.get("candidate"), dict):
        rows.append(plan["candidate"])
    rows.extend(x for x in (plan.get("alternatives") or []) if isinstance(x, dict))
    return rows


def _candidate_live_gate(candidate: dict[str, Any]) -> tuple[bool, str]:
    quality = float(candidate.get("quality_score") or 0.0)
    tier = str(candidate.get("quality_tier") or "BASE").upper()
    micro = candidate.get("microstructure") if isinstance(candidate.get("microstructure"), dict) else {}
    source = candidate.get("source_signal") if isinstance(candidate.get("source_signal"), dict) else {}
    if quality < LIVE_MIN_QUALITY_SCORE:
        return False, "QUALITY_BELOW_LIVE_GATE"
    if tier not in {"STRONG", "ELITE"}:
        return False, "QUALITY_TIER_BELOW_STRONG"
    if int(source.get("breadth_alignment") or 0) < 0:
        return False, "MARKET_BREADTH_OPPOSES_SIGNAL"
    if LIVE_REQUIRE_FRESH_MICRO_CONFIRM and not bool(micro.get("microstructure_confirmed")):
        return False, "FRESH_MICROSTRUCTURE_NOT_CONFIRMED"
    return True, "LIVE_GATE_OK"


def _setup_opportunity_score(candidate: dict[str, Any], signal: dict[str, Any]) -> float:
    source = candidate.get("source_signal") if isinstance(candidate.get("source_signal"), dict) else {}
    micro = candidate.get("microstructure") if isinstance(candidate.get("microstructure"), dict) else {}
    quality = float(candidate.get("quality_score") or 0.0)
    distance = min(max(float(signal.get("breakout_distance_bps") or 0.0), 0.0), 30.0)
    volume = min(max(float(source.get("volume_ratio") or 0.0) - 1.0, 0.0), 2.0)
    flow = max(float(micro.get("aligned_flow") or 0.0), 0.0)
    breadth = max(int(source.get("breadth_alignment") or 0), 0)
    return quality + 0.60 * distance + 5.0 * volume + 10.0 * flow + 3.0 * breadth


def _best_confirmed_opportunity(
    plan: dict[str, Any],
    state: dict[str, Any],
    now_ms: int,
) -> tuple[dict[str, Any], dict[str, Any], float] | None:
    best: tuple[dict[str, Any], dict[str, Any], float] | None = None
    for candidate in _candidate_choices(plan)[:SETUP_SCAN_CANDIDATES]:
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol or _cooldown_active(state, symbol, now_ms):
            continue
        gate_ok, _ = _candidate_live_gate(candidate)
        if not gate_ok:
            continue
        try:
            snap = setup_snapshot(symbol)
        except Exception as exc:
            _log({"event": "SETUP_V3_SNAPSHOT_ERROR", "symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            continue
        confirmed = snap.get("confirmed")
        if not confirmed or not compatible_direction(candidate, str(confirmed.get("direction") or "")):
            continue
        score = _setup_opportunity_score(candidate, confirmed)
        if best is None or score > best[2]:
            best = (candidate, confirmed, score)
    return best


def _find_setup_candidate(
    plan: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    state: dict[str, Any],
    now_ms: int,
    ignore_symbol_cooldown: bool = False,
) -> dict[str, Any] | None:
    target = str(symbol).upper()
    expected = str(direction).upper()
    for candidate in _candidate_choices(plan):
        cand_symbol = str(candidate.get("symbol") or "").upper()
        if cand_symbol != target:
            continue
        if not compatible_direction(candidate, expected):
            continue
        gate_ok, _ = _candidate_live_gate(candidate)
        if not gate_ok:
            continue
        if not ignore_symbol_cooldown and _cooldown_active(state, cand_symbol, now_ms):
            continue
        return candidate
    return None


def _setup_live_success(
    state: dict[str, Any],
    setup: dict[str, Any],
    candidate: dict[str, Any],
    result: dict[str, Any],
    *,
    live_stage: str,
    now_ms: int,
) -> dict[str, Any]:
    state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 1
    state["stats"]["auto_entries"] = int(state.get("stats", {}).get("auto_entries", 0)) + 1
    symbol = str((result.get("candidate") or {}).get("symbol") or candidate.get("symbol") or "").upper()
    _set_symbol_cooldown(state, symbol, now_ms)
    state["last_any_entry_ts_ms"] = now_ms

    setup["stage"] = live_stage
    setup["last_live_entry_ts_ms"] = now_ms
    setup["current_live_direction"] = (
        "LONG" if str(candidate.get("side") or "").lower() == "buy" else "SHORT"
    )
    setup["live_quality_score"] = candidate.get("quality_score")
    setup["live_quality_tier"] = candidate.get("quality_tier")
    if live_stage == "FIRST_LIVE":
        setup["first_live_opened"] = True
    elif live_stage == "REVERSAL_LIVE":
        setup["reversal_used"] = True
    state["setup_v2"] = setup

    _log({
        "event": "SETUP_V2_LIVE_ENTRY",
        "symbol": symbol,
        "setup_stage": live_stage,
        "original_direction": setup.get("original_direction"),
        "candidate_direction": (
            (candidate.get("source_signal") or {}).get("base_side")
            if isinstance(candidate.get("source_signal"), dict)
            else None
        ),
        "session_entry_count": state["session_entry_count"],
        "result": result,
        "setup": setup,
    })
    return {
        "ok": True,
        "reason": "SETUP_V2_LIVE_ENTRY_OPENED",
        "symbol": symbol,
        "setup_stage": live_stage,
        "session_entry_count": state["session_entry_count"],
    }


def _setup_live_abort(
    state: dict[str, Any],
    result: dict[str, Any],
    pair_reason: str | None,
) -> dict[str, Any]:
    if result.get("reason") == "FUTURES_CANARY_ABORTED":
        state["stats"]["execution_aborts"] = int(state.get("stats", {}).get("execution_aborts", 0)) + 1
        if result.get("actual_order_submitted"):
            state["session_entry_count"] = int(state.get("session_entry_count") or 0) + 1
            state["entry_halt_reason"] = "LIVE_ABORT_CIRCUIT_BREAKER"
            _log({
                "event": "SETUP_V2_LIVE_ABORT_HALT",
                "result": result,
                "pair_reason": pair_reason,
            })
            return {"ok": False, "reason": "LIVE_ABORT_CIRCUIT_BREAKER"}
    _log({
        "event": "SETUP_V2_ENTRY_NOT_OPENED",
        "result": result,
        "pair_reason": pair_reason,
    })
    return {"ok": False, "reason": str(result.get("reason") or "ENTRY_FAILED")}


def _detect_setup_exchange_close(
    client: Any,
    state: dict[str, Any],
    exits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Advance setup state when exchange STOP/TP closed a leg between manager polls."""
    setup = state.get("setup_v2")
    if not isinstance(setup, dict) or str(setup.get("stage") or "") not in {"FIRST_LIVE", "REVERSAL_LIVE"}:
        return []
    symbol = str(setup.get("symbol") or "").upper()
    if not symbol:
        return []
    if any(str(x.get("symbol") or "").upper() == symbol for x in exits):
        return []
    if symbol in _position_rows_map(client.open_positions()):
        return []

    result = {
        "ok": True,
        "reason": "EXCHANGE_POSITION_GONE",
        "symbol": symbol,
        "exit_reason": "EXCHANGE_PROTECTION_OR_EXTERNAL",
        "pnl_bps_before_close": None,
        "pnl_unknown": True,
    }
    _log({"event": "SETUP_V3_EXCHANGE_CLOSE_DETECTED", **result, "setup": setup})
    return [result]


def _setup_invalidation_exit(client: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    setup = state.get("setup_v2")
    if not isinstance(setup, dict) or str(setup.get("stage") or "") not in {"FIRST_LIVE", "REVERSAL_LIVE"}:
        return []
    symbol = str(setup.get("symbol") or "").upper()
    direction = str(setup.get("current_live_direction") or setup.get("original_direction") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return []

    meta = (state.get("positions") or {}).get(symbol) or {}
    opened_ms = int(meta.get("opened_ts_ms") or setup.get("last_live_entry_ts_ms") or 0)
    if opened_ms and int(time.time() * 1000) - opened_ms < 60_000:
        return []

    try:
        close = latest_completed_close(symbol)
    except Exception:
        return []
    if not breakout_invalidated(
        direction,
        float(setup.get("range_high") or 0.0),
        float(setup.get("range_low") or 0.0),
        close,
    ):
        return []

    row = _position_rows_map(client.open_positions()).get(symbol)
    if row is None:
        return []
    pnl_bps = float(meta.get("last_pnl_bps") or 0.0)
    result = _close_position(client, state, row, "SETUP_INVALIDATION", pnl_bps)
    _log({
        "event": "SETUP_V3_INVALIDATION_EXIT",
        "symbol": symbol,
        "direction": direction,
        "completed_close": close,
        "range_high": setup.get("range_high"),
        "range_low": setup.get("range_low"),
        "result": result,
    })
    return [result]


def _apply_setup_exit_state(
    state: dict[str, Any],
    exits: list[dict[str, Any]],
    now_ms: int,
) -> None:
    setup = state.get("setup_v2")
    if not isinstance(setup, dict) or not setup:
        return
    symbol = str(setup.get("symbol") or "").upper()
    if not symbol:
        return

    for row in exits:
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        stage = str(setup.get("stage") or "")
        if not bool(row.get("pnl_unknown")):
            gross_bps = float(row.get("pnl_bps_before_close") or 0.0)
            _log({
                "event": "SETUP_V3_TRADE_RESULT",
                "symbol": symbol,
                "setup_stage": stage,
                "exit_reason": row.get("exit_reason"),
                "gross_bps": gross_bps,
                "approx_net_bps": gross_bps - ROUND_TRIP_TAKER_COST_BPS,
                "quality_tier": setup.get("live_quality_tier"),
                "quality_score": setup.get("live_quality_score"),
                "setup_opportunity_score": setup.get("setup_opportunity_score"),
            })
        if stage == "FIRST_LIVE":
            setup["stage"] = "WAIT_REVERSAL"
            setup["first_live_closed"] = True
            setup["first_live_exit_ts_ms"] = now_ms
            setup["first_live_exit_reason"] = row.get("exit_reason")
            _log({
                "event": "SETUP_V2_WAIT_REVERSAL",
                "symbol": symbol,
                "setup": setup,
                "exit": row,
            })
        elif stage == "REVERSAL_LIVE":
            setup["stage"] = "DONE"
            setup["done"] = True
            setup["done_ts_ms"] = now_ms
            setup["reversal_exit_reason"] = row.get("exit_reason")
            _log({
                "event": "SETUP_V2_DONE",
                "symbol": symbol,
                "setup": setup,
                "exit": row,
            })
        state["setup_v2"] = setup


def _try_setup_v2_entry(
    state: dict[str, Any],
    plan: dict[str, Any],
    now_ms: int,
    pair_reason: str | None,
) -> dict[str, Any]:
    setup = state.get("setup_v2")
    if not isinstance(setup, dict):
        setup = {}

    stage = str(setup.get("stage") or "")
    if stage in {"FIRST_LIVE", "REVERSAL_LIVE"}:
        return {"ok": True, "reason": f"SETUP_V2_{stage}_MANAGED"}

    if stage == "DONE":
        return {"ok": True, "reason": "SETUP_V2_DONE"}

    if stage == "PROBE_SHADOW":
        if now_ms >= int(setup.get("expires_ts_ms") or 0):
            _log({"event": "SETUP_V2_PROBE_EXPIRED", "setup": setup})
            state["setup_v2"] = {}
            setup = {}
            stage = ""
        else:
            symbol = str(setup.get("symbol") or "").upper()
            snap = setup_snapshot(symbol)
            confirmed = snap.get("confirmed")
            if not confirmed or str(confirmed.get("direction") or "").upper() != str(setup.get("original_direction") or "").upper():
                return {
                    "ok": True,
                    "reason": "SETUP_V2_PROBE_WAIT_CONFIRMATION",
                    "symbol": symbol,
                    "setup": setup,
                }

            direction = str(confirmed["direction"]).upper()
            candidate = _find_setup_candidate(
                plan,
                symbol=symbol,
                direction=direction,
                state=state,
                now_ms=now_ms,
            )
            if candidate is None:
                return {
                    "ok": True,
                    "reason": "SETUP_V2_CONFIRMED_BUT_QUALITY_NOT_READY",
                    "symbol": symbol,
                    "direction": direction,
                }

            setup.update({
                "stage": "CONFIRMED_READY",
                "range_high": float(confirmed["range_high"]),
                "range_low": float(confirmed["range_low"]),
                "anchor_open": float(confirmed["anchor_open"]),
                "signal_close": float(confirmed["close"]),
                "confirmed_ts_ms": now_ms,
            })
            result = execute_candidate(dict(candidate))
            if result.get("ok") and result.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION":
                return _setup_live_success(
                    state,
                    setup,
                    candidate,
                    result,
                    live_stage="FIRST_LIVE",
                    now_ms=now_ms,
                )
            return _setup_live_abort(state, result, pair_reason)

    if stage == "WAIT_REVERSAL":
        if bool(setup.get("reversal_used")):
            setup["stage"] = "DONE"
            setup["done"] = True
            state["setup_v2"] = setup
            return {"ok": True, "reason": "SETUP_V2_DONE"}

        symbol = str(setup.get("symbol") or "").upper()
        close = latest_completed_close(symbol)
        rev = fixed_range_reversal(
            symbol,
            str(setup.get("original_direction") or ""),
            float(setup.get("range_high") or 0.0),
            float(setup.get("range_low") or 0.0),
            close,
        )
        if not rev:
            return {
                "ok": True,
                "reason": "SETUP_V2_WAIT_REVERSAL_BREAK",
                "symbol": symbol,
            }

        direction = str(rev["direction"]).upper()
        candidate = _find_setup_candidate(
            plan,
            symbol=symbol,
            direction=direction,
            state=state,
            now_ms=now_ms,
            ignore_symbol_cooldown=True,
        )
        if candidate is None:
            return {
                "ok": True,
                "reason": "SETUP_V2_REVERSAL_BREAK_BUT_QUALITY_NOT_READY",
                "symbol": symbol,
                "direction": direction,
                "reversal": rev,
            }

        result = execute_candidate(dict(candidate))
        if result.get("ok") and result.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION":
            setup["reversal_signal"] = rev
            return _setup_live_success(
                state,
                setup,
                candidate,
                result,
                live_stage="REVERSAL_LIVE",
                now_ms=now_ms,
            )
        return _setup_live_abort(state, result, pair_reason)

    if not plan.get("ready"):
        return {
            "ok": True,
            "reason": str(plan.get("reason") or pair_reason or "NO_ENTRY"),
            "candidate": (plan.get("public_scan") or {}).get("candidate"),
            "pair_reason": pair_reason,
        }

    confirmed_best = _best_confirmed_opportunity(plan, state, now_ms)
    if confirmed_best is not None:
        candidate, confirmed, setup_score = confirmed_best
        setup = new_setup_from_signal(confirmed, now_ms)
        setup["stage"] = "CONFIRMED_READY"
        setup["setup_opportunity_score"] = round(setup_score, 4)
        state["setup_v2"] = setup
        result = execute_candidate(dict(candidate))
        if result.get("ok") and result.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION":
            return _setup_live_success(
                state,
                setup,
                candidate,
                result,
                live_stage="FIRST_LIVE",
                now_ms=now_ms,
            )
        return _setup_live_abort(state, result, pair_reason)

    first_probe: tuple[dict[str, Any], dict[str, Any], float] | None = None
    for candidate in _candidate_choices(plan)[:SETUP_SCAN_CANDIDATES]:
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol or _cooldown_active(state, symbol, now_ms):
            continue
        try:
            snap = setup_snapshot(symbol)
        except Exception:
            continue
        probe = snap.get("probe")
        if probe and compatible_direction(candidate, str(probe.get("direction") or "")):
            score = _setup_opportunity_score(candidate, probe)
            if first_probe is None or score > first_probe[2]:
                first_probe = (candidate, probe, score)

    if first_probe is not None:
        candidate, probe, probe_score = first_probe
        setup = new_setup_from_signal(probe, now_ms)
        setup["quality_tier_at_probe"] = candidate.get("quality_tier")
        setup["quality_score_at_probe"] = candidate.get("quality_score")
        setup["setup_opportunity_score"] = round(probe_score, 4)
        state["setup_v2"] = setup
        _log({
            "event": "SETUP_V2_PROBE_SHADOW",
            "symbol": setup["symbol"],
            "direction": setup["original_direction"],
            "setup": setup,
            "candidate": candidate,
        })
        return {
            "ok": True,
            "reason": "SETUP_V2_PROBE_SHADOW_STARTED",
            "symbol": setup["symbol"],
            "direction": setup["original_direction"],
            "expires_sec": PROBE_EXPIRY_SEC,
        }

    return {
        "ok": True,
        "reason": "SETUP_V2_WAITING_FOR_RANGE_BREAKOUT",
        "pair_reason": pair_reason,
    }


def _try_entry(state: dict[str, Any]) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    allowed, reason = _entry_allowed(now_ms, state)
    if not allowed:
        return {"ok": True, "reason": reason}

    paused, pause_remaining = _temporary_entry_pause(state, now_ms)
    if paused:
        return {
            "ok": True,
            "reason": "LOSS_STREAK_PAUSE",
            "pause_remaining_sec": pause_remaining,
            "consecutive_net_losses": int(state.get("consecutive_net_losses") or 0),
        }

    pair_result = _try_pair_entry(state)
    if state.get("entry_halt_reason"):
        return {
            "ok": False,
            "reason": str(state["entry_halt_reason"]),
            "pair_reason": pair_result.get("reason"),
        }

    plan = private_plan()
    try:
        shadow_observe_plan(plan)
    except Exception as exc:
        _log({"event": "SHADOW_OBSERVE_ERROR", "error": f"{type(exc).__name__}: {exc}"})

    if SETUP_ENGINE_ENABLED:
        return _try_setup_v2_entry(
            state,
            plan,
            now_ms,
            str(pair_result.get("reason") or ""),
        )

    return {
        "ok": True,
        "reason": "SETUP_ENGINE_DISABLED",
        "pair_reason": pair_result.get("reason"),
    }


def run_session() -> None:
    state = _load_state()
    _session_policy_disarm()

    from futures_private import client_from_env

    client = client_from_env()
    initial = _portfolio_snapshot(client)
    now_ms = int(time.time() * 1000)

    state["strategy_version"] = "SETUP_V3_PROFIT_SCALE"
    state["session_start_equity"] = float(initial["equity_usd"])
    state["session_start_ts_ms"] = now_ms
    state["session_deadline_ts_ms"] = now_ms + SESSION_DURATION_SEC * 1000
    state["session_entry_count"] = 0
    state["setup_v2"] = {}
    state["symbol_cooldowns"] = {}
    state["last_any_entry_ts_ms"] = 0
    state["consecutive_net_losses"] = 0
    state["entry_pause_until_ts_ms"] = 0
    state["session_equity_drawdown_pct"] = 0.0
    state.pop("entry_halt_reason", None)
    state["session_capital_usd"] = min(SESSION_CAPITAL_USD, float(initial["equity_usd"]))
    _save_state(state)

    _log({
        "event": "BOUNDED_SESSION_START",
        "strategy_version": state["strategy_version"],
        "session_start_equity": state["session_start_equity"],
        "session_capital_usd": state["session_capital_usd"],
        "session_duration_sec": SESSION_DURATION_SEC,
        "session_max_entries": SESSION_MAX_ENTRIES,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_trade_notional_usd": MAX_NOTIONAL_USD,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        "max_session_drawdown_pct": MAX_SESSION_DRAWDOWN_PCT,
        "setup_engine_enabled": SETUP_ENGINE_ENABLED,
        "setup_probe_lookback_min": PROBE_LOOKBACK_MIN,
        "setup_confirm_lookback_min": CONFIRM_LOOKBACK_MIN,
        "setup_breakout_buffer_pct": BREAKOUT_BUFFER_PCT,
        "setup_min_move_pct": MIN_MOVE_FROM_ANCHOR_PCT,
        "setup_probe_expiry_sec": PROBE_EXPIRY_SEC,
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
            exits.extend(_detect_setup_exchange_close(client, state, exits))
            exits.extend(_setup_invalidation_exit(client, state))
            now_ms = int(time.time() * 1000)
            _apply_exit_cooldowns(state, exits, now_ms)
            _apply_setup_exit_state(state, exits, now_ms)
            snap = _portfolio_snapshot(client)
            _apply_loss_feedback(
                state,
                exits,
                float(snap["equity_usd"]),
                now_ms,
            )

            try:
                shadow = shadow_resolve_due()
                if int(shadow.get("resolved") or 0) > 0:
                    _log({
                        "event": "SHADOW_LEARNING_UPDATE",
                        "resolved": shadow.get("resolved"),
                    })
            except Exception as exc:
                _log({"event": "SHADOW_RESOLVE_ERROR", "error": f"{type(exc).__name__}: {exc}"})

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
                f"remaining={remaining_sec}s | "
                f"setup={(state.get('setup_v2') or {}).get('stage','IDLE')} | "
                f"entry={entry.get('reason')} | exits={len(exits)}"
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
        "loss_brake": {
            "pause_after_losses": LOSS_STREAK_PAUSE_AFTER,
            "halt_after_losses": LOSS_STREAK_HALT_AFTER,
            "pause_sec": LOSS_STREAK_PAUSE_SEC,
            "soft_session_loss_pct": SESSION_SOFT_LOSS_PCT,
        },
        "setup_v2": {
            "enabled": SETUP_ENGINE_ENABLED,
            "probe_lookback_min": PROBE_LOOKBACK_MIN,
            "confirm_lookback_min": CONFIRM_LOOKBACK_MIN,
            "breakout_buffer_pct": BREAKOUT_BUFFER_PCT,
            "min_move_from_anchor_pct": MIN_MOVE_FROM_ANCHOR_PCT,
            "probe_expiry_sec": PROBE_EXPIRY_SEC,
            "scan_candidates": SETUP_SCAN_CANDIDATES,
            "live_min_quality_score": LIVE_MIN_QUALITY_SCORE,
            "require_fresh_micro_confirm": LIVE_REQUIRE_FRESH_MICRO_CONFIRM,
            "max_actual_entries": SESSION_MAX_ENTRIES,
        },
        "simple_pair_live_enabled": PAIR_LIVE_ENABLED,
        "state": state,
    }


def selftest() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    checks = {
        "duration_60m": SESSION_DURATION_SEC == 3600,
        "max_entries_2": SESSION_MAX_ENTRIES == 2,
        "setup_engine_enabled": SETUP_ENGINE_ENABLED,
        "capital_22": SESSION_CAPITAL_USD == 22.0,
        "drawdown_5": MAX_SESSION_DRAWDOWN_PCT == 5.0,
        "max_open_4": MAX_OPEN_POSITIONS == 4,
        "adaptive_pacing": (
            GLOBAL_ENTRY_ELITE_SEC < GLOBAL_ENTRY_STRONG_SEC < GLOBAL_ENTRY_BASE_SEC
        ),
        "loss_brake_ordered": (
            0 < LOSS_STREAK_PAUSE_AFTER < LOSS_STREAK_HALT_AFTER
            and LOSS_STREAK_PAUSE_SEC > 0
            and SESSION_SOFT_LOSS_PCT > 0
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
            {"session_deadline_ts_ms": now_ms + 60_000, "session_entry_count": 2},
        ) == (False, "SESSION_ENTRY_LIMIT_REACHED"),
        "setup_probe_shadow_only": PROBE_LOOKBACK_MIN == 3,
        "setup_confirm_15m": CONFIRM_LOOKBACK_MIN == 15,
        "setup_scan_candidates_positive": SETUP_SCAN_CANDIDATES >= 6,
        "live_quality_gate": LIVE_MIN_QUALITY_SCORE >= 68.0,
        "pair_shadow_off_during_live": not PAIR_SHADOW_SCAN_DURING_LIVE,
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

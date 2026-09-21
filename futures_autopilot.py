from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from futures_canary import (
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
    MAX_PORTFOLIO_NOTIONAL_USD,
    MAX_NOTIONAL_PCT_EQUITY,
    MAX_NOTIONAL_USD,
    _exit_limit_price,
)
from futures_private import (
    cancel_symbol_orders,
    client_from_env,
    contract_size,
    min_lot,
    open_order_rows,
    place_order,
    readiness,
    round_price_to_tick,
    round_size_down,
    save_policy,
)

STATE_PATH = Path("data/futures_autopilot_state.json")
LOG_PATH = Path("data/futures_autopilot_events.jsonl")
PID_PATH = Path("data/futures_autopilot.pid")

LOOP_SEC = 5
MIN_PROFIT_HOLD_SEC = 20
SMALL_PROFIT_AFTER_SEC = 90
NO_PROGRESS_SEC = 180
HARD_MAX_HOLD_SEC = 480

QUICK_PROFIT_GROSS_BPS = 45.0
SMALL_PROFIT_GROSS_BPS = 35.0
NO_PROGRESS_MAX_FAVORABLE_BPS = 45.0
NO_PROGRESS_CURRENT_BPS = 22.0

ADOPT_STOP_BPS = 45.0
ADOPT_TAKE_BPS = 45.0
MAX_SESSION_DRAWDOWN_PCT = 50.0
SESSION_CAPITAL_USD = 22.0
MAX_CONSECUTIVE_ERRORS = 5


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(event: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts_ms": _now_ms(), **event}
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _load_state() -> dict[str, Any]:
    default = {
        "version": 1,
        "created_ts_ms": _now_ms(),
        "session_start_equity": None,
        "positions": {},
        "stats": {
            "auto_entries": 0,
            "auto_exits": 0,
            "quick_profit_exits": 0,
            "small_profit_exits": 0,
            "no_progress_exits": 0,
            "hard_time_exits": 0,
            "protection_rescues": 0,
            "execution_aborts": 0,
        },
    }
    if not STATE_PATH.exists():
        return default
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            default.update(data)
            if not isinstance(default.get("positions"), dict):
                default["positions"] = {}
            if not isinstance(default.get("stats"), dict):
                default["stats"] = {}
    except Exception:
        pass
    return default


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _position_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("openPositions") or []
    return [x for x in rows if isinstance(x, dict)]


def _position_rows_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in _position_rows(payload)
        if row.get("symbol")
    }


def _ticker_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("tickers") or []
    return {
        str(row.get("symbol") or "").upper(): row
        for row in rows
        if isinstance(row, dict) and row.get("symbol")
    }


def _mid(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    try:
        bid = float(row.get("bid"))
        ask = float(row.get("ask"))
        if bid > 0 and ask >= bid:
            return (bid + ask) / 2.0
    except Exception:
        pass
    for key in ("markPrice", "last", "indexPrice"):
        try:
            value = float(row.get(key))
            if value > 0:
                return value
        except Exception:
            continue
    return None


def _signed_size(row: dict[str, Any]) -> float:
    size = abs(float(row.get("size") or 0.0))
    return -size if str(row.get("side") or "").lower() == "short" else size


def _entry_price(row: dict[str, Any], fallback: float) -> float:
    try:
        px = float(row.get("price") or 0.0)
        if px > 0:
            return px
    except Exception:
        pass
    return fallback


def _pnl_bps(row: dict[str, Any], mid: float) -> float:
    entry = _entry_price(row, mid)
    if entry <= 0:
        return 0.0
    direction = -1.0 if str(row.get("side") or "").lower() == "short" else 1.0
    return direction * (mid / entry - 1.0) * 10000.0


def _parse_iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        return None


def _order_symbol_rows(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    target = str(symbol).upper()
    return [
        row for row in open_order_rows(payload)
        if str(row.get("symbol") or row.get("tradeable") or "").upper() == target
    ]


def _policy_patch(live: bool) -> dict[str, Any]:
    return {
        "live_execution": bool(live),
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        "allowed_roots": ["*"],
    }


def _protect_position(client: Any, row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    signed = _signed_size(row)
    size = round_size_down(symbol, abs(signed))
    if size < min_lot(symbol):
        return {"ok": False, "reason": "POSITION_BELOW_MIN_LOT", "symbol": symbol}

    mid = _mid(_ticker_map(client.tickers()).get(symbol))
    if mid is None:
        return {"ok": False, "reason": "NO_TICKER", "symbol": symbol}

    cancelled = cancel_symbol_orders(client, symbol, reduce_only_only=True)
    exit_side = "sell" if signed > 0 else "buy"
    stop_frac = ADOPT_STOP_BPS / 10000.0
    take_frac = ADOPT_TAKE_BPS / 10000.0

    if signed > 0:
        stop_price = round_price_to_tick(symbol, mid * (1.0 - stop_frac), mode="down")
        take_price = round_price_to_tick(symbol, mid * (1.0 + take_frac), mode="up")
    else:
        stop_price = round_price_to_tick(symbol, mid * (1.0 + stop_frac), mode="up")
        take_price = round_price_to_tick(symbol, mid * (1.0 - take_frac), mode="down")

    save_policy(_policy_patch(True))
    try:
        stop = place_order(
            symbol, exit_side, size, reduce_only=True, order_type="stp",
            stop_price=stop_price,
            limit_price=_exit_limit_price(symbol, exit_side, stop_price),
            trigger_signal="mark",
            cli_ord_id=f"as{_now_ms()}",
        )
        take = place_order(
            symbol, exit_side, size, reduce_only=True, order_type="take_profit",
            stop_price=take_price,
            limit_price=_exit_limit_price(symbol, exit_side, take_price),
            trigger_signal="mark",
            cli_ord_id=f"at{_now_ms()}",
        )
    finally:
        save_policy(_policy_patch(False))

    ok = bool(stop.get("submitted_live")) and bool(take.get("submitted_live"))
    if not ok:
        cancel_symbol_orders(client, symbol, reduce_only_only=True)
        save_policy(_policy_patch(True))
        try:
            flat = place_order(
                symbol, exit_side, size, reduce_only=True, order_type="mkt",
                cli_ord_id=f"af{_now_ms()}",
            )
        finally:
            save_policy(_policy_patch(False))
        return {
            "ok": False,
            "reason": "PROTECTION_FAILED_FLATTEN_ATTEMPTED",
            "symbol": symbol,
            "cancelled": cancelled,
            "stop": stop,
            "take": take,
            "flatten": flat,
        }

    return {
        "ok": True,
        "reason": "PROTECTED",
        "symbol": symbol,
        "stop_price": stop_price,
        "take_price": take_price,
        "cancelled_previous": cancelled,
    }


def _adopt_positions(client: Any, state: dict[str, Any], positions_payload: dict[str, Any], orders_payload: dict[str, Any]) -> None:
    rows = _position_rows_map(positions_payload)
    now = _now_ms()

    for symbol in list(state["positions"].keys()):
        if symbol not in rows:
            cleanup = cancel_symbol_orders(client, symbol, reduce_only_only=True)
            _log({"event": "POSITION_GONE_CLEANUP", "symbol": symbol, "cleanup": cleanup})
            state["positions"].pop(symbol, None)

    active_symbols = set(rows)
    stale_seen: set[str] = set()
    for order in open_order_rows(orders_payload):
        symbol = str(order.get("symbol") or order.get("tradeable") or "").upper()
        if (
            symbol
            and symbol not in active_symbols
            and symbol not in stale_seen
            and bool(order.get("reduceOnly", False))
        ):
            stale_seen.add(symbol)
            cleanup = cancel_symbol_orders(client, symbol, reduce_only_only=True)
            _log({"event": "STALE_ORDER_CLEANUP", "symbol": symbol, "cleanup": cleanup})

    tickers = _ticker_map(client.tickers())
    for symbol, row in rows.items():
        if symbol in state["positions"]:
            continue

        mid = _mid(tickers.get(symbol)) or float(row.get("price") or 0.0)
        opened_ms = _parse_iso_ms(row.get("fillTime")) or now
        entry = _entry_price(row, mid)

        protection_rows = [
            o for o in _order_symbol_rows(orders_payload, symbol)
            if bool(o.get("reduceOnly", False))
        ]
        protect_result = None
        if len(protection_rows) < 2:
            protect_result = _protect_position(client, row)
            if not protect_result.get("ok"):
                _log({"event": "ADOPT_PROTECTION_FAILED", "symbol": symbol, "result": protect_result})
                continue
            state["stats"]["protection_rescues"] = int(state["stats"].get("protection_rescues", 0)) + 1

        state["positions"][symbol] = {
            "opened_ts_ms": opened_ms,
            "entry_price": entry,
            "side": str(row.get("side") or "").lower(),
            "size": abs(float(row.get("size") or 0.0)),
            "max_favorable_bps": 0.0,
            "adopted": True,
            "last_pnl_bps": 0.0,
        }
        _log({
            "event": "POSITION_ADOPTED",
            "symbol": symbol,
            "entry_price": entry,
            "side": state["positions"][symbol]["side"],
            "protection_refreshed": bool(protect_result),
        })


def _close_position(client: Any, state: dict[str, Any], row: dict[str, Any], reason: str, pnl_bps: float) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    signed = _signed_size(row)
    exit_side = "sell" if signed > 0 else "buy"

    cancelled_before = cancel_symbol_orders(client, symbol, reduce_only_only=True)
    latest = _position_rows_map(client.open_positions()).get(symbol)
    if latest is None:
        state["positions"].pop(symbol, None)
        result = {
            "ok": True,
            "reason": "ALREADY_CLOSED_ON_EXCHANGE",
            "symbol": symbol,
            "exit_reason": reason,
            "pnl_bps_before_close": pnl_bps,
            "cancelled_before": cancelled_before,
        }
        _log({"event": "AUTO_EXIT", **result})
        return result

    signed = _signed_size(latest)
    size = round_size_down(symbol, abs(signed))
    exit_side = "sell" if signed > 0 else "buy"

    save_policy(_policy_patch(True))
    try:
        close = place_order(
            symbol, exit_side, size, reduce_only=True, order_type="mkt",
            cli_ord_id=f"ax{_now_ms()}",
        )
    finally:
        save_policy(_policy_patch(False))

    gone = False
    for _ in range(20):
        time.sleep(0.25)
        if symbol not in _position_rows_map(client.open_positions()):
            gone = True
            break

    cleanup = cancel_symbol_orders(client, symbol, reduce_only_only=True)
    if gone:
        state["positions"].pop(symbol, None)

    result = {
        "ok": bool(close.get("submitted_live")) and gone,
        "reason": "AUTO_EXIT_COMPLETE" if gone else "AUTO_EXIT_NOT_CONFIRMED",
        "symbol": symbol,
        "exit_reason": reason,
        "pnl_bps_before_close": pnl_bps,
        "close": close,
        "cancelled_before": cancelled_before,
        "cleanup": cleanup,
    }
    _log({"event": "AUTO_EXIT", **result})

    if gone:
        state["stats"]["auto_exits"] = int(state["stats"].get("auto_exits", 0)) + 1
        key = {
            "QUICK_PROFIT": "quick_profit_exits",
            "SMALL_PROFIT": "small_profit_exits",
            "NO_PROGRESS": "no_progress_exits",
            "HARD_MAX_HOLD": "hard_time_exits",
        }.get(reason)
        if key:
            state["stats"][key] = int(state["stats"].get(key, 0)) + 1
    return result


def _exit_reason(age_sec: float, pnl_bps: float, max_fav_bps: float) -> str | None:
    if age_sec >= HARD_MAX_HOLD_SEC:
        return "HARD_MAX_HOLD"
    if (
        age_sec >= NO_PROGRESS_SEC
        and pnl_bps < NO_PROGRESS_CURRENT_BPS
        and max_fav_bps < NO_PROGRESS_MAX_FAVORABLE_BPS
    ):
        return "NO_PROGRESS"
    if age_sec >= SMALL_PROFIT_AFTER_SEC and pnl_bps >= SMALL_PROFIT_GROSS_BPS:
        return "SMALL_PROFIT"
    if age_sec >= MIN_PROFIT_HOLD_SEC and pnl_bps >= QUICK_PROFIT_GROSS_BPS:
        return "QUICK_PROFIT"
    return None


def _flatten_all_positions(client: Any, state: dict[str, Any], reason: str) -> dict[str, Any]:
    rows = _position_rows_map(client.open_positions())
    results: list[dict[str, Any]] = []

    for symbol, row in rows.items():
        signed = _signed_size(row)
        size = round_size_down(symbol, abs(signed))
        exit_side = "sell" if signed > 0 else "buy"

        cancelled = cancel_symbol_orders(client, symbol, reduce_only_only=True)

        save_policy(_policy_patch(True))
        try:
            close = place_order(
                symbol,
                exit_side,
                size,
                reduce_only=True,
                order_type="mkt",
                cli_ord_id=f"kb{_now_ms()}",
            )
        finally:
            save_policy(_policy_patch(False))

        gone = False
        for _ in range(20):
            time.sleep(0.25)
            if symbol not in _position_rows_map(client.open_positions()):
                gone = True
                break

        cleanup = cancel_symbol_orders(client, symbol, reduce_only_only=True)
        if gone:
            state["positions"].pop(symbol, None)

        results.append({
            "symbol": symbol,
            "closed": gone,
            "close": close,
            "cancelled_before": cancelled,
            "cleanup": cleanup,
        })

    result = {
        "ok": all(bool(x.get("closed")) for x in results) if results else True,
        "reason": reason,
        "positions": results,
    }
    _log({"event": "ACCOUNT_KILL_SWITCH", **result})
    return result


def _drawdown_guard(client: Any, state: dict[str, Any]) -> dict[str, Any] | None:
    snap = _portfolio_snapshot(client)
    equity = float(snap["equity_usd"])
    start = state.get("session_start_equity")

    if start is None or float(start) <= 0:
        state["session_start_equity"] = equity
        return None

    allocated = min(SESSION_CAPITAL_USD, float(start))
    max_loss_usd = allocated * MAX_SESSION_DRAWDOWN_PCT / 100.0
    threshold = float(start) - max_loss_usd
    if equity > threshold:
        return None

    result = _flatten_all_positions(client, state, "MAX_SESSION_DRAWDOWN")
    result.update({
        "equity_usd": equity,
        "session_start_equity": float(start),
        "session_capital_usd": allocated,
        "max_loss_usd": max_loss_usd,
        "drawdown_pct_limit": MAX_SESSION_DRAWDOWN_PCT,
        "threshold_equity_usd": threshold,
    })
    return result


def _manage_positions(client: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    positions_payload = client.open_positions()
    orders_payload = client.open_orders()
    _adopt_positions(client, state, positions_payload, orders_payload)

    rows = _position_rows_map(client.open_positions())
    tickers = _ticker_map(client.tickers())
    actions: list[dict[str, Any]] = []
    now = _now_ms()

    for symbol, row in list(rows.items()):
        meta = state["positions"].get(symbol)
        if not meta:
            continue
        mid = _mid(tickers.get(symbol))
        if mid is None:
            continue

        pnl_bps = _pnl_bps(row, mid)
        age_sec = max(0.0, (now - int(meta.get("opened_ts_ms") or now)) / 1000.0)
        max_fav = max(float(meta.get("max_favorable_bps") or 0.0), pnl_bps)
        meta["max_favorable_bps"] = max_fav
        meta["last_pnl_bps"] = pnl_bps
        meta["last_mid"] = mid
        meta["last_age_sec"] = age_sec

        reason = _exit_reason(age_sec, pnl_bps, max_fav)
        if reason:
            actions.append(_close_position(client, state, row, reason, pnl_bps))

    return actions


def _portfolio_snapshot(client: Any) -> dict[str, Any]:
    r = readiness()
    rows = _position_rows_map(client.open_positions())
    tickers = _ticker_map(client.tickers())
    notional = 0.0
    for symbol, row in rows.items():
        mid = _mid(tickers.get(symbol))
        if mid is None:
            continue
        try:
            notional += abs(float(row.get("size") or 0.0)) * mid * contract_size(symbol)
        except Exception:
            continue
    return {
        "equity_usd": float(r.get("equity_usd") or 0.0),
        "open_position_count": len(rows),
        "portfolio_notional_usd": notional,
        "positions": rows,
        "readiness": r,
    }


def run_forever() -> None:
    state = _load_state()
    client = client_from_env()
    save_policy(_policy_patch(False))

    # The user's 50% max-loss budget starts fresh when the manager is armed.
    initial = _portfolio_snapshot(client)
    state["session_start_equity"] = float(initial["equity_usd"])
    _save_state(state)

    errors = 0

    _log({
        "event": "LIVE_MANAGER_START",
        "pid": os.getpid(),
        "session_start_equity": state["session_start_equity"],
        "max_session_drawdown_pct": MAX_SESSION_DRAWDOWN_PCT,
    })
    print("FUTURES LIVE MANAGER: armed")
    print(
        f"manage_open_positions_only=true | "
        f"no_progress={NO_PROGRESS_SEC}s | hard_max={HARD_MAX_HOLD_SEC}s"
    )

    while True:
        try:
            guard = _drawdown_guard(client, state)
            if guard is not None:
                _save_state(state)
                print(
                    f"KILL SWITCH: equity drawdown reached {MAX_SESSION_DRAWDOWN_PCT:.0f}%. "
                    f"Flatten attempted. Manager stops."
                )
                return

            actions = _manage_positions(client, state)
            snap = _portfolio_snapshot(client)
            _save_state(state)

            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"equity=USD {snap['equity_usd']:.4f} | "
                f"open={snap['open_position_count']}/{MAX_OPEN_POSITIONS} | "
                f"notional=USD {snap['portfolio_notional_usd']:.4f} | "
                f"new_live_entries=confirmation_required | exits={len(actions)}"
            )
            errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            errors += 1
            _log({"event": "LOOP_ERROR", "error": f"{type(exc).__name__}: {exc}", "count": errors})
            print(f"AUTOPILOT ERROR {errors}/{MAX_CONSECUTIVE_ERRORS}: {type(exc).__name__}: {exc}")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                save_policy(_policy_patch(False))
                _log({"event": "AUTOPILOT_CIRCUIT_BREAKER", "reason": "MAX_CONSECUTIVE_ERRORS"})
                print("Circuit breaker: nove vstupy zastaveny. Exchange STOP/TP zustavaji aktivni.")
                return
        time.sleep(LOOP_SEC)


def status() -> dict[str, Any]:
    state = _load_state()
    client = client_from_env()
    snap = _portfolio_snapshot(client)
    return {
        "ok": True,
        "mode": "FUTURES_LIVE_MANAGER",
        "new_live_entries": "confirmation_required",
        "autonomous_position_management": True,
        "rules": {
            "loop_sec": LOOP_SEC,
            "quick_profit_gross_bps": QUICK_PROFIT_GROSS_BPS,
            "small_profit_after_sec": SMALL_PROFIT_AFTER_SEC,
            "small_profit_gross_bps": SMALL_PROFIT_GROSS_BPS,
            "no_progress_sec": NO_PROGRESS_SEC,
            "hard_max_hold_sec": HARD_MAX_HOLD_SEC,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "max_trade_notional_usd": MAX_NOTIONAL_USD,
            "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
            "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
            "max_session_drawdown_pct": MAX_SESSION_DRAWDOWN_PCT,
        },
        "exchange": {
            "equity_usd": snap["equity_usd"],
            "open_position_count": snap["open_position_count"],
            "portfolio_notional_usd": snap["portfolio_notional_usd"],
            "positions": snap["positions"],
        },
        "state": state,
    }


def selftest() -> dict[str, Any]:
    checks = {
        "quick_profit": _exit_reason(30, 31.0, 31.0) == "QUICK_PROFIT",
        "small_profit": _exit_reason(100, 23.0, 23.0) == "SMALL_PROFIT",
        "no_progress": _exit_reason(181, 5.0, 15.0) == "NO_PROGRESS",
        "hard_max": _exit_reason(481, 100.0, 100.0) == "HARD_MAX_HOLD",
        "no_early_exit": _exit_reason(10, 100.0, 100.0) is None,
        "constants_sane": (
            QUICK_PROFIT_GROSS_BPS > 20.0
            and SMALL_PROFIT_GROSS_BPS >= 20.0
            and NO_PROGRESS_SEC < HARD_MAX_HOLD_SEC
            and MAX_OPEN_POSITIONS == 4
        ),
        "drawdown_limit_is_50": MAX_SESSION_DRAWDOWN_PCT == 50.0,
        "capital_budget_is_22": SESSION_CAPITAL_USD == 22.0,
    }
    return {"ok": all(checks.values()), "checks": checks}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            text = (result.stdout or "").lower()
            return str(pid) in text and "no tasks are running" not in text
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _acquire_pid_lock() -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0
        if _pid_is_running(pid):
            raise RuntimeError(f"Autopilot uz bezi pod PID {pid}")
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _release_pid_lock() -> None:
    try:
        if PID_PATH.exists() and PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_PATH.unlink()
    except Exception:
        pass


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
        if args.confirm != "ARM-LIVE-MANAGER":
            raise SystemExit("Live manager requires --confirm ARM-LIVE-MANAGER")
        _acquire_pid_lock()
        try:
            run_forever()
        finally:
            save_policy(_policy_patch(False))
            _release_pid_lock()
        return

    ap.print_help()


if __name__ == "__main__":
    main()

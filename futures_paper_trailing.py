from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests

from futures_autopilot import (
    HARD_MAX_HOLD_SEC,
    NO_PROGRESS_CURRENT_BPS,
    NO_PROGRESS_MAX_FAVORABLE_BPS,
    NO_PROGRESS_SEC,
    _trailing_floor_bps,
)
from futures_canary import (
    BACKUP_TAKE_PROFIT_BPS,
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_NOTIONAL_USD,
    ROUND_TRIP_TAKER_COST_BPS,
    TARGET_NOTIONAL_USD,
    public_scan,
)
from futures_pairs import scan_pairs

STATE_PATH = Path("data/futures_paper_trailing_state.json")
EVENT_LOG = Path("data/futures_paper_trailing_events.jsonl")
REPORT_PATH = Path("reports/futures_paper_trailing_latest.json")

PAPER_START_EQUITY_USD = 22.0
SESSION_DURATION_SEC = 60 * 60
LOOP_SEC = 5
SIGNAL_REFRESH_SEC = 30
PAIR_REFRESH_SEC = 60
REENTRY_COOLDOWN_SEC = 180
HARD_STOP_BPS = 45.0
MAX_NEW_ENTRIES = 40
PAIR_MODES = {"inverse_correlation", "relative_value"}
TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_state() -> dict[str, Any]:
    now = _now_ms()
    return {
        "version": 1,
        "created_ts_ms": now,
        "session_start_ts_ms": now,
        "session_deadline_ts_ms": now + SESSION_DURATION_SEC * 1000,
        "equity_usd": PAPER_START_EQUITY_USD,
        "start_equity_usd": PAPER_START_EQUITY_USD,
        "positions": {},
        "pairs": {},
        "cooldowns": {},
        "closed": [],
        "stats": {
            "entries": 0,
            "exits": 0,
            "wins": 0,
            "losses": 0,
            "pair_entries": 0,
            "single_entries": 0,
        },
    }


def _load_state(reset: bool = False) -> dict[str, Any]:
    if reset or not STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _default_state()


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _log(event: dict[str, Any]) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts_ms": _now_ms(), **event}, ensure_ascii=False) + "\n")


def _ticker_mids() -> dict[str, float]:
    r = requests.get(TICKERS_URL, timeout=15)
    r.raise_for_status()
    rows = r.json().get("tickers") or []
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        try:
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
            if bid > 0 and ask >= bid:
                out[symbol] = (bid + ask) / 2.0
                continue
            mark = float(row.get("markPrice") or row.get("last") or 0.0)
            if mark > 0:
                out[symbol] = mark
        except Exception:
            continue
    return out


def _pnl_bps(pos: dict[str, Any], mid: float) -> float:
    entry = float(pos["entry_price"])
    if entry <= 0:
        return 0.0
    direction = 1.0 if str(pos["side"]).upper() == "LONG" else -1.0
    return direction * (mid / entry - 1.0) * 10000.0


def _exit_reason(age_sec: float, pnl_bps: float, max_fav_bps: float) -> str | None:
    if pnl_bps <= -HARD_STOP_BPS:
        return "HARD_STOP"
    if pnl_bps >= BACKUP_TAKE_PROFIT_BPS:
        return "BACKUP_TAKE_PROFIT"

    floor = _trailing_floor_bps(max_fav_bps)
    if floor is not None:
        if pnl_bps <= floor:
            return "TRAILING_RATCHET"
        return None

    if (
        age_sec >= NO_PROGRESS_SEC
        and pnl_bps < NO_PROGRESS_CURRENT_BPS
        and max_fav_bps < NO_PROGRESS_MAX_FAVORABLE_BPS
    ):
        return "NO_PROGRESS"

    if age_sec >= HARD_MAX_HOLD_SEC:
        return "HARD_MAX_HOLD"
    return None


def _cooldown_ok(state: dict[str, Any], symbol: str, now_ms: int) -> bool:
    return now_ms >= int(state.get("cooldowns", {}).get(symbol, 0) or 0)


def _portfolio_notional(state: dict[str, Any]) -> float:
    return sum(float(x.get("notional_usd") or 0.0) for x in state["positions"].values())


def _can_open(state: dict[str, Any], symbols: list[str], now_ms: int) -> bool:
    if len(state["positions"]) + len(symbols) > MAX_OPEN_POSITIONS:
        return False
    projected = _portfolio_notional(state) + TARGET_NOTIONAL_USD * len(symbols)
    if projected > MAX_PORTFOLIO_NOTIONAL_USD + 1e-9:
        return False
    for symbol in symbols:
        if symbol in state["positions"] or not _cooldown_ok(state, symbol, now_ms):
            return False
    return int(state["stats"].get("entries", 0)) + len(symbols) <= MAX_NEW_ENTRIES


def _open_leg(
    state: dict[str, Any],
    symbol: str,
    side: str,
    mid: float,
    source: str,
    pair_id: str | None = None,
) -> None:
    now = _now_ms()
    state["positions"][symbol] = {
        "symbol": symbol,
        "side": side.upper(),
        "entry_price": float(mid),
        "notional_usd": TARGET_NOTIONAL_USD,
        "opened_ts_ms": now,
        "max_favorable_bps": 0.0,
        "last_pnl_bps": 0.0,
        "source": source,
        "pair_id": pair_id,
    }
    state["stats"]["entries"] = int(state["stats"].get("entries", 0)) + 1
    _log({
        "event": "PAPER_ENTRY",
        "symbol": symbol,
        "side": side.upper(),
        "entry_price": mid,
        "notional_usd": TARGET_NOTIONAL_USD,
        "source": source,
        "pair_id": pair_id,
    })


def _close_leg(state: dict[str, Any], symbol: str, mid: float, reason: str) -> dict[str, Any] | None:
    pos = state["positions"].get(symbol)
    if not pos:
        return None
    pnl_bps = _pnl_bps(pos, mid)
    gross_usd = float(pos["notional_usd"]) * pnl_bps / 10000.0
    modeled_cost_usd = float(pos["notional_usd"]) * ROUND_TRIP_TAKER_COST_BPS / 10000.0
    net_usd = gross_usd - modeled_cost_usd
    state["equity_usd"] = float(state["equity_usd"]) + net_usd
    state["stats"]["exits"] = int(state["stats"].get("exits", 0)) + 1
    if net_usd > 0:
        state["stats"]["wins"] = int(state["stats"].get("wins", 0)) + 1
    elif net_usd < 0:
        state["stats"]["losses"] = int(state["stats"].get("losses", 0)) + 1

    row = {
        **pos,
        "exit_price": float(mid),
        "closed_ts_ms": _now_ms(),
        "exit_reason": reason,
        "gross_pnl_bps": pnl_bps,
        "modeled_round_trip_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
        "gross_usd": gross_usd,
        "net_usd": net_usd,
    }
    state["closed"].append(row)
    state["closed"] = state["closed"][-500:]
    state["cooldowns"][symbol] = _now_ms() + REENTRY_COOLDOWN_SEC * 1000
    state["positions"].pop(symbol, None)
    _log({"event": "PAPER_EXIT", **row})
    return row


def _manage_pairs(state: dict[str, Any], mids: dict[str, float], now_ms: int) -> set[str]:
    handled: set[str] = set()
    for pair_id, meta in list(state["pairs"].items()):
        legs = [s for s in meta.get("legs", []) if s in state["positions"]]
        if not legs:
            state["pairs"].pop(pair_id, None)
            continue
        if len(legs) < 2:
            for symbol in legs:
                mid = mids.get(symbol)
                if mid:
                    _close_leg(state, symbol, mid, "PAIR_LEG_MISSING")
                    handled.add(symbol)
            state["pairs"].pop(pair_id, None)
            continue
        if any(symbol not in mids for symbol in legs):
            continue

        pnls = [_pnl_bps(state["positions"][symbol], mids[symbol]) for symbol in legs]
        combined_bps = sum(pnls) / len(pnls)
        age_sec = max(0.0, (now_ms - int(meta["opened_ts_ms"])) / 1000.0)
        max_fav = max(float(meta.get("max_favorable_bps") or 0.0), combined_bps)
        meta["max_favorable_bps"] = max_fav
        meta["last_pnl_bps"] = combined_bps
        meta["trailing_floor_bps"] = _trailing_floor_bps(max_fav)
        reason = _exit_reason(age_sec, combined_bps, max_fav)
        if reason:
            for symbol in list(legs):
                _close_leg(state, symbol, mids[symbol], f"PAIR_{reason}")
                handled.add(symbol)
            state["pairs"].pop(pair_id, None)
    return handled


def _manage_positions(state: dict[str, Any], mids: dict[str, float]) -> None:
    now = _now_ms()
    handled = _manage_pairs(state, mids, now)
    for symbol, pos in list(state["positions"].items()):
        if symbol in handled or pos.get("pair_id"):
            continue
        mid = mids.get(symbol)
        if mid is None:
            continue
        pnl_bps = _pnl_bps(pos, mid)
        age_sec = max(0.0, (now - int(pos["opened_ts_ms"])) / 1000.0)
        max_fav = max(float(pos.get("max_favorable_bps") or 0.0), pnl_bps)
        pos["max_favorable_bps"] = max_fav
        pos["last_pnl_bps"] = pnl_bps
        pos["trailing_floor_bps"] = _trailing_floor_bps(max_fav)
        reason = _exit_reason(age_sec, pnl_bps, max_fav)
        if reason:
            _close_leg(state, symbol, mid, reason)


def _try_pair_entry(state: dict[str, Any], pair_scan: dict[str, Any], mids: dict[str, float]) -> bool:
    now = _now_ms()
    for cand in pair_scan.get("top", []):
        if str(cand.get("mode")) not in PAIR_MODES:
            continue
        long_symbol = str(cand.get("long_symbol") or "").upper()
        short_symbol = str(cand.get("short_symbol") or "").upper()
        if not long_symbol or not short_symbol or long_symbol == short_symbol:
            continue
        if long_symbol not in mids or short_symbol not in mids:
            continue
        if not _can_open(state, [long_symbol, short_symbol], now):
            continue
        pair_id = f"pair-{now}-{long_symbol}-{short_symbol}"
        state["pairs"][pair_id] = {
            "pair_id": pair_id,
            "mode": cand.get("mode"),
            "opened_ts_ms": now,
            "legs": [long_symbol, short_symbol],
            "max_favorable_bps": 0.0,
            "candidate": cand,
        }
        _open_leg(state, long_symbol, "LONG", mids[long_symbol], f"pair:{cand.get('mode')}", pair_id)
        _open_leg(state, short_symbol, "SHORT", mids[short_symbol], f"pair:{cand.get('mode')}", pair_id)
        state["stats"]["pair_entries"] = int(state["stats"].get("pair_entries", 0)) + 1
        return True
    return False


def _try_single_entry(state: dict[str, Any], scan: dict[str, Any], mids: dict[str, float]) -> bool:
    now = _now_ms()
    rows = [x for x in scan.get("all", []) if x.get("canary_signal_ready")]
    for cand in rows:
        symbol = str(cand.get("symbol") or "").upper()
        side = str(cand.get("side") or "").upper()
        if side not in {"LONG", "SHORT"} or symbol not in mids:
            continue
        if not _can_open(state, [symbol], now):
            continue
        _open_leg(state, symbol, side, mids[symbol], "canary")
        state["stats"]["single_entries"] = int(state["stats"].get("single_entries", 0)) + 1
        return True
    return False


def _write_report(state: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    closed = state.get("closed", [])
    net = sum(float(x.get("net_usd") or 0.0) for x in closed)
    wins = sum(1 for x in closed if float(x.get("net_usd") or 0.0) > 0)
    report = {
        "mode": "PAPER_ONLY",
        "actual_order_submitted": False,
        "generated_ts_ms": _now_ms(),
        "start_equity_usd": state.get("start_equity_usd"),
        "equity_usd": state.get("equity_usd"),
        "net_usd": net,
        "closed_legs": len(closed),
        "win_rate_pct": (wins / len(closed) * 100.0) if closed else None,
        "open_positions": list(state.get("positions", {}).values()),
        "stats": state.get("stats"),
        "trailing": {
            "hard_stop_bps": HARD_STOP_BPS,
            "backup_take_profit_bps": BACKUP_TAKE_PROFIT_BPS,
            "round_trip_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
            "note": "Ratchet floor is imported from futures_autopilot and never loosens after max favorable excursion rises.",
        },
        "closed": closed[-100:],
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def run(reset: bool = False) -> None:
    state = _load_state(reset=reset)
    if reset:
        try:
            EVENT_LOG.unlink()
        except FileNotFoundError:
            pass

    last_signal = 0.0
    last_pair = 0.0
    scan: dict[str, Any] = {}
    pair_scan: dict[str, Any] = {}

    print("FUTURES PAPER TRAILING: RUNNING | NO LIVE ORDERS")
    print(
        f"{SESSION_DURATION_SEC // 60} min | start USD {state['equity_usd']:.2f} | "
        f"max {MAX_OPEN_POSITIONS} legs | USD {TARGET_NOTIONAL_USD:.2f}/leg | "
        f"hard stop {HARD_STOP_BPS:.0f} bps | backup TP {BACKUP_TAKE_PROFIT_BPS:.0f} bps"
    )

    while _now_ms() < int(state["session_deadline_ts_ms"]):
        try:
            mids = _ticker_mids()
            _manage_positions(state, mids)
            now = time.time()

            if now - last_signal >= SIGNAL_REFRESH_SEC:
                scan = public_scan()
                last_signal = now

            if scan and now - last_pair >= PAIR_REFRESH_SEC:
                symbols = [str(s).upper() for s in scan.get("symbols", []) if s]
                pair_scan = scan_pairs(symbols)
                last_pair = now

            opened = False
            if pair_scan:
                opened = _try_pair_entry(state, pair_scan, mids)
            if not opened and scan:
                _try_single_entry(state, scan, mids)

            _save_state(state)
            _write_report(state)

            open_rows = list(state["positions"].values())
            open_desc = ", ".join(
                f"{x['symbol']} {x['side']} {float(x.get('last_pnl_bps') or 0.0):+.1f}bps"
                for x in open_rows
            ) or "none"
            print(
                f"[{time.strftime('%H:%M:%S')}] equity=USD {float(state['equity_usd']):.4f} "
                f"| open={len(open_rows)}/{MAX_OPEN_POSITIONS} | {open_desc}"
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _log({"event": "PAPER_LOOP_ERROR", "error": f"{type(exc).__name__}: {exc}"})
            print(f"PAPER ERROR: {type(exc).__name__}: {exc}")

        time.sleep(LOOP_SEC)

    mids = _ticker_mids()
    for pair_id, meta in list(state["pairs"].items()):
        for symbol in list(meta.get("legs", [])):
            if symbol in state["positions"] and symbol in mids:
                _close_leg(state, symbol, mids[symbol], "SESSION_END")
        state["pairs"].pop(pair_id, None)
    for symbol in list(state["positions"]):
        if symbol in mids:
            _close_leg(state, symbol, mids[symbol], "SESSION_END")

    _save_state(state)
    _write_report(state)
    print(
        f"PAPER COMPLETE | equity=USD {float(state['equity_usd']):.4f} | "
        f"closed={len(state['closed'])} | report={REPORT_PATH}"
    )


def selftest() -> dict[str, Any]:
    checks = {
        "hard_stop": _exit_reason(10, -46.0, 0.0) == "HARD_STOP",
        "no_progress": _exit_reason(NO_PROGRESS_SEC + 1, 0.0, 10.0) == "NO_PROGRESS",
        "trailing_armed": _trailing_floor_bps(50.0) is not None,
        "trailing_exit": _exit_reason(30, 30.0, 50.0) == "TRAILING_RATCHET",
        "winner_runs": _exit_reason(30, 48.0, 50.0) is None,
        "backup_tp": _exit_reason(30, BACKUP_TAKE_PROFIT_BPS + 1, BACKUP_TAKE_PROFIT_BPS + 1) == "BACKUP_TAKE_PROFIT",
    }
    return {"ok": all(checks.values()), "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        raise SystemExit(0 if r["ok"] else 2)
    if args.run:
        run(reset=args.reset)
        return
    ap.print_help()


if __name__ == "__main__":
    main()

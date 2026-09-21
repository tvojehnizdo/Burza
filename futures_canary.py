from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from engine import live_futures_pulse
from futures_private import (
    client_from_env,
    load_policy,
    min_lot,
    order_preflight,
    place_order,
    position_map,
    readiness,
    round_size_down,
    save_policy,
)

SYMBOLS = ["PF_ETHUSD", "PF_SOLUSD", "PF_XBTUSD"]
EVENT_LOG = Path("data/futures_canary_events.jsonl")

# Tier-1 Futures taker fee 5 bps/side + conservative 2 bps slippage
# + 3 bps execution buffer/side = 20 bps modeled round-trip.
TAKER_FEE_BPS_PER_SIDE = 5.0
SLIPPAGE_BPS_PER_SIDE = 2.0
EXEC_BUFFER_BPS_PER_SIDE = 3.0
ROUND_TRIP_TAKER_COST_BPS = 2.0 * (
    TAKER_FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE + EXEC_BUFFER_BPS_PER_SIDE
)
MIN_TAKER_NET_EDGE_BPS = 5.0
MAX_NOTIONAL_USD = 3.0
MAX_NOTIONAL_PCT_EQUITY = 25.0
MAX_OPEN_POSITIONS = 1


def _log(event: dict[str, Any]) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts_ms": int(time.time() * 1000), **event}
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def public_scan() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        try:
            p = live_futures_pulse(symbol)
        except Exception as exc:
            rows.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not p:
            continue

        expected_bps = float(p.get("expected_move_proxy_pct") or 0.0) * 100.0
        net_taker_bps = expected_bps - ROUND_TRIP_TAKER_COST_BPS
        side = str(p.get("side") or "NONE").upper()
        confidence = float(p.get("confidence") or 0.0)
        pulse = bool(p.get("pulse"))

        p = dict(p)
        p.update({
            "expected_move_proxy_bps": round(expected_bps, 3),
            "taker_round_trip_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
            "taker_net_edge_bps": round(net_taker_bps, 3),
            "canary_signal_ready": bool(
                pulse
                and side in {"LONG", "SHORT"}
                and confidence >= 0.64
                and net_taker_bps >= MIN_TAKER_NET_EDGE_BPS
            ),
        })
        rows.append(p)

    rows.sort(
        key=lambda x: (
            bool(x.get("canary_signal_ready")),
            float(x.get("taker_net_edge_bps") or -999.0),
            float(x.get("confidence") or 0.0),
        ),
        reverse=True,
    )
    ready = [x for x in rows if x.get("canary_signal_ready")]
    return {
        "ready": bool(ready),
        "reason": "FUTURES_CANARY_SIGNAL_READY" if ready else "NO_POSITIVE_FUTURES_CANARY",
        "candidate": ready[0] if ready else (rows[0] if rows else None),
        "ready_count": len(ready),
        "all": rows,
        "round_trip_taker_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
        "min_net_edge_bps": MIN_TAKER_NET_EDGE_BPS,
        "actual_order_submitted": False,
    }


def _ticker_mid(client: Any, symbol: str) -> float:
    payload = client.tickers()
    rows = payload.get("tickers") or []
    for row in rows:
        if str(row.get("symbol") or "").upper() != symbol.upper():
            continue
        try:
            bid = float(row.get("bid"))
            ask = float(row.get("ask"))
            if bid > 0 and ask >= bid:
                return (bid + ask) / 2.0
        except Exception:
            pass
        for key in ("markPrice", "last", "indexPrice"):
            try:
                px = float(row.get(key))
                if px > 0:
                    return px
            except Exception:
                continue
    raise RuntimeError(f"No usable ticker for {symbol}")


def _position_size(client: Any, symbol: str) -> float:
    return float(position_map(client.open_positions()).get(symbol.upper(), 0.0))


def private_plan() -> dict[str, Any]:
    scan = public_scan()
    r = readiness()

    if not r.get("safe_to_arm"):
        return {
            "ready": False,
            "reason": "FUTURES_ACCOUNT_NOT_READY",
            "public_scan": scan,
            "readiness": r,
            "actual_order_submitted": False,
        }
    if int(r.get("open_position_count") or 0) != 0:
        return {
            "ready": False,
            "reason": "EXISTING_FUTURES_POSITION",
            "public_scan": scan,
            "readiness": r,
            "actual_order_submitted": False,
        }

    equity = float(r.get("equity_usd") or 0.0)
    notional_cap = min(MAX_NOTIONAL_USD, equity * MAX_NOTIONAL_PCT_EQUITY / 100.0)
    if notional_cap <= 0:
        return {
            "ready": False,
            "reason": "NO_FUTURES_EQUITY",
            "public_scan": scan,
            "readiness": r,
            "actual_order_submitted": False,
        }

    candidates = [x for x in scan.get("all", []) if x.get("canary_signal_ready")]
    client = client_from_env()
    executable: list[dict[str, Any]] = []

    for p in candidates:
        symbol = str(p["symbol"]).upper()
        px = _ticker_mid(client, symbol)
        raw_size = notional_cap / px
        size = round_size_down(symbol, raw_size)
        minimum = min_lot(symbol)
        if size < minimum:
            continue
        side = "buy" if str(p.get("side")).upper() == "LONG" else "sell"
        try:
            pre = order_preflight(symbol, side, size, reduce_only=False, client=client)
        except Exception as exc:
            continue

        atr_frac = max(float(p.get("atr_pct") or 0.0) / 100.0, 0.0001)
        stop_frac = min(max(1.8 * atr_frac, 0.0035), 0.0120)
        take_frac = min(max(2.0 * stop_frac, 0.0060), 0.0250)

        if side == "buy":
            stop_price = px * (1.0 - stop_frac)
            take_price = px * (1.0 + take_frac)
        else:
            stop_price = px * (1.0 + stop_frac)
            take_price = px * (1.0 - take_frac)

        executable.append({
            "symbol": symbol,
            "side": side,
            "size": size,
            "mid_price": px,
            "estimated_notional_usd": size * px,
            "equity_usd": equity,
            "notional_cap_usd": notional_cap,
            "stop_price": stop_price,
            "take_profit_price": take_price,
            "stop_distance_pct": stop_frac * 100.0,
            "take_profit_distance_pct": take_frac * 100.0,
            "taker_net_edge_bps": p.get("taker_net_edge_bps"),
            "confidence": p.get("confidence"),
            "source_signal": p,
            "preflight": pre,
        })

    executable.sort(
        key=lambda x: (float(x["taker_net_edge_bps"]), float(x["confidence"])),
        reverse=True,
    )

    return {
        "ready": bool(executable),
        "reason": "FUTURES_CANARY_EXECUTABLE" if executable else "SIGNAL_EXISTS_BUT_NOT_EXECUTABLE",
        "candidate": executable[0] if executable else None,
        "alternatives": executable[1:],
        "public_scan": scan,
        "readiness": r,
        "policy_target": {
            "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
            "max_order_notional_usd": MAX_NOTIONAL_USD,
            "max_open_positions": MAX_OPEN_POSITIONS,
        },
        "actual_order_submitted": False,
    }


def execute() -> dict[str, Any]:
    # Keep policy disarmed during planning. It is armed only for the few calls
    # needed to establish the canary and protective reduce-only orders.
    save_policy({
        "live_execution": False,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "allowed_roots": ["XBTUSD", "ETHUSD", "SOLUSD"],
    })

    plan = private_plan()
    if not plan.get("ready"):
        return {
            **plan,
            "actual_order_submitted": False,
        }

    candidate = dict(plan["candidate"])
    symbol = candidate["symbol"]
    side = candidate["side"]
    size = float(candidate["size"])
    exit_side = "sell" if side == "buy" else "buy"
    client = client_from_env()

    entry: dict[str, Any] | None = None
    stop: dict[str, Any] | None = None
    take: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None

    try:
        # Deactivate any stale dead-man timer so it cannot later cancel the
        # protective orders we are about to place.
        try:
            client.deadman(0)
        except Exception:
            pass

        save_policy({"live_execution": True})
        entry = place_order(
            symbol,
            side,
            size,
            reduce_only=False,
            order_type="mkt",
            cli_ord_id=f"canary-entry-{int(time.time())}",
            use_deadman=False,
        )

        if not entry.get("submitted_live"):
            raise RuntimeError(f"Entry not submitted: {entry}")

        actual_size = 0.0
        for _ in range(20):
            time.sleep(0.35)
            actual_size = abs(_position_size(client, symbol))
            if actual_size >= min_lot(symbol):
                break
        if actual_size < min_lot(symbol):
            raise RuntimeError("Entry was submitted but no open position became visible")

        protected_size = round_size_down(symbol, min(actual_size, size))
        if protected_size < min_lot(symbol):
            raise RuntimeError("Visible position is below supported protective-order size")

        stop = place_order(
            symbol,
            exit_side,
            protected_size,
            reduce_only=True,
            order_type="stp",
            stop_price=float(candidate["stop_price"]),
            trigger_signal="mark",
            cli_ord_id=f"canary-stop-{int(time.time())}",
            use_deadman=False,
        )
        if not stop.get("submitted_live"):
            raise RuntimeError(f"Stop order not submitted: {stop}")

        take = place_order(
            symbol,
            exit_side,
            protected_size,
            reduce_only=True,
            order_type="take_profit",
            stop_price=float(candidate["take_profit_price"]),
            trigger_signal="mark",
            cli_ord_id=f"canary-tp-{int(time.time())}",
            use_deadman=False,
        )
        if not take.get("submitted_live"):
            raise RuntimeError(f"Take-profit order not submitted: {take}")

        result = {
            "ok": True,
            "reason": "FUTURES_CANARY_LIVE_WITH_PROTECTION",
            "candidate": candidate,
            "entry": entry,
            "stop": stop,
            "take_profit": take,
            "actual_order_submitted": True,
        }
        _log(result)
        return result

    except Exception as exc:
        # If any protection step fails after an entry, flatten immediately.
        try:
            visible = _position_size(client, symbol)
            if abs(visible) >= min_lot(symbol):
                close_side = "sell" if visible > 0 else "buy"
                close_size = round_size_down(symbol, abs(visible))
                compensation = place_order(
                    symbol,
                    close_side,
                    close_size,
                    reduce_only=True,
                    order_type="mkt",
                    cli_ord_id=f"canary-flat-{int(time.time())}",
                    use_deadman=False,
                )
        except Exception as comp_exc:
            compensation = {"error": f"{type(comp_exc).__name__}: {comp_exc}"}

        result = {
            "ok": False,
            "reason": "FUTURES_CANARY_ABORTED",
            "error": f"{type(exc).__name__}: {exc}",
            "candidate": candidate,
            "entry": entry,
            "stop": stop,
            "take_profit": take,
            "compensation": compensation,
            "actual_order_submitted": bool(entry and entry.get("submitted_live")),
        }
        _log(result)
        return result
    finally:
        save_policy({"live_execution": False})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args()

    if args.execute:
        if args.confirm != "SPUSTIT-FUTURES-CANARY":
            raise SystemExit("Execution requires --confirm SPUSTIT-FUTURES-CANARY")
        print(json.dumps(execute(), indent=2, ensure_ascii=False, default=str))
        return
    if args.plan:
        print(json.dumps(private_plan(), indent=2, ensure_ascii=False, default=str))
        return
    print(json.dumps(public_scan(), indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

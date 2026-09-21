from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from futures_private import (
    client_from_env,
    instrument_specs,
    load_policy,
    min_lot,
    order_preflight,
    place_order,
    position_map,
    readiness,
    round_size_down,
    round_price_to_tick,
    save_policy,
)

MAX_UNIVERSE = 24
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
MAX_NOTIONAL_PCT_EQUITY = 35.0
MAX_OPEN_POSITIONS = 1
CHARTS = "https://futures.kraken.com/api/charts/v1"


def _candles(symbol: str, count: int = 120) -> pd.DataFrame:
    r = requests.get(
        f"{CHARTS}/trade/{symbol}/1m",
        params={"count": count},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    rows = body.get("candles") or []
    if not rows:
        raise RuntimeError(f"No futures candles for {symbol}")
    df = pd.DataFrame(rows)
    needed = ["time", "open", "high", "low", "close", "volume"]
    missing = [x for x in needed if x not in df.columns]
    if missing:
        raise RuntimeError(f"Unexpected futures candle schema for {symbol}: {missing}")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().reset_index(drop=True)


def _ret(s: pd.Series, n: int) -> float:
    if len(s) <= n:
        return 0.0
    a = float(s.iloc[-n - 1])
    b = float(s.iloc[-1])
    return b / a - 1.0 if a > 0 else 0.0


def _signal(symbol: str) -> dict[str, Any]:
    df = _candles(symbol, 120)
    if len(df) < 65:
        raise RuntimeError(f"Insufficient candles for {symbol}: {len(df)}")

    close = df["close"]
    last = float(close.iloc[-1])
    ema6 = float(close.ewm(span=6, adjust=False).mean().iloc[-1])
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    r5 = _ret(close, 5)
    r15 = _ret(close, 15)
    r30 = _ret(close, 30)
    r60 = _ret(close, 60)

    prev = close.shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())
    atr_bps = atr / last * 10000.0 if last > 0 else 0.0

    vol_med = float(df["volume"].tail(30).median())
    vol_ratio = float(df["volume"].iloc[-1] / vol_med) if vol_med > 0 else 1.0

    up = last > ema6 > ema20 and r5 > 0 and r15 > 0
    down = last < ema6 < ema20 and r5 < 0 and r15 < 0
    side = "LONG" if up else "SHORT" if down else "NONE"

    momentum_bps = max(
        abs(r5) * 10000.0 * 0.55,
        abs(r15) * 10000.0 * 0.70,
        abs(r30) * 10000.0 * 0.55,
        abs(r60) * 10000.0 * 0.35,
    )
    expected_bps = max(0.0, min(momentum_bps, atr_bps * 5.0))
    net_taker_bps = expected_bps - ROUND_TRIP_TAKER_COST_BPS

    confirmations = 0
    if side != "NONE":
        confirmations += 1
    if (side == "LONG" and r30 > 0) or (side == "SHORT" and r30 < 0):
        confirmations += 1
    if (side == "LONG" and r60 > 0) or (side == "SHORT" and r60 < 0):
        confirmations += 1
    if vol_ratio >= 0.45:
        confirmations += 1

    confidence = min(0.95, 0.45 + 0.10 * confirmations + min(expected_bps / 500.0, 0.15))
    ready = (
        side in {"LONG", "SHORT"}
        and confirmations >= 3
        and confidence >= 0.64
        and net_taker_bps >= MIN_TAKER_NET_EDGE_BPS
    )

    return {
        "symbol": symbol,
        "market": "futures",
        "price": last,
        "side": side,
        "confidence": round(confidence, 4),
        "confirmations": confirmations,
        "r5_bps": round(r5 * 10000.0, 3),
        "r15_bps": round(r15 * 10000.0, 3),
        "r30_bps": round(r30 * 10000.0, 3),
        "r60_bps": round(r60 * 10000.0, 3),
        "atr_pct": round(atr_bps / 100.0, 4),
        "atr_bps": round(atr_bps, 3),
        "volume_ratio": round(vol_ratio, 3),
        "expected_move_proxy_bps": round(expected_bps, 3),
        "taker_round_trip_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
        "taker_net_edge_bps": round(net_taker_bps, 3),
        "canary_signal_ready": bool(ready),
    }


def _dynamic_universe(max_symbols: int = MAX_UNIVERSE) -> list[str]:
    specs = instrument_specs()
    r = requests.get("https://futures.kraken.com/derivatives/api/v3/tickers", timeout=20)
    r.raise_for_status()
    rows = r.json().get("tickers") or []
    ranked: list[tuple[float, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        spec = specs.get(symbol) or {}
        if not symbol.startswith("PF_") or not bool(spec.get("tradeable")):
            continue
        try:
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
        except Exception:
            continue
        if bid <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        try:
            min_notional = float(spec.get("qty_step") or 0.0) * mid
        except Exception:
            min_notional = 999999.0
        if min_notional <= 0 or min_notional > MAX_NOTIONAL_USD:
            continue
        liquidity = 0.0
        for key in ("volumeQuote", "volume24h", "volume", "openInterest"):
            try:
                liquidity = max(liquidity, float(row.get(key) or 0.0))
            except Exception:
                pass
        spread_bps = ((ask - bid) / mid * 10000.0) if mid > 0 else 9999.0
        score = liquidity - spread_bps * 1000.0
        ranked.append((score, symbol))
    ranked.sort(reverse=True)
    return [s for _, s in ranked[:max_symbols]]


def _log(event: dict[str, Any]) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts_ms": int(time.time() * 1000), **event}
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def public_scan() -> dict[str, Any]:
    symbols = _dynamic_universe()
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as pool:
        futs = {pool.submit(_signal, symbol): symbol for symbol in symbols}
        for fut in as_completed(futs):
            symbol = futs[fut]
            try:
                rows.append(fut.result())
            except Exception as exc:
                rows.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

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
        "universe_count": len(symbols),
        "symbols": symbols,
        "all": rows,
        "round_trip_taker_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
        "min_net_edge_bps": MIN_TAKER_NET_EDGE_BPS,
        "actual_order_submitted": False,
        "note": "Dynamic Kraken PF_* perpetual universe; 1m momentum/ATR screen with taker-cost gating.",
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
    # Keep the read-only planning policy aligned with the canary constants.
    # This never arms live execution; it only synchronizes risk caps used by
    # order_preflight so plan and execution evaluate the same limits.
    save_policy({
        "live_execution": False,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "allowed_roots": ["*"],
    })
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
    rejected: list[dict[str, Any]] = []

    for p in candidates:
        symbol = str(p["symbol"]).upper()
        px = _ticker_mid(client, symbol)
        raw_size = notional_cap / px
        size = round_size_down(symbol, raw_size)
        minimum = min_lot(symbol)
        if size < minimum:
            rejected.append({
                "symbol": symbol,
                "reason": "BELOW_MIN_LOT_AFTER_CAP",
                "price": px,
                "equity_usd": equity,
                "notional_cap_usd": notional_cap,
                "raw_size": raw_size,
                "rounded_size": size,
                "min_lot": minimum,
                "minimum_lot_notional_usd": minimum * px,
                "signal_net_edge_bps": p.get("taker_net_edge_bps"),
            })
            continue
        side = "buy" if str(p.get("side")).upper() == "LONG" else "sell"
        try:
            pre = order_preflight(symbol, side, size, reduce_only=False, client=client)
        except Exception as exc:
            rejected.append({
                "symbol": symbol,
                "reason": "PREFLIGHT_REJECTED",
                "error": f"{type(exc).__name__}: {exc}",
                "price": px,
                "equity_usd": equity,
                "notional_cap_usd": notional_cap,
                "size": size,
                "estimated_notional_usd": size * px,
                "min_lot": minimum,
                "signal_net_edge_bps": p.get("taker_net_edge_bps"),
            })
            continue

        atr_frac = max(float(p.get("atr_pct") or 0.0) / 100.0, 0.0001)
        stop_frac = min(max(1.8 * atr_frac, 0.0035), 0.0120)
        take_frac = min(max(2.0 * stop_frac, 0.0060), 0.0250)

        if side == "buy":
            stop_price = round_price_to_tick(symbol, px * (1.0 - stop_frac), mode="down")
            take_price = round_price_to_tick(symbol, px * (1.0 + take_frac), mode="up")
        else:
            stop_price = round_price_to_tick(symbol, px * (1.0 + stop_frac), mode="up")
            take_price = round_price_to_tick(symbol, px * (1.0 - take_frac), mode="down")

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

    if executable:
        plan_reason = "FUTURES_CANARY_EXECUTABLE"
    elif not candidates:
        plan_reason = "NO_CURRENT_FUTURES_SIGNAL"
    else:
        plan_reason = "SIGNAL_EXISTS_BUT_NOT_EXECUTABLE"

    return {
        "ready": bool(executable),
        "reason": plan_reason,
        "candidate": executable[0] if executable else None,
        "alternatives": executable[1:],
        "rejected_candidates": rejected,
        "public_scan": scan,
        "readiness": r,
        "policy_target": {
            "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
            "max_order_notional_usd": MAX_NOTIONAL_USD,
            "max_open_positions": MAX_OPEN_POSITIONS,
        },
        "actual_order_submitted": False,
    }


def rescue_existing_position() -> dict[str, Any]:
    save_policy({
        "live_execution": True,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "allowed_roots": ["*"],
    })
    client = client_from_env()
    try:
        positions = position_map(client.open_positions())
        active = [(s, float(q)) for s, q in positions.items() if abs(float(q)) >= min_lot(s)]
        if not active:
            return {"ok": True, "reason": "NO_EXISTING_POSITION", "actual_order_submitted": False}
        if len(active) != 1:
            return {
                "ok": False,
                "reason": "MULTIPLE_EXISTING_POSITIONS",
                "positions": active,
                "actual_order_submitted": False,
            }

        symbol, signed_size = active[0]
        side = "sell" if signed_size > 0 else "buy"
        size = round_size_down(symbol, abs(signed_size))
        px = _ticker_mid(client, symbol)
        try:
            client.cancel_all_orders()
        except Exception:
            pass

        stop_frac = 0.005
        take_frac = 0.010
        if signed_size > 0:
            stop_price = round_price_to_tick(symbol, px * (1.0 - stop_frac), mode="down")
            take_price = round_price_to_tick(symbol, px * (1.0 + take_frac), mode="up")
        else:
            stop_price = round_price_to_tick(symbol, px * (1.0 + stop_frac), mode="up")
            take_price = round_price_to_tick(symbol, px * (1.0 - take_frac), mode="down")

        stop = place_order(
            symbol, side, size, reduce_only=True, order_type="stp",
            stop_price=stop_price, trigger_signal="mark",
            cli_ord_id=f"rs{int(time.time() * 1000)}",
        )
        take = place_order(
            symbol, side, size, reduce_only=True, order_type="take_profit",
            stop_price=take_price, trigger_signal="mark",
            cli_ord_id=f"rt{int(time.time() * 1000)}",
        )
        if stop.get("submitted_live") and take.get("submitted_live"):
            return {
                "ok": True,
                "reason": "EXISTING_POSITION_PROTECTED",
                "symbol": symbol,
                "size": size,
                "stop_price": stop_price,
                "take_profit_price": take_price,
                "stop": stop,
                "take_profit": take,
                "actual_order_submitted": True,
            }

        try:
            client.cancel_all_orders()
        except Exception:
            pass
        flat = place_order(
            symbol, side, size, reduce_only=True, order_type="mkt",
            cli_ord_id=f"rf{int(time.time() * 1000)}",
        )
        return {
            "ok": bool(flat.get("submitted_live")),
            "reason": "EXISTING_POSITION_FLATTENED" if flat.get("submitted_live") else "RESCUE_FLATTEN_FAILED",
            "symbol": symbol,
            "stop": stop,
            "take_profit": take,
            "flatten": flat,
            "actual_order_submitted": bool(flat.get("submitted_live")),
        }
    finally:
        save_policy({"live_execution": False})


def execute() -> dict[str, Any]:
    # Keep policy disarmed during planning. It is armed only for the few calls
    # needed to establish the canary and protective reduce-only orders.
    save_policy({
        "live_execution": False,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "allowed_roots": ["*"],
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
            cli_ord_id=f"ce{int(time.time() * 1000)}",
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
            cli_ord_id=f"cs{int(time.time() * 1000)}",
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
            cli_ord_id=f"ct{int(time.time() * 1000)}",
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
                    cli_ord_id=f"cf{int(time.time() * 1000)}",
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
    ap.add_argument("--rescue", action="store_true")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args()

    if args.rescue:
        print(json.dumps(rescue_existing_position(), indent=2, ensure_ascii=False, default=str))
        return
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

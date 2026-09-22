from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from futures_scale_gate import evidence as scale_evidence, scale_multiplier as evidence_scale_multiplier

from futures_private import (
    client_from_env,
    contract_size,
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

INVERT_DIRECTION = False

MAX_UNIVERSE = 28
UNIVERSE_PREFILTER = 48
MAX_UNIVERSE_SPREAD_BPS = 20.0
PUBLIC_SCAN_CACHE_SEC = 20
BREADTH_STRONG_THRESHOLD = 0.35
EVENT_LOG = Path("data/futures_canary_events.jsonl")

# Tier-1 Futures taker fee 5 bps/side + conservative 2 bps slippage
# + 3 bps execution buffer/side = 20 bps modeled round-trip.
TAKER_FEE_BPS_PER_SIDE = 5.0
SLIPPAGE_BPS_PER_SIDE = 2.0
EXEC_BUFFER_BPS_PER_SIDE = 3.0
ROUND_TRIP_TAKER_COST_BPS = 2.0 * (
    TAKER_FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE + EXEC_BUFFER_BPS_PER_SIDE
)
MIN_TAKER_NET_EDGE_BPS = 25.0
MIN_VOLUME_RATIO = 0.60
BREAKOUT_VOLUME_RATIO = 1.00
MAX_EMA6_DISTANCE_ATR = 1.25
VOL_TARGET_ATR_BPS = 20.0
MIN_TARGET_NOTIONAL_USD = 2.0
MICRO_MIN_TRADES = 8
MICRO_VETO_TRADES = 12
MICRO_OPPOSING_FLOW_VETO = -0.18
MICRO_ALIGNED_FLOW_CONFIRM = 0.05
MICRO_FLOW_MAX_AGE_SEC = 120
LIVE_MIN_QUALITY_SCORE = 68.0
BACKUP_TAKE_PROFIT_BPS = 300.0
HARD_STOP_BPS = 45.0
TARGET_NOTIONAL_USD = 5.0
MAX_NOTIONAL_USD = 10.0
MAX_NOTIONAL_PCT_EQUITY = 35.0
MAX_OPEN_POSITIONS = 4
MAX_PORTFOLIO_NOTIONAL_USD = 20.0
MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY = 95.0
CHARTS = "https://futures.kraken.com/api/charts/v1"
FUTURES_HISTORY = "https://futures.kraken.com/derivatives/api/v3/history"

_SCAN_CACHE_TS = 0.0
_SCAN_CACHE: dict[str, Any] | None = None


def execution_signal_side(base_side: str) -> str:
    side = str(base_side or "").upper()
    if side not in {"LONG", "SHORT"}:
        return side
    if not INVERT_DIRECTION:
        return side
    return "SHORT" if side == "LONG" else "LONG"


def _timestamp_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        x = float(value)
        if x > 1e12:
            return int(x)
        if x > 1e9:
            return int(x * 1000)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _completed_candles(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "time" not in df.columns:
        return df.copy()
    last_ts = _timestamp_ms(df["time"].iloc[-1])
    if last_ts is None:
        return df.copy()
    if int(time.time() * 1000) - last_ts < 60_000:
        return df.iloc[:-1].reset_index(drop=True)
    return df.reset_index(drop=True)


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
    df = df.dropna().reset_index(drop=True)
    return _completed_candles(df)


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
    median_range = float((df["high"] - df["low"]).tail(30).median())
    median_range_bps = median_range / last * 10000.0 if last > 0 else 0.0
    realized = float(close.pct_change().dropna().tail(60).std(ddof=0))
    realized_bps = realized * 10000.0 if math.isfinite(realized) else 0.0
    volatility_score = (
        atr_bps * 0.55
        + median_range_bps * 0.30
        + realized_bps * 0.15
    )

    vol_med = float(df["volume"].tail(30).median())
    vol_ratio = float(df["volume"].iloc[-1] / vol_med) if vol_med > 0 else 1.0

    up = last > ema6 > ema20 and r5 > 0 and r15 > 0
    down = last < ema6 < ema20 and r5 < 0 and r15 < 0
    side = "LONG" if up else "SHORT" if down else "NONE"

    prior_high_20 = float(df["high"].iloc[-21:-1].max())
    prior_low_20 = float(df["low"].iloc[-21:-1].min())
    breakout = (
        (side == "LONG" and last >= prior_high_20)
        or (side == "SHORT" and last <= prior_low_20)
    )
    ema6_distance_bps = abs(last / ema6 - 1.0) * 10000.0 if ema6 > 0 else 9999.0
    ema6_distance_atr = ema6_distance_bps / max(atr_bps, 1e-9)
    trend_persistent = (
        (side == "LONG" and r30 > 0 and r60 > 0)
        or (side == "SHORT" and r30 < 0 and r60 < 0)
    )
    continuation_ok = bool(
        breakout and vol_ratio >= BREAKOUT_VOLUME_RATIO
    ) or bool(
        ema6_distance_atr <= MAX_EMA6_DISTANCE_ATR
    )
    regime = (
        "BREAKOUT" if breakout and vol_ratio >= BREAKOUT_VOLUME_RATIO
        else "TREND_CONTINUATION" if side != "NONE" and continuation_ok
        else "OVEREXTENDED" if side != "NONE"
        else "NO_TREND"
    )

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
    if vol_ratio >= MIN_VOLUME_RATIO:
        confirmations += 1

    confidence = min(0.95, 0.45 + 0.10 * confirmations + min(expected_bps / 500.0, 0.15))
    ready = (
        side in {"LONG", "SHORT"}
        and confirmations >= 3
        and confidence >= 0.64
        and trend_persistent
        and continuation_ok
        and vol_ratio >= MIN_VOLUME_RATIO
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
        "median_range_bps": round(median_range_bps, 3),
        "realized_vol_bps": round(realized_bps, 3),
        "volatility_score": round(volatility_score, 3),
        "volume_ratio": round(vol_ratio, 3),
        "min_volume_ratio": MIN_VOLUME_RATIO,
        "breakout_volume_ratio": BREAKOUT_VOLUME_RATIO,
        "prior_high_20": prior_high_20,
        "prior_low_20": prior_low_20,
        "breakout": bool(breakout),
        "ema6_distance_bps": round(ema6_distance_bps, 3),
        "ema6_distance_atr": round(ema6_distance_atr, 3),
        "max_ema6_distance_atr": MAX_EMA6_DISTANCE_ATR,
        "trend_persistent": bool(trend_persistent),
        "continuation_ok": bool(continuation_ok),
        "regime": regime,
        "expected_move_proxy_bps": round(expected_bps, 3),
        "taker_round_trip_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
        "taker_net_edge_bps": round(net_taker_bps, 3),
        "canary_signal_ready": bool(ready),
    }


def _dynamic_universe(max_symbols: int = UNIVERSE_PREFILTER) -> list[str]:
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
        if not symbol.startswith("PF_") or not symbol.endswith("USD") or not bool(spec.get("tradeable")):
            continue

        try:
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
        except Exception:
            continue
        if bid <= 0 or ask < bid:
            continue

        mid = (bid + ask) / 2.0
        spread_bps = ((ask - bid) / mid * 10000.0) if mid > 0 else 9999.0
        if spread_bps > MAX_UNIVERSE_SPREAD_BPS:
            continue

        try:
            min_notional = (
                float(spec.get("qty_step") or 0.0)
                * mid
                * float(spec.get("contract_size") or 1.0)
            )
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

        # Cheap stage: keep only liquid/tight markets. Candle volatility is
        # ranked later from the same data already fetched for the signal.
        score = liquidity / max(1.0, 1.0 + spread_bps)
        ranked.append((score, symbol))

    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[:max_symbols]]



def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, float(value)))


def _market_breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trend_rows = [
        x for x in rows
        if str(x.get("side") or "") in {"LONG", "SHORT"} and bool(x.get("trend_persistent"))
    ]
    longs = sum(1 for x in trend_rows if x.get("side") == "LONG")
    shorts = sum(1 for x in trend_rows if x.get("side") == "SHORT")
    total = longs + shorts
    breadth = (longs - shorts) / total if total else 0.0
    if breadth >= BREADTH_STRONG_THRESHOLD:
        regime = "BULL_BREADTH"
    elif breadth <= -BREADTH_STRONG_THRESHOLD:
        regime = "BEAR_BREADTH"
    else:
        regime = "TWO_SIDED"
    return {
        "long_trend_count": longs,
        "short_trend_count": shorts,
        "trend_count": total,
        "breadth_score": round(breadth, 4),
        "regime": regime,
    }


def _breadth_alignment(side: str, breadth: dict[str, Any]) -> int:
    regime = str(breadth.get("regime") or "TWO_SIDED")
    side = str(side).upper()
    if regime == "BULL_BREADTH":
        return 1 if side == "LONG" else -1
    if regime == "BEAR_BREADTH":
        return 1 if side == "SHORT" else -1
    return 0


def _recent_trade_flow(symbol: str) -> dict[str, Any]:
    """Fresh taker-flow proxy using only recent Kraken Futures trades."""
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - MICRO_FLOW_MAX_AGE_SEC * 1000
    try:
        r = requests.get(FUTURES_HISTORY, params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        rows = r.json().get("history") or []
    except Exception as exc:
        return {
            "available": False,
            "fresh": False,
            "symbol": symbol,
            "trade_count": 0,
            "flow_imbalance": 0.0,
            "max_age_sec": MICRO_FLOW_MAX_AGE_SEC,
            "error": f"{type(exc).__name__}: {exc}",
        }

    buy = 0.0
    sell = 0.0
    count = 0
    newest_ms: int | None = None
    oldest_ms: int | None = None
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type") or "fill").lower()
        if typ in {"block", "assignment", "termination"}:
            continue
        ts_ms = _timestamp_ms(row.get("time"))
        if ts_ms is None or ts_ms < cutoff_ms:
            continue
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            continue
        try:
            px = float(row.get("price") or 0.0)
            size = float(row.get("size") or 0.0)
        except Exception:
            continue
        weight = abs(px * size)
        if weight <= 0:
            continue
        newest_ms = ts_ms if newest_ms is None else max(newest_ms, ts_ms)
        oldest_ms = ts_ms if oldest_ms is None else min(oldest_ms, ts_ms)
        if side == "buy":
            buy += weight
        else:
            sell += weight
        count += 1

    total = buy + sell
    flow = (buy - sell) / total if total > 0 else 0.0
    newest_age_sec = (now_ms - newest_ms) / 1000.0 if newest_ms is not None else None
    fresh = bool(count >= MICRO_MIN_TRADES and newest_age_sec is not None and newest_age_sec <= MICRO_FLOW_MAX_AGE_SEC)
    return {
        "available": bool(count),
        "fresh": fresh,
        "symbol": symbol,
        "trade_count": count,
        "buy_notional_proxy": round(buy, 6),
        "sell_notional_proxy": round(sell, 6),
        "flow_imbalance": round(flow, 4),
        "newest_age_sec": round(newest_age_sec, 3) if newest_age_sec is not None else None,
        "window_span_sec": round((newest_ms - oldest_ms) / 1000.0, 3) if newest_ms is not None and oldest_ms is not None else None,
        "max_age_sec": MICRO_FLOW_MAX_AGE_SEC,
    }


def _quality_profile(signal: dict[str, Any], micro: dict[str, Any]) -> dict[str, Any]:
    side = str(signal.get("side") or "").upper()
    direction = 1.0 if side == "LONG" else -1.0
    flow = float(micro.get("flow_imbalance") or 0.0)
    aligned_flow = direction * flow
    trade_count = int(micro.get("trade_count") or 0)
    flow_fresh = bool(micro.get("fresh"))

    micro_veto = bool(
        flow_fresh
        and trade_count >= MICRO_VETO_TRADES
        and aligned_flow <= MICRO_OPPOSING_FLOW_VETO
    )
    micro_confirmed = bool(
        flow_fresh
        and trade_count >= MICRO_MIN_TRADES
        and aligned_flow >= MICRO_ALIGNED_FLOW_CONFIRM
    )

    confidence = _clamp(float(signal.get("confidence") or 0.0), 0.0, 1.0)
    edge = _clamp((float(signal.get("taker_net_edge_bps") or 0.0) - MIN_TAKER_NET_EDGE_BPS) / 75.0, 0.0, 1.0)
    volume = _clamp((float(signal.get("volume_ratio") or 0.0) - MIN_VOLUME_RATIO) / 1.40, 0.0, 1.0)
    structure = 1.0 if bool(signal.get("breakout")) else 0.72 if bool(signal.get("continuation_ok")) else 0.0
    flow_score = _clamp((aligned_flow + 0.50) / 1.00, 0.0, 1.0) if flow_fresh and trade_count >= MICRO_MIN_TRADES else 0.50
    breadth_alignment = int(signal.get("breadth_alignment") or 0)
    breadth_score = 0.65 if breadth_alignment > 0 else 0.35 if breadth_alignment < 0 else 0.50

    quality = 100.0 * (
        0.30 * confidence
        + 0.25 * edge
        + 0.15 * volume
        + 0.15 * structure
        + 0.10 * flow_score
        + 0.05 * breadth_score
    )
    if micro_veto:
        quality = min(quality, 49.0)

    if quality >= 80.0 and micro_confirmed:
        tier = "ELITE"
    elif quality >= 68.0 and not micro_veto:
        tier = "STRONG"
    else:
        tier = "BASE"

    return {
        "quality_score": round(quality, 2),
        "quality_tier": tier,
        "microstructure_ok": not micro_veto,
        "microstructure_confirmed": micro_confirmed,
        "microstructure_fresh": flow_fresh,
        "aligned_flow": round(aligned_flow, 4),
        "micro": micro,
    }


def _log(event: dict[str, Any]) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts_ms": int(time.time() * 1000), **event}
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def public_scan(force_refresh: bool = False) -> dict[str, Any]:
    global _SCAN_CACHE_TS, _SCAN_CACHE
    now = time.time()
    if (
        not force_refresh
        and _SCAN_CACHE is not None
        and now - _SCAN_CACHE_TS < PUBLIC_SCAN_CACHE_SEC
    ):
        cached = dict(_SCAN_CACHE)
        cached["cache_age_sec"] = round(now - _SCAN_CACHE_TS, 3)
        cached["from_cache"] = True
        return cached

    prefilter_symbols = _dynamic_universe(UNIVERSE_PREFILTER)
    scanned: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(prefilter_symbols)))) as pool:
        futs = {pool.submit(_signal, symbol): symbol for symbol in prefilter_symbols}
        for fut in as_completed(futs):
            symbol = futs[fut]
            try:
                scanned.append(fut.result())
            except Exception as exc:
                scanned.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

    valid = [x for x in scanned if not x.get("error")]
    breadth = _market_breadth(valid)

    valid.sort(
        key=lambda x: float(x.get("volatility_score") or 0.0),
        reverse=True,
    )
    rows = valid[:MAX_UNIVERSE]
    symbols = [str(x.get("symbol") or "") for x in rows]

    for row in rows:
        align = _breadth_alignment(str(row.get("side") or ""), breadth)
        row["breadth_alignment"] = align
        row["market_regime"] = breadth["regime"]
        row["market_breadth_score"] = breadth["breadth_score"]
        row["ranking_edge_bps"] = round(
            float(row.get("taker_net_edge_bps") or 0.0) + 8.0 * align,
            3,
        )

    rows.sort(
        key=lambda x: (
            bool(x.get("canary_signal_ready")),
            float(x.get("ranking_edge_bps") or -999.0),
            float(x.get("confidence") or 0.0),
        ),
        reverse=True,
    )
    ready = [x for x in rows if x.get("canary_signal_ready")]
    result = {
        "ready": bool(ready),
        "reason": "FUTURES_CANARY_SIGNAL_READY" if ready else "NO_POSITIVE_FUTURES_CANARY",
        "candidate": ready[0] if ready else (rows[0] if rows else None),
        "ready_count": len(ready),
        "universe_count": len(symbols),
        "prefilter_count": len(prefilter_symbols),
        "symbols": symbols,
        "all": rows,
        "market_breadth": breadth,
        "round_trip_taker_cost_bps": ROUND_TRIP_TAKER_COST_BPS,
        "min_net_edge_bps": MIN_TAKER_NET_EDGE_BPS,
        "actual_order_submitted": False,
        "from_cache": False,
        "cache_age_sec": 0.0,
        "note": (
            "Broad Kraken PF_*USD discovery, then deep ranking by volatility, "
            "trend persistence, breakout/anti-chase, breadth and execution gates."
        ),
    }
    _SCAN_CACHE = result
    _SCAN_CACHE_TS = now
    return dict(result)


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


def _symbols_in_open_orders(payload: Any) -> set[str]:
    out: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in ("symbol", "tradeable"):
                value = obj.get(key)
                if value:
                    out.add(str(value).upper())
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(payload)
    return out


def _position_size(client: Any, symbol: str) -> float:
    return float(position_map(client.open_positions()).get(symbol.upper(), 0.0))


def _target_notional_for_signal(
    signal: dict[str, Any],
    minimum_notional: float,
    quality: dict[str, Any] | None = None,
) -> float:
    """Scale only proven edge; volatility and signal quality can only reduce base risk."""
    try:
        atr_bps = max(float(signal.get("atr_bps") or 0.0), 1e-9)
    except Exception:
        atr_bps = VOL_TARGET_ATR_BPS
    vol_scale = min(1.0, max(0.40, VOL_TARGET_ATR_BPS / atr_bps))

    q = quality or {}
    qscore = float(q.get("quality_score") or 0.0)
    qtier = str(q.get("quality_tier") or "BASE").upper()
    if qtier == "ELITE" and qscore >= 80.0:
        quality_scale = 1.0
    elif qtier == "STRONG" and qscore >= LIVE_MIN_QUALITY_SCORE:
        quality_scale = 0.85
    else:
        quality_scale = 0.70

    evidence_scale = evidence_scale_multiplier()
    target = TARGET_NOTIONAL_USD * vol_scale * quality_scale * evidence_scale
    target = max(MIN_TARGET_NOTIONAL_USD, target, float(minimum_notional))
    return min(MAX_NOTIONAL_USD, target)


def private_plan() -> dict[str, Any]:
    # Keep the read-only planning policy aligned with the canary constants.
    # This never arms live execution; it only synchronizes risk caps used by
    # order_preflight so plan and execution evaluate the same limits.
    save_policy({
        "live_execution": False,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
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
    open_count = int(r.get("open_position_count") or 0)
    if open_count >= MAX_OPEN_POSITIONS:
        return {
            "ready": False,
            "reason": "POSITION_SLOTS_FULL",
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
    open_symbols = set(position_map(client.open_positions()).keys())
    order_symbols = _symbols_in_open_orders(r.get("open_orders") or {})
    stale_order_symbols = order_symbols - open_symbols
    blocked_symbols = open_symbols | stale_order_symbols
    candidates = [x for x in candidates if str(x.get("symbol") or "").upper() not in blocked_symbols]
    executable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for p in candidates:
        symbol = str(p["symbol"]).upper()
        base_signal_side = str(p.get("side") or "").upper()
        execution_signal = execution_signal_side(base_signal_side)

        # Keep the historical qualifier intact, then trade the opposite side.
        # This makes the new mode a clean anti-signal experiment rather than
        # silently changing both selection and direction at once.
        micro = _recent_trade_flow(symbol)
        quality = _quality_profile(p, micro)
        if not quality.get("microstructure_ok"):
            rejected.append({
                "symbol": symbol,
                "reason": "MICROSTRUCTURE_OPPOSES_BASE_SIGNAL",
                "signal": p,
                "quality": quality,
                "base_signal_side": base_signal_side,
                "execution_signal_side": execution_signal,
                "direction_inverted": INVERT_DIRECTION,
            })
            continue
        px = _ticker_mid(client, symbol)
        csize = contract_size(symbol)
        minimum = min_lot(symbol)
        minimum_notional = minimum * px * csize
        desired_notional = _target_notional_for_signal(p, minimum_notional, quality)
        raw_size = desired_notional / (px * csize)
        size = round_size_down(symbol, raw_size)
        if size < minimum and minimum_notional <= MAX_NOTIONAL_USD + 1e-9:
            size = minimum
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
                "minimum_lot_notional_usd": minimum * px * csize,
                "signal_net_edge_bps": p.get("taker_net_edge_bps"),
            })
            continue
        side = "buy" if execution_signal == "LONG" else "sell"
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
                "estimated_notional_usd": size * px * csize,
                "min_lot": minimum,
                "signal_net_edge_bps": p.get("taker_net_edge_bps"),
            })
            continue

        stop_frac = HARD_STOP_BPS / 10000.0
        # Fixed loss distance stays unchanged while the profit side is allowed
        # to run under the ratcheting trailing manager.
        take_frac = BACKUP_TAKE_PROFIT_BPS / 10000.0

        if side == "buy":
            stop_price = round_price_to_tick(symbol, px * (1.0 - stop_frac), mode="down")
            take_price = round_price_to_tick(symbol, px * (1.0 + take_frac), mode="up")
        else:
            stop_price = round_price_to_tick(symbol, px * (1.0 + stop_frac), mode="up")
            take_price = round_price_to_tick(symbol, px * (1.0 - take_frac), mode="down")

        _validate_protection_prices(symbol, side, px, stop_price, take_price)

        executable.append({
            "symbol": symbol,
            "side": side,
            "size": size,
            "mid_price": px,
            "estimated_notional_usd": size * px * csize,
            "target_notional_usd": desired_notional,
            "volatility_sizing_scale": round(desired_notional / TARGET_NOTIONAL_USD, 4),
            "evidence_scale": evidence_scale_multiplier(),
            "scale_evidence": scale_evidence(),
            "equity_usd": equity,
            "notional_cap_usd": notional_cap,
            "stop_price": stop_price,
            "take_profit_price": take_price,
            "stop_distance_pct": stop_frac * 100.0,
            "take_profit_distance_pct": take_frac * 100.0,
            "taker_net_edge_bps": p.get("taker_net_edge_bps"),
            "confidence": p.get("confidence"),
            "base_signal_side": base_signal_side,
            "execution_signal_side": execution_signal,
            "direction_inverted": INVERT_DIRECTION,
            "source_signal": {
                **p,
                "base_side": base_signal_side,
                "execution_side": execution_signal,
                "direction_inverted": INVERT_DIRECTION,
            },
            "quality_score": quality["quality_score"],
            "quality_tier": quality["quality_tier"],
            "microstructure": quality,
            "preflight": pre,
        })

    executable.sort(
        key=lambda x: (
            float(x.get("quality_score") or 0.0),
            float(x["taker_net_edge_bps"]),
            float(x["confidence"]),
        ),
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
        "open_symbols": sorted(open_symbols),
        "stale_order_symbols": sorted(stale_order_symbols),
        "public_scan": scan,
        "readiness": r,
        "policy_target": {
            "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
            "max_order_notional_usd": MAX_NOTIONAL_USD,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
            "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        },
        "actual_order_submitted": False,
    }



def plan_specific_candidate(
    symbol: str,
    side: str,
    source_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only executable plan for a specific pair leg."""
    symbol = str(symbol).upper()
    side = str(side).lower()
    if side not in {"buy", "sell"}:
        return {"ready": False, "reason": "INVALID_SIDE", "actual_order_submitted": False}

    save_policy({
        "live_execution": False,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        "allowed_roots": ["*"],
    })
    r = readiness()
    if not r.get("safe_to_arm"):
        return {"ready": False, "reason": "FUTURES_ACCOUNT_NOT_READY", "readiness": r, "actual_order_submitted": False}
    if int(r.get("open_position_count") or 0) >= MAX_OPEN_POSITIONS:
        return {"ready": False, "reason": "POSITION_SLOTS_FULL", "readiness": r, "actual_order_submitted": False}

    equity = float(r.get("equity_usd") or 0.0)
    if equity <= 0:
        return {"ready": False, "reason": "NO_FUTURES_EQUITY", "readiness": r, "actual_order_submitted": False}

    client = client_from_env()
    open_symbols = set(position_map(client.open_positions()).keys())
    order_symbols = _symbols_in_open_orders(r.get("open_orders") or {})
    blocked_symbols = open_symbols | (order_symbols - open_symbols)
    if symbol in blocked_symbols:
        return {
            "ready": False,
            "reason": "SYMBOL_ALREADY_OPEN_OR_BLOCKED",
            "symbol": symbol,
            "readiness": r,
            "actual_order_submitted": False,
        }

    px = _ticker_mid(client, symbol)
    csize = contract_size(symbol)
    minimum = min_lot(symbol)
    minimum_notional = minimum * px * csize
    desired_notional = min(MAX_NOTIONAL_USD, max(TARGET_NOTIONAL_USD, minimum_notional))
    raw_size = desired_notional / (px * csize)
    size = round_size_down(symbol, raw_size)
    if size < minimum and minimum_notional <= MAX_NOTIONAL_USD + 1e-9:
        size = minimum
    if size < minimum:
        return {
            "ready": False,
            "reason": "BELOW_MIN_LOT_AFTER_CAP",
            "symbol": symbol,
            "price": px,
            "min_lot": minimum,
            "minimum_lot_notional_usd": minimum_notional,
            "actual_order_submitted": False,
        }

    try:
        pre = order_preflight(symbol, side, size, reduce_only=False, client=client)
    except Exception as exc:
        return {
            "ready": False,
            "reason": "PREFLIGHT_REJECTED",
            "symbol": symbol,
            "error": f"{type(exc).__name__}: {exc}",
            "actual_order_submitted": False,
        }

    stop_frac = HARD_STOP_BPS / 10000.0
    take_frac = BACKUP_TAKE_PROFIT_BPS / 10000.0
    if side == "buy":
        stop_price = round_price_to_tick(symbol, px * (1.0 - stop_frac), mode="down")
        take_price = round_price_to_tick(symbol, px * (1.0 + take_frac), mode="up")
    else:
        stop_price = round_price_to_tick(symbol, px * (1.0 + stop_frac), mode="up")
        take_price = round_price_to_tick(symbol, px * (1.0 - take_frac), mode="down")

    _validate_protection_prices(symbol, side, px, stop_price, take_price)

    candidate = {
        "symbol": symbol,
        "side": side,
        "size": size,
        "mid_price": px,
        "estimated_notional_usd": size * px * csize,
        "equity_usd": equity,
        "notional_cap_usd": min(MAX_NOTIONAL_USD, equity * MAX_NOTIONAL_PCT_EQUITY / 100.0),
        "stop_price": stop_price,
        "take_profit_price": take_price,
        "stop_distance_pct": stop_frac * 100.0,
        "take_profit_distance_pct": take_frac * 100.0,
        "taker_net_edge_bps": float((source_signal or {}).get("score") or 0.0),
        "confidence": None,
        "source_signal": source_signal or {"mode": "specific_pair_leg"},
        "preflight": pre,
    }
    return {
        "ready": True,
        "reason": "FUTURES_SPECIFIC_EXECUTABLE",
        "candidate": candidate,
        "readiness": r,
        "actual_order_submitted": False,
    }


def _validate_protection_prices(
    symbol: str,
    side: str,
    entry_price: float,
    stop_price: float,
    take_price: float,
) -> None:
    side = str(side).lower()
    entry = float(entry_price)
    stop = float(stop_price)
    take = float(take_price)
    if entry <= 0 or stop <= 0 or take <= 0:
        raise RuntimeError(
            f"Invalid protection price for {symbol}: entry={entry}, stop={stop}, take={take}"
        )
    if side == "buy":
        if not (stop < entry < take):
            raise RuntimeError(
                f"Invalid LONG protection geometry for {symbol}: stop={stop}, entry={entry}, take={take}"
            )
    elif side == "sell":
        if not (take < entry < stop):
            raise RuntimeError(
                f"Invalid SHORT protection geometry for {symbol}: take={take}, entry={entry}, stop={stop}"
            )
    else:
        raise RuntimeError(f"Invalid side for protection validation: {side}")


def _exit_limit_price(symbol: str, exit_side: str, trigger_price: float) -> float:
    # Kraken Futures stop/take-profit examples use both stopPrice and limitPrice.
    # Give the triggered reduce-only limit 10 bps of execution room.
    if exit_side == "sell":
        return round_price_to_tick(symbol, trigger_price * 0.999, mode="down")
    return round_price_to_tick(symbol, trigger_price * 1.001, mode="up")


def rescue_existing_position() -> dict[str, Any]:
    save_policy({
        "live_execution": True,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        "allowed_roots": ["*"],
    })
    client = client_from_env()
    try:
        positions = position_map(client.open_positions())
        active = [(s, float(q)) for s, q in positions.items() if abs(float(q)) >= min_lot(s)]
        if not active:
            return {"ok": True, "reason": "NO_EXISTING_POSITION", "actual_order_submitted": False}
        if len(active) > MAX_OPEN_POSITIONS:
            return {
                "ok": False,
                "reason": "TOO_MANY_EXISTING_POSITIONS",
                "positions": active,
                "actual_order_submitted": False,
            }
        if len(active) > 1:
            return {
                "ok": True,
                "reason": "EXISTING_PORTFOLIO_PRESERVED",
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

        stop_frac = HARD_STOP_BPS / 10000.0
        take_frac = BACKUP_TAKE_PROFIT_BPS / 10000.0
        if signed_size > 0:
            stop_price = round_price_to_tick(symbol, px * (1.0 - stop_frac), mode="down")
            take_price = round_price_to_tick(symbol, px * (1.0 + take_frac), mode="up")
        else:
            stop_price = round_price_to_tick(symbol, px * (1.0 + stop_frac), mode="up")
            take_price = round_price_to_tick(symbol, px * (1.0 - take_frac), mode="down")

        stop = place_order(
            symbol, side, size, reduce_only=True, order_type="stp",
            stop_price=stop_price,
            limit_price=_exit_limit_price(symbol, side, stop_price),
            trigger_signal="mark",
            cli_ord_id=f"rs{int(time.time() * 1000)}",
        )
        take = place_order(
            symbol, side, size, reduce_only=True, order_type="take_profit",
            stop_price=take_price,
            limit_price=_exit_limit_price(symbol, side, take_price),
            trigger_signal="mark",
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



def execute_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    save_policy({
        "live_execution": False,
        "max_order_notional_pct_equity": MAX_NOTIONAL_PCT_EQUITY,
        "max_order_notional_usd": MAX_NOTIONAL_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_portfolio_notional_usd": MAX_PORTFOLIO_NOTIONAL_USD,
        "max_portfolio_notional_pct_equity": MAX_PORTFOLIO_NOTIONAL_PCT_EQUITY,
        "allowed_roots": ["*"],
    })

    candidate = dict(candidate)
    symbol = str(candidate["symbol"]).upper()
    side = str(candidate["side"]).lower()
    size = float(candidate["size"])
    _validate_protection_prices(
        symbol,
        side,
        float(candidate["mid_price"]),
        float(candidate["stop_price"]),
        float(candidate["take_profit_price"]),
    )
    exit_side = "sell" if side == "buy" else "buy"
    client = client_from_env()

    entry: dict[str, Any] | None = None
    stop: dict[str, Any] | None = None
    take: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None

    try:
        try:
            client.deadman(0)
        except Exception:
            pass

        # Re-run account-level preflight immediately before the live send.
        order_preflight(symbol, side, size, reduce_only=False, client=client)

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

        stop_trigger = float(candidate["stop_price"])
        stop = place_order(
            symbol, exit_side, protected_size, reduce_only=True, order_type="stp",
            stop_price=stop_trigger,
            limit_price=_exit_limit_price(symbol, exit_side, stop_trigger),
            trigger_signal="mark",
            cli_ord_id=f"cs{int(time.time() * 1000)}",
            use_deadman=False,
        )
        if not stop.get("submitted_live"):
            raise RuntimeError(f"Stop order not submitted: {stop}")

        take_trigger = float(candidate["take_profit_price"])
        take = place_order(
            symbol, exit_side, protected_size, reduce_only=True, order_type="take_profit",
            stop_price=take_trigger,
            limit_price=_exit_limit_price(symbol, exit_side, take_trigger),
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
        try:
            try:
                from futures_private import cancel_symbol_orders
                cancel_symbol_orders(client, symbol, reduce_only_only=True)
            except Exception:
                pass
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


def execute() -> dict[str, Any]:
    plan = private_plan()
    if not plan.get("ready"):
        return {**plan, "actual_order_submitted": False}
    return execute_candidate(dict(plan["candidate"]))

def live_status() -> dict[str, Any]:
    client = client_from_env()
    r = readiness()
    pos_payload = client.open_positions()
    positions = position_map(pos_payload)
    orders = client.open_orders()

    events: list[dict[str, Any]] = []
    if EVENT_LOG.exists():
        try:
            lines = EVENT_LOG.read_text(encoding="utf-8").splitlines()
            for line in lines[-100:]:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        events.append(row)
                except Exception:
                    continue
        except Exception:
            pass

    submitted = [e for e in events if e.get("actual_order_submitted")]
    protected = [e for e in events if e.get("reason") == "FUTURES_CANARY_LIVE_WITH_PROTECTION"]
    aborted = [e for e in events if e.get("reason") == "FUTURES_CANARY_ABORTED"]

    return {
        "ok": True,
        "equity_usd": r.get("equity_usd"),
        "open_position_count": len(positions),
        "positions": positions,
        "open_orders": orders,
        "policy": r.get("policy"),
        "live_execution": r.get("live_execution"),
        "event_log": {
            "rows": len(events),
            "submitted_live_events": len(submitted),
            "protected_live_events": len(protected),
            "aborted_live_events": len(aborted),
            "last_events": events[-10:],
        },
    }


def selftest() -> dict[str, Any]:
    breadth = _market_breadth([
        {"side": "LONG", "trend_persistent": True},
        {"side": "LONG", "trend_persistent": True},
        {"side": "LONG", "trend_persistent": True},
        {"side": "SHORT", "trend_persistent": True},
    ])
    signal = {
        "side": "LONG",
        "confidence": 0.90,
        "taker_net_edge_bps": 70.0,
        "volume_ratio": 1.4,
        "breakout": True,
        "continuation_ok": True,
        "breadth_alignment": 1,
    }
    aligned = _quality_profile(signal, {
        "available": True, "fresh": True, "trade_count": 30, "flow_imbalance": 0.40,
    })
    opposing = _quality_profile(signal, {
        "available": True, "fresh": True, "trade_count": 30, "flow_imbalance": -0.50,
    })
    low_vol_size = _target_notional_for_signal({"atr_bps": 12.0}, 0.1)
    high_vol_size = _target_notional_for_signal({"atr_bps": 50.0}, 0.1)
    checks = {
        "breadth_bull": breadth["regime"] == "BULL_BREADTH",
        "aligned_flow_passes": bool(aligned["microstructure_ok"]),
        "opposing_flow_vetoes": not bool(opposing["microstructure_ok"]),
        "quality_orders_flow": float(aligned["quality_score"]) > float(opposing["quality_score"]),
        "volatility_reduces_size": high_vol_size < low_vol_size,
        "broad_universe": UNIVERSE_PREFILTER > MAX_UNIVERSE >= 20,
        "scan_cache_positive": PUBLIC_SCAN_CACHE_SEC > 0,
        "flow_window_positive": MICRO_FLOW_MAX_AGE_SEC > 0,
        "live_quality_gate_positive": LIVE_MIN_QUALITY_SCORE >= 50.0,
        "inverse_long_to_short": execution_signal_side("LONG") == ("SHORT" if INVERT_DIRECTION else "LONG"),
        "inverse_short_to_long": execution_signal_side("SHORT") == ("LONG" if INVERT_DIRECTION else "SHORT"),
    }
    return {"ok": all(checks.values()), "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--rescue", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args()

    if args.selftest:
        print(json.dumps(selftest(), indent=2, ensure_ascii=False, default=str))
        return
    if args.status:
        print(json.dumps(live_status(), indent=2, ensure_ascii=False, default=str))
        return
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

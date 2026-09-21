from __future__ import annotations

import itertools
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from alpha_discovery import (
    AlphaRuntime,
    discover_models,
    ALPHA_COST_BPS,
    EXECUTION_MODE,
    VALIDATED_HORIZONS,
    SHADOW_HORIZONS,
)
from microstructure import KrakenMicroRecorder

KRAKEN = "https://api.kraken.com"
KRAKEN_FUTURES = "https://futures.kraken.com/api/charts/v1"
START_CAPITAL = float(os.getenv("START_CAPITAL", "5000"))
MAX_DD = float(os.getenv("MAX_DRAWDOWN_PCT", "10")) / 100.0
RISK_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.75")) / 100.0
SPOT_MAKER_FEE_BPS = float(os.getenv("KRAKEN_MAKER_BPS", "40"))
FUTURES_MAKER_FEE_BPS = float(os.getenv("KRAKEN_FUTURES_MAKER_BPS", "2"))
SLIPPAGE_BPS = float(os.getenv("SLIPPAGE_BPS", "2"))
EXECUTION_PENALTY_BPS = float(os.getenv("EXECUTION_PENALTY_BPS", "3"))
PULSE_MIN = float(os.getenv("PULSE_MIN", "0.64"))
UNIVERSE_MAX = int(os.getenv("UNIVERSE_MAX", "24"))
SCAN_WORKERS = int(os.getenv("SCAN_WORKERS", "4"))
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "XBTUSD,ETHUSD,SOLUSD").split(",") if s.strip()]
FUTURES_SYMBOLS = [s.strip() for s in os.getenv("FUTURES_SYMBOLS", "PF_XBTUSD,PF_ETHUSD,PF_SOLUSD,PF_XAUUSD,PF_XAGUSD,PF_WTIOILUSD,PF_AAPLXUSD,PF_GOOGLXUSD,PF_TSLAXUSD").split(",") if s.strip()]

ENGINE_BUILD = "4.1-cost-aware"
app = FastAPI(title="IMPULSE MAX 5K - Kraken Pulse Hunter", version=ENGINE_BUILD)
RECORDER = KrakenMicroRecorder()
ALPHA_RUNTIME = AlphaRuntime()
AUTO_RECORD = os.getenv("AUTO_RECORD", "1").lower() in {"1","true","yes","on"}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ImpulseMax5K/2.0"})


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    notional: float
    pnl: float
    costs: float
    reason: str
    equity: float


def kraken_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = SESSION.get(KRAKEN + path, params=params or {}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError("; ".join(data["error"]))
    return data["result"]


def asset_pairs() -> dict[str, dict[str, Any]]:
    return kraken_get("/0/public/AssetPairs")


def kraken_universe(max_pairs: int = UNIVERSE_MAX) -> list[str]:
    pairs = asset_pairs()
    eligible: dict[str, str] = {}
    for internal, meta in pairs.items():
        ws = meta.get("wsname") or ""
        alt = meta.get("altname") or ""
        status = meta.get("status", "online")
        if not ws or not alt or status != "online" or ".d" in alt.lower():
            continue
        parts = ws.split("/")
        if len(parts) != 2:
            continue
        base, quote = parts
        if quote not in {"USD", "EUR"}:
            continue
        if base in {"USD", "EUR", "USDT", "USDC", "DAI"}:
            continue
        eligible[internal] = alt

    ticker = kraken_get("/0/public/Ticker")
    ranked: list[tuple[float, str]] = []
    for internal, alt in eligible.items():
        d = ticker.get(internal)
        if not d:
            continue
        try:
            turnover = float(d["v"][1]) * float(d["p"][1])
        except Exception:
            turnover = 0.0
        if turnover > 0:
            ranked.append((turnover, alt))
    ranked.sort(reverse=True)
    return [alt for _, alt in ranked[:max_pairs]]


def klines(symbol: str, interval: int = 1, limit: int = 720, drop_live: bool = True) -> pd.DataFrame:
    result = kraken_get("/0/public/OHLC", {"pair": symbol, "interval": interval})
    key = next(k for k in result if k != "last")
    rows = result[key]
    if drop_live and rows:
        rows = rows[:-1]
    rows = rows[-limit:]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vwap", "volume", "count"])
    for col in ["open", "high", "low", "close", "vwap", "volume", "count"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"]), unit="s", utc=True)
    return df.dropna().sort_values("ts").reset_index(drop=True)


def features(df: pd.DataFrame, fast: int = 8, slow: int = 24, zwin: int = 30, atrn: int = 14) -> pd.DataFrame:
    x = df.copy()
    x["ef"] = x.close.ewm(span=fast, adjust=False).mean()
    x["es"] = x.close.ewm(span=slow, adjust=False).mean()
    x["ret"] = np.log(x.close / x.close.shift(1))
    x["mom"] = x.close.pct_change(fast)
    x["mom_slow"] = x.close.pct_change(slow)
    x["mu"] = x.close.rolling(zwin).mean()
    x["sd"] = x.close.rolling(zwin).std(ddof=0)
    x["z"] = (x.close - x.mu) / x.sd.replace(0, np.nan)
    prev = x.close.shift(1)
    tr = pd.concat([(x.high - x.low), (x.high - prev).abs(), (x.low - prev).abs()], axis=1).max(axis=1)
    x["atr"] = tr.rolling(atrn).mean()
    x["atr_med"] = x["atr"].rolling(60).median()
    medv = x.volume.rolling(30).median()
    x["rv"] = np.where(
        medv > 0,
        x.volume / medv,
        1.0,
    )
    x["rv"] = pd.Series(x["rv"], index=x.index).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    x["hh"] = x.high.shift(1).rolling(20).max()
    x["ll"] = x.low.shift(1).rolling(20).min()
    return x


def pulse_logic(row: pd.Series) -> dict[str, Any]:
    required = ("close", "ef", "es", "mom", "mom_slow", "z", "rv", "atr", "atr_med", "hh", "ll")
    if any(pd.isna(row.get(k)) for k in required):
        return {
            "pulse": False, "side": "NONE", "confidence": 0.0,
            "confirmations": 0, "contradictions": 99, "regime": "WARMUP",
            "trend": 0.0, "momentum": 0.0, "atrp": 0.0, "z": 0.0, "rv": 0.0
        }

    close = float(row.close)
    trend = float((row.ef - row.es) / close)
    mom = float(row.mom)
    mom_slow = float(row.mom_slow)
    z = float(row.z)
    rv = float(row.rv)
    atrp = float(row.atr / close)
    volshock = float(row.atr / row.atr_med) if float(row.atr_med) > 0 else 1.0

    breakout_up = bool(row.close > row.hh)
    breakout_dn = bool(row.close < row.ll)
    weak_trend = abs(trend) < 0.0010

    # Directional score is symmetric. Positive = LONG, negative = SHORT.
    directional = (
        0.36 * np.tanh(trend * 300)
        + 0.30 * np.tanh(mom * 45)
        + 0.16 * np.tanh(mom_slow * 22)
        + 0.10 * (1 if breakout_up else (-1 if breakout_dn else 0))
    )
    if rv > 1.15:
        directional *= 1.08

    range_side = "NONE"
    if weak_trend and z <= -2.1 and volshock < 2.4:
        range_side = "LONG"
    elif weak_trend and z >= 2.1 and volshock < 2.4:
        range_side = "SHORT"

    if range_side != "NONE":
        side = range_side
        regime = "RANGE_REVERSION"
    elif directional > 0.20:
        side = "LONG"
        regime = "TREND"
    elif directional < -0.20:
        side = "SHORT"
        regime = "TREND"
    else:
        side = "NONE"
        regime = "RANGE"

    long_evidence = [
        trend > 0.0008, mom > 0.0012, mom_slow > 0.0015,
        breakout_up and rv >= 0.9
    ]
    short_evidence = [
        trend < -0.0008, mom < -0.0012, mom_slow < -0.0015,
        breakout_dn and rv >= 0.9
    ]
    if side == "LONG":
        confirmations = sum(bool(x) for x in long_evidence)
        contradictions = sum(bool(x) for x in short_evidence)
    elif side == "SHORT":
        confirmations = sum(bool(x) for x in short_evidence)
        contradictions = sum(bool(x) for x in long_evidence)
    else:
        confirmations, contradictions = 0, 0

    # Explicitly reject fragile conditions rather than interpreting them as alpha.
    if (breakout_up or breakout_dn) and rv < 0.65:
        contradictions += 1
    if volshock > 3.0 or atrp > 0.045:
        contradictions += 1

    if range_side != "NONE":
        confirmations = max(confirmations, 2)
        confidence = 0.64 + min(max(abs(z) - 2.1, 0.0), 1.5) * 0.07
    else:
        confidence = 0.43 + 0.105 * confirmations + 0.13 * min(abs(float(directional)), 1.0)
        if rv > 1.2:
            confidence += 0.035
    confidence -= 0.17 * contradictions
    confidence = float(np.clip(confidence, 0.0, 0.99))

    coherent = side != "NONE" and contradictions == 0 and (
        confirmations >= 3 or range_side != "NONE"
    )
    return {
        "pulse": bool(coherent and confidence >= PULSE_MIN),
        "side": side,
        "confidence": round(confidence, 4),
        "confirmations": int(confirmations),
        "contradictions": int(contradictions),
        "regime": regime,
        "trend": trend,
        "momentum": mom,
        "momentum_slow": mom_slow,
        "directional_score": round(float(directional), 5),
        "atrp": atrp,
        "z": z,
        "rv": rv,
        "volshock": round(volshock, 4),
    }


def per_side_cost_rate(market: str = "spot") -> float:
    maker = FUTURES_MAKER_FEE_BPS if market == "futures" else SPOT_MAKER_FEE_BPS
    return (maker + SLIPPAGE_BPS + EXECUTION_PENALTY_BPS) / 10000.0


def live_pulse(symbol: str) -> dict[str, Any] | None:
    x = features(klines(symbol, limit=220))
    if len(x) < 60:
        return None
    row = x.iloc[-1]
    p = pulse_logic(row)
    expected_move = max(abs(float(p.get("momentum", 0.0))), 1.6 * float(p.get("atrp", 0.0)))
    round_trip = 2.0 * per_side_cost_rate("spot")
    net_edge = expected_move - round_trip
    p.update({
        "symbol": symbol,
        "price": round(float(row.close), 10),
        "atr_pct": round(float(p.get("atrp", 0.0)) * 100, 3),
        "volume_ratio": round(float(p.get("rv", 0.0)), 3),
        "expected_move_proxy_pct": round(expected_move * 100, 3),
        "round_trip_cost_floor_pct": round(round_trip * 100, 3),
        "net_edge_proxy_pct": round(net_edge * 100, 3),
        "tradeable": bool(p["pulse"] and p["side"] == "LONG" and expected_move >= round_trip * 1.35 and p["confidence"] >= PULSE_MIN),
    })
    return p


def run_bt(df: pd.DataFrame, symbol: str, params: dict[str, Any], market: str = "spot") -> dict[str, Any]:
    x = features(df, params["fast"], params["slow"], params["zwin"])
    eq = START_CAPITAL
    peak = eq
    maxdd = 0.0
    trades: list[Trade] = []
    pos: dict[str, Any] | None = None
    side_cost = per_side_cost_rate(market)
    warmup = max(params["slow"], params["zwin"], 65)

    for i in range(warmup, len(x) - 1):
        row = x.iloc[i]
        nxt = x.iloc[i + 1]

        if pos is not None:
            sign = 1.0 if pos["side"] == "LONG" else -1.0
            if pos["side"] == "LONG":
                stop_px = pos["entry"] * (1.0 - pos["stop"])
                take_px = pos["entry"] * (1.0 + pos["take"])
                stop_hit = float(row.low) <= stop_px
                take_hit = float(row.high) >= take_px
            else:
                stop_px = pos["entry"] * (1.0 + pos["stop"])
                take_px = pos["entry"] * (1.0 - pos["take"])
                stop_hit = float(row.high) >= stop_px
                take_hit = float(row.low) <= take_px

            age = i - pos["signal_i"]
            now_pulse = pulse_logic(row)
            reason = None
            exit_px = None

            # Conservative ordering if both barriers are crossed inside one OHLC candle.
            if stop_hit:
                reason, exit_px = "SL", stop_px
            elif take_hit:
                reason, exit_px = "TP", take_px
            elif age >= params["max_hold"]:
                reason, exit_px = "TIME", float(row.close)
            elif now_pulse["pulse"] and now_pulse["side"] not in ("NONE", pos["side"]) and age >= 2:
                reason, exit_px = "REVERSAL", float(row.close)
            elif now_pulse["contradictions"] >= 2 and age >= 2:
                reason, exit_px = "CONTRADICTION", float(row.close)

            if reason:
                gross_ret = sign * (exit_px - pos["entry"]) / pos["entry"]
                gross = pos["notional"] * gross_ret
                costs = pos["notional"] * side_cost + max(pos["notional"] + gross, 0.0) * side_cost
                pnl = gross - costs
                eq += pnl
                trades.append(Trade(
                    symbol, pos["side"], str(pos["time"]), str(row.ts), pos["entry"], exit_px,
                    pos["notional"], pnl, costs, reason, eq
                ))
                pos = None

        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak if peak else 0.0)
        if eq <= START_CAPITAL * (1.0 - MAX_DD):
            break

        if pos is None:
            pulse = pulse_logic(row)
            if not pulse["pulse"] or pulse["confidence"] < params["threshold"]:
                continue
            side = pulse["side"]
            if market == "spot" and side != "LONG":
                continue

            atrp = max(float(pulse["atrp"]), 0.0005)
            round_trip = 2.0 * side_cost
            expected_move = max(
                abs(float(pulse["momentum"])),
                abs(float(pulse.get("momentum_slow", 0.0))) * 0.55,
                1.7 * atrp,
            )
            if expected_move < params.get("edge_multiple", 1.35) * round_trip:
                continue

            stop = max(params["stop_atr"] * atrp, round_trip * 0.75)
            take = max(params["take_atr"] * atrp, round_trip * params["cost_multiple"])
            if take <= params["cost_multiple"] * round_trip:
                continue

            risk_cash = min(eq * RISK_PCT, max(eq - START_CAPITAL * (1.0 - MAX_DD), 0.0))
            notional = min(eq * params["max_alloc"], risk_cash / stop if stop > 0 else 0.0)
            if notional < 100:
                continue

            entry = float(nxt.open)
            pos = {
                "side": side, "entry": entry, "time": nxt.ts, "signal_i": i,
                "notional": notional, "stop": stop, "take": take,
            }

    if pos is not None and len(x):
        row = x.iloc[-1]
        exit_px = float(row.close)
        sign = 1.0 if pos["side"] == "LONG" else -1.0
        gross_ret = sign * (exit_px - pos["entry"]) / pos["entry"]
        gross = pos["notional"] * gross_ret
        costs = pos["notional"] * side_cost + max(pos["notional"] + gross, 0.0) * side_cost
        pnl = gross - costs
        eq += pnl
        trades.append(Trade(
            symbol, pos["side"], str(pos["time"]), str(row.ts), pos["entry"], exit_px,
            pos["notional"], pnl, costs, "EOD", eq
        ))

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else (99.0 if wins else 0.0)
    expectancy = float(np.mean(pnls)) if pnls else 0.0
    longs = sum(t.side == "LONG" for t in trades)
    shorts = sum(t.side == "SHORT" for t in trades)

    return {
        "symbol": symbol,
        "market": market,
        "equity": round(eq, 2),
        "return_pct": round((eq / START_CAPITAL - 1.0) * 100.0, 3),
        "trades": len(trades),
        "longs": longs,
        "shorts": shorts,
        "win_rate": round(100.0 * len(wins) / len(pnls), 2) if pnls else 0.0,
        "profit_factor": round(float(pf), 3),
        "expectancy_czk": round(expectancy, 3),
        "max_dd_pct": round(maxdd * 100.0, 3),
        "costs_czk": round(sum(t.costs for t in trades), 2),
        "ledger": [asdict(t) for t in trades],
    }


def objective(r: dict[str, Any]) -> float:
    if r["trades"] < 3:
        return -999.0
    return (
        float(r["return_pct"])
        + 1.4 * min(float(r["profit_factor"]), 4.0)
        - 2.0 * float(r["max_dd_pct"])
        + 0.02 * float(r["trades"])
    )


def optimize(symbol: str) -> dict[str, Any]:
    df = klines(symbol, limit=720)
    if len(df) < 300:
        raise RuntimeError(f"Insufficient OHLC history: {len(df)} bars")

    n = len(df)
    a, b = int(n * 0.60), int(n * 0.80)
    train = df.iloc[:a].reset_index(drop=True)
    valid = df.iloc[a:b].reset_index(drop=True)
    holdout = df.iloc[b:].reset_index(drop=True)

    grid = []
    for fast, slow, thr, sa, ta, hold in itertools.product(
        [5, 8, 12], [20, 30], [0.62, 0.68, 0.74],
        [1.0, 1.4], [1.8, 2.4, 3.0], [10, 25, 60]
    ):
        if fast >= slow:
            continue
        p = {
            "fast": fast, "slow": slow, "zwin": 30, "threshold": thr,
            "stop_atr": sa, "take_atr": ta, "max_hold": hold,
            "max_alloc": 0.55, "cost_multiple": 1.5, "edge_multiple": 1.35
        }
        tr = run_bt(train, symbol, p, "spot")
        grid.append((objective(tr), p, tr))
    grid.sort(key=lambda z: z[0], reverse=True)

    candidates = []
    for _, p, tr in grid[:12]:
        va = run_bt(valid, symbol, p, "spot")
        candidates.append((objective(va), p, tr, va))
    candidates.sort(key=lambda z: z[0], reverse=True)

    _, p, tr, va = candidates[0]
    ho = run_bt(holdout, symbol, p, "spot")
    full = run_bt(df, symbol, p, "spot")

    return {
        "params": p,
        "train": {k: v for k, v in tr.items() if k != "ledger"},
        "validation": {k: v for k, v in va.items() if k != "ledger"},
        "holdout": {k: v for k, v in ho.items() if k != "ledger"},
        "full": full,
        "data_warning": "REST OHLC is a short recent window; holdout is a smoke test, not proof of durable profitability.",
    }


def futures_klines(symbol: str, count: int = 720) -> pd.DataFrame:
    url = f"{KRAKEN_FUTURES}/trade/{symbol}/1m"
    r = SESSION.get(url, params={"count": count}, timeout=15)
    r.raise_for_status()
    data = r.json()
    rows = data.get("candles", [])
    if not rows:
        raise RuntimeError(f"No futures candles for {symbol}")
    df = pd.DataFrame(rows)
    required = ["time", "open", "high", "low", "close", "volume"]
    missing = [x for x in required if x not in df.columns]
    if missing:
        raise RuntimeError(f"Unexpected futures candle schema: missing {missing}")
    df = df.rename(columns={"time": "ts"})
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["vwap"] = df["close"]
    df["count"] = 0
    df["ts"] = pd.to_datetime(pd.to_numeric(df["ts"]), unit="ms", utc=True)
    return df[["ts","open","high","low","close","vwap","volume","count"]].dropna().sort_values("ts").reset_index(drop=True)


def live_futures_pulse(symbol: str) -> dict[str, Any] | None:
    x = features(futures_klines(symbol, 240))
    if len(x) < 60:
        return None
    row = x.iloc[-1]
    p = pulse_logic(row)
    expected_move = max(abs(float(p.get("momentum", 0.0))), 1.6 * float(p.get("atrp", 0.0)))
    round_trip = 2.0 * per_side_cost_rate("futures")
    net_edge = expected_move - round_trip
    p.update({
        "symbol": symbol,
        "market": "futures-paper",
        "price": round(float(row.close), 10),
        "atr_pct": round(float(p.get("atrp", 0.0)) * 100, 3),
        "volume_ratio": round(float(p.get("rv", 0.0)), 3),
        "expected_move_proxy_pct": round(expected_move * 100, 3),
        "round_trip_cost_floor_pct": round(round_trip * 100, 3),
        "net_edge_proxy_pct": round(net_edge * 100, 3),
        "tradeable": bool(p["pulse"] and expected_move >= round_trip * 1.35 and p["confidence"] >= PULSE_MIN),
    })
    return p


def synthetic_frame(kind: str, n: int = 720, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    if kind == "trend":
        r = 0.00045 + rng.normal(0, 0.0012, n)
    elif kind == "chop":
        r = rng.normal(0, 0.0015, n)
    elif kind == "down":
        r = -0.00045 + rng.normal(0, 0.0015, n)
    else:
        raise ValueError(kind)

    close = 100.0 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    wiggle = np.abs(rng.normal(0.0012, 0.0005, n))
    high = np.maximum(open_, close) * (1 + wiggle)
    low = np.minimum(open_, close) * (1 - wiggle)
    volume = rng.lognormal(4.0, 0.45, n) * (1 + np.maximum(r, 0) * 250)
    ts = pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC")

    return pd.DataFrame({
        "ts": ts, "open": open_, "high": high, "low": low,
        "close": close, "vwap": close, "volume": volume, "count": 100
    })


def selftest_report() -> dict[str, Any]:
    p = {
        "fast": 8, "slow": 24, "zwin": 30, "threshold": 0.64,
        "stop_atr": 1.2, "take_atr": 2.4, "max_hold": 25,
        "max_alloc": 0.55, "cost_multiple": 1.5
    }
    trend = run_bt(synthetic_frame("trend"), "SYNTH_TREND", p)
    chop = run_bt(synthetic_frame("chop"), "SYNTH_CHOP", p)
    down = run_bt(synthetic_frame("down"), "SYNTH_DOWN", p)

    checks = {
        "weak_micro_is_cost_gated": trend["trades"] == 0,
        "no_nan_metrics": all(np.isfinite([
            trend["return_pct"], chop["return_pct"], down["return_pct"]
        ])),
        "capital_guard": min(
            trend["equity"], chop["equity"], down["equity"]
        ) >= START_CAPITAL * (1 - MAX_DD) - 100,
        "downtrend_not_forced_long": down["trades"] <= max(trend["trades"], 1),
    }

    return {
        "ok": all(checks.values()),
        "checks": checks,
        "trend": {k: v for k, v in trend.items() if k != "ledger"},
        "chop": {k: v for k, v in chop.items() if k != "ledger"},
        "down": {k: v for k, v in down.items() if k != "ledger"},
        "interpretation": "At Tier-1 spot costs, ordinary 1m micro-signals are correctly rejected rather than forced.",
    }


@app.on_event("startup")
def v4_startup():
    if AUTO_RECORD:
        RECORDER.start()
        ALPHA_RUNTIME.start()


@app.on_event("shutdown")
def v4_shutdown():
    ALPHA_RUNTIME.stop()
    RECORDER.stop()


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta charset='utf-8'><title>IMPULSE MAX 5K V4</title>
<style>
body{font-family:system-ui;max-width:1100px;margin:30px auto;padding:0 16px;background:#0b1020;color:#e8eefc}
button{padding:11px 16px;margin:4px;border-radius:8px;border:0;cursor:pointer}
pre{white-space:pre-wrap;background:#141b31;padding:16px;border-radius:12px;min-height:220px}
small{color:#9aa9c7}
</style></head><body>
<h1>IMPULSE MAX 5K — Kraken Pulse Hunter V4</h1>
<p><b>PAPER / RESEARCH.</b> Live orders are disabled. V4 records Kraken L2 order book + taker trades, learns validated 30s/60s microstructure states and only then opens fixed-horizon PAPER signals.</p>
<div>
<button onclick="go('/api/v4/status')">V4 status</button>
<button onclick="go('/api/v4/start','POST')">Start recorder + alpha</button>
<button onclick="go('/api/v4/models')">Alpha models</button>
<button onclick="go('/api/v4/paper')">Paper ledger</button>
<button onclick="go('/api/futures-pulses')">Cross-asset futures scan</button>
<button onclick="go('/api/v4/stop','POST')">Stop</button>
</div>
<small>Decision cadence 30 s; market data is recorded continuously at ~1 s snapshots. A trade is not forced when no validated edge exists.</small>
<pre id='o'>Ready.</pre>
<script>
async function go(u,m='GET'){o.textContent='Running...';try{let r=await fetch(u,{method:m});o.textContent=JSON.stringify(await r.json(),null,2)}catch(e){o.textContent=String(e)}}
</script></body></html>"""


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": ENGINE_BUILD,
        "build": ENGINE_BUILD,
        "mode": "PAPER_RESEARCH",
        "capital": START_CAPITAL,
        "execution_mode": EXECUTION_MODE,
        "alpha_cost_bps": ALPHA_COST_BPS,
        "validated_horizons": list(VALIDATED_HORIZONS),
        "shadow_horizons": list(SHADOW_HORIZONS),
        "live_orders": False,
    }


@app.post("/api/v4/start")
def v4_start():
    r1 = RECORDER.start()
    r2 = ALPHA_RUNTIME.start()
    return {
        "ok": True,
        "recorder_started": r1,
        "alpha_started": r2,
        "status": {"recorder": RECORDER.status(), "alpha": ALPHA_RUNTIME.status()},
        "live_orders": False,
    }


@app.post("/api/v4/stop")
def v4_stop():
    ALPHA_RUNTIME.stop()
    RECORDER.stop()
    return {"ok": True, "message": "V4 recorder/alpha stop requested", "live_orders": False}


@app.get("/api/v4/status")
def v4_status():
    return {
        "version": ENGINE_BUILD,
        "build": ENGINE_BUILD,
        "recorder": RECORDER.status(),
        "alpha": ALPHA_RUNTIME.status(),
        "decision_interval_s": int(os.getenv("ALPHA_INTERVAL_S", "30")),
        "execution_mode": EXECUTION_MODE,
        "alpha_cost_bps": ALPHA_COST_BPS,
        "validated_horizons": list(VALIDATED_HORIZONS),
        "shadow_horizons": list(SHADOW_HORIZONS),
        "live_orders": False,
    }


@app.get("/api/v4/models")
def v4_models():
    try:
        # AlphaRuntime already recomputes models on its own cadence. Reuse the
        # latest completed snapshot instead of doing the expensive discovery
        # again on every Supervisor status poll.
        if ALPHA_RUNTIME.last_models is not None:
            return ALPHA_RUNTIME.last_models
        return discover_models()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "live_orders": False}


@app.get("/api/v4/paper")
def v4_paper():
    from microstructure import connect_db
    with connect_db() as con:
        rows = con.execute(
            """SELECT id,opened_ms,closed_ms,symbol,side,horizon_s,entry,exit,
                      notional_czk,model_edge_bps,model_score,cost_bps,pnl_czk,
                      net_bps,state_key,status
               FROM paper_trades ORDER BY id DESC LIMIT 100"""
        ).fetchall()
        shadow_rows = con.execute(
            """SELECT id,opened_ms,closed_ms,symbol,side,horizon_s,entry,exit,
                      notional_czk,signal_edge_bps,signal_score,cost_bps,pnl_czk,
                      net_bps,state_key,signal_kind,status
               FROM shadow_paper_trades ORDER BY id DESC LIMIT 100"""
        ).fetchall()
        scenario_rows = con.execute(
            """SELECT id,opened_ms,closed_ms,symbol,side,horizon_s,entry,exit,
                      notional_czk,gross_edge_bps,score,cost_bps,pnl_czk,
                      net_bps,state_key,scenario,status
               FROM scenario_paper_trades ORDER BY id DESC LIMIT 100"""
        ).fetchall()
    cols = ["id","opened_ms","closed_ms","symbol","side","horizon_s","entry","exit",
            "notional_czk","model_edge_bps","model_score","cost_bps","pnl_czk",
            "net_bps","state_key","status"]
    shadow_cols = ["id","opened_ms","closed_ms","symbol","side","horizon_s","entry","exit",
                   "notional_czk","signal_edge_bps","signal_score","cost_bps","pnl_czk",
                   "net_bps","state_key","signal_kind","status"]
    scenario_cols = ["id","opened_ms","closed_ms","symbol","side","horizon_s","entry","exit",
                     "notional_czk","gross_edge_bps","score","cost_bps","pnl_czk",
                     "net_bps","state_key","scenario","status"]
    return {
        "trades": [dict(zip(cols, r)) for r in rows],
        "shadow_trades": [dict(zip(shadow_cols, r)) for r in shadow_rows],
        "scenario_trades": [dict(zip(scenario_cols, r)) for r in scenario_rows],
        "shadow_counts_for_live_gate": False,
        "scenario_counts_for_live_gate": False,
        "live_orders": False,
    }


@app.get("/api/selftest")
def selftest():
    return selftest_report()


@app.get("/api/pulses")
def pulses():
    universe = kraken_universe(UNIVERSE_MAX)
    out: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max(1, min(SCAN_WORKERS, 6))) as ex:
        futures = {ex.submit(live_pulse, s): s for s in universe}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                p = fut.result()
                if p:
                    out.append(p)
            except Exception as e:
                out.append({"symbol": s, "error": str(e)})

    good = [x for x in out if x.get("tradeable")]
    good.sort(
        key=lambda x: (x.get("net_edge_proxy_pct", -99), x.get("confidence", 0)),
        reverse=True
    )

    return {
        "mode": "KRAKEN_PULSE_SCAN",
        "scanned": len(universe),
        "tradeable": len(good),
        "best": good[:10],
        "all": out,
        "note": "Candidate scanner only; no real orders are submitted.",
    }


@app.get("/api/futures-pulses")
def futures_pulses():
    out = []
    with ThreadPoolExecutor(max_workers=max(1, min(SCAN_WORKERS, 6))) as ex:
        futures = {ex.submit(live_futures_pulse, s): s for s in FUTURES_SYMBOLS}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                p = fut.result()
                if p:
                    out.append(p)
            except Exception as e:
                out.append({"symbol": s, "error": str(e)})
    good = [x for x in out if x.get("tradeable")]
    good.sort(key=lambda x: (x.get("net_edge_proxy_pct", -99), x.get("confidence", 0)), reverse=True)
    return {
        "mode": "KRAKEN_FUTURES_PAPER_SCAN",
        "tradeable": len(good),
        "best": good,
        "all": out,
        "note": "PAPER research only. Notional model is capped below account equity; no live derivatives orders are enabled.",
    }


@app.post("/api/run")
def run():
    out: dict[str, Any] = {}
    for s in SYMBOLS:
        try:
            out[s] = optimize(s)
        except Exception as e:
            out[s] = {"error": str(e)}

    return {
        "mode": "PAPER_REPLAY",
        "start_capital_czk": START_CAPITAL,
        "results": out,
        "warning": "Short REST replay is a smoke test, not evidence of durable profitability. LIVE remains disabled.",
    }

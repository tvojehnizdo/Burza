from __future__ import annotations

import math
from typing import Any

import pandas as pd
import requests

from dynamic_universe import discover

API = "https://api.kraken.com/0/public"


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = requests.get(API + path, params=params or {}, timeout=12)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError("; ".join(body["error"]))
    return body["result"]


def _ohlc(pair: str, limit: int = 90) -> pd.DataFrame:
    data = _get("/OHLC", {"pair": pair, "interval": 1})
    key = next(k for k in data if k != "last")
    rows = data[key][:-1][-limit:]
    df = pd.DataFrame(rows, columns=[
        "ts","open","high","low","close","vwap","volume","count"
    ])
    for col in ("open","high","low","close","vwap","volume","count"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().reset_index(drop=True)


def _ret(s: pd.Series, n: int) -> float:
    if len(s) <= n:
        return 0.0
    a, b = float(s.iloc[-n-1]), float(s.iloc[-1])
    return b / a - 1.0 if a > 0 else 0.0


def _ema(s: pd.Series, span: int) -> float:
    return float(s.ewm(span=span, adjust=False).mean().iloc[-1])


def scan(
    balances: dict[str, Any],
    max_pairs: int = 40,
    max_spread_bps: float = 25.0,
    maker_fee_bps_per_side: float = 40.0,
    execution_buffer_bps: float = 4.0,
    min_net_edge_bps: float = 5.0,
) -> dict[str, Any]:
    markets = discover(max_pairs=max_pairs, max_spread_bps=max_spread_bps)
    out: list[dict[str, Any]] = []

    usd = float(balances.get("ZUSD") or balances.get("USD") or 0.0)
    usdc = float(balances.get("USDC") or 0.0)

    for meta in markets:
        ws = str(meta.get("symbol") or "")
        alt = str(meta.get("altname") or ws.replace("/", ""))
        quote = str(meta.get("quote") or (ws.split("/",1)[1] if "/" in ws else ""))
        available = usdc if quote == "USDC" else usd if quote == "USD" else 0.0
        if available <= 0:
            continue

        try:
            df = _ohlc(alt, 90)
        except Exception:
            continue
        if len(df) < 35:
            continue

        close = df["close"]
        last = float(close.iloc[-1])
        if last <= 0:
            continue

        r3 = _ret(close, 3)
        r5 = _ret(close, 5)
        r10 = _ret(close, 10)
        r15 = _ret(close, 15)
        ema6 = _ema(close, 6)
        ema20 = _ema(close, 20)

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

        trend_ok = last > ema6 > ema20
        momentum_ok = r3 > 0 and r5 > 0 and r10 > 0 and r15 > -0.0025

        # Conservative forward-move proxy: observed momentum capped by recent
        # volatility. This is a ranking/filtering proxy, not a profit forecast.
        momentum_bps = max(r3 * 10000.0, r5 * 10000.0 * 0.75, r10 * 10000.0 * 0.45)
        vol_cap_bps = max(atr_bps * 2.2, atr_bps)
        expected_move_bps = max(0.0, min(momentum_bps, vol_cap_bps))

        spread_bps = float(meta.get("spread_bps") or 0.0)
        round_trip_cost_bps = 2.0 * maker_fee_bps_per_side + spread_bps + execution_buffer_bps
        net_edge_bps = expected_move_bps - round_trip_cost_bps

        try:
            ordermin = float(meta.get("ordermin") or 0.0)
        except Exception:
            ordermin = 0.0
        try:
            costmin = float(meta.get("costmin") or 0.0)
        except Exception:
            costmin = 0.0

        minimum_notional = max(costmin, ordermin * last)
        deployable = available * 0.95
        size_ok = deployable >= minimum_notional and minimum_notional > 0

        quality = (
            trend_ok
            and momentum_ok
            and vol_ratio >= 0.70
            and expected_move_bps > 0
            and net_edge_bps >= min_net_edge_bps
            and size_ok
        )

        score = (
            net_edge_bps
            + (10.0 if trend_ok else 0.0)
            + min(max(vol_ratio - 1.0, 0.0) * 5.0, 10.0)
        )

        out.append({
            "symbol": ws,
            "kraken_pair": alt,
            "quote": quote,
            "price": last,
            "available_quote": round(available, 8),
            "deployable_quote_95pct": round(deployable, 8),
            "ordermin_base": ordermin,
            "costmin_quote": costmin,
            "minimum_notional_quote": round(minimum_notional, 8),
            "volume_base": round(deployable / last, 12) if size_ok else 0.0,
            "spread_bps": round(spread_bps, 3),
            "maker_roundtrip_fee_bps": round(2.0 * maker_fee_bps_per_side, 3),
            "round_trip_cost_bps": round(round_trip_cost_bps, 3),
            "expected_move_proxy_bps": round(expected_move_bps, 3),
            "net_edge_proxy_bps": round(net_edge_bps, 3),
            "r3_bps": round(r3 * 10000.0, 3),
            "r5_bps": round(r5 * 10000.0, 3),
            "r10_bps": round(r10 * 10000.0, 3),
            "r15_bps": round(r15 * 10000.0, 3),
            "atr_bps": round(atr_bps, 3),
            "volume_ratio": round(vol_ratio, 3),
            "trend_ok": trend_ok,
            "momentum_ok": momentum_ok,
            "size_ok": size_ok,
            "ready": bool(quality),
            "score": round(score, 3),
        })

    out.sort(key=lambda x: (bool(x["ready"]), float(x["score"])), reverse=True)
    ready = [x for x in out if x["ready"]]
    return {
        "ready": bool(ready),
        "reason": "FAST_CANARY_READY" if ready else "NO_POSITIVE_FAST_CANARY",
        "candidate": ready[0] if ready else (out[0] if out else None),
        "ready_count": len(ready),
        "scanned_count": len(out),
        "top": out[:12],
        "actual_order_submitted": False,
        "note": "Fast canary uses live 1m OHLC/momentum/ATR and maker-cost economics; it is a screening proxy, not a guarantee of profit.",
    }

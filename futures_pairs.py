from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd

from futures_canary import _candles

NEG_CORR_MAX = -0.55
POS_CORR_MIN = 0.70
MIN_PAIR_MOMENTUM_BPS = 20.0
MIN_RV_ZSCORE = 1.50
PAIR_LOOKBACK = 60


def _series(symbol: str) -> pd.Series:
    df = _candles(symbol, 100)
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < PAIR_LOOKBACK + 5:
        raise RuntimeError(f"Insufficient pair history for {symbol}")
    return close.tail(PAIR_LOOKBACK + 1).reset_index(drop=True)


def _load(symbols: list[str]) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as pool:
        futs = {pool.submit(_series, s): s for s in symbols}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                out[s] = fut.result()
            except Exception:
                continue
    return out


def _ret_bps(s: pd.Series, n: int = 15) -> float:
    if len(s) <= n:
        return 0.0
    a = float(s.iloc[-n - 1])
    b = float(s.iloc[-1])
    return (b / a - 1.0) * 10000.0 if a > 0 else 0.0


def _corr(a: pd.Series, b: pd.Series) -> float:
    ra = a.pct_change().dropna()
    rb = b.pct_change().dropna()
    n = min(len(ra), len(rb), PAIR_LOOKBACK)
    if n < 20:
        return 0.0
    c = float(ra.tail(n).reset_index(drop=True).corr(rb.tail(n).reset_index(drop=True)))
    return c if math.isfinite(c) else 0.0


def _spread_z(a: pd.Series, b: pd.Series) -> float:
    # Log-ratio spread works even when assets have very different nominal prices.
    aa = a.astype(float)
    bb = b.astype(float)
    spread = (aa.apply(math.log) - bb.apply(math.log)).tail(PAIR_LOOKBACK)
    if len(spread) < 20:
        return 0.0
    mean = float(spread.mean())
    std = float(spread.std(ddof=0))
    if std <= 0:
        return 0.0
    return float((spread.iloc[-1] - mean) / std)


def scan_pairs(symbols: list[str]) -> dict[str, Any]:
    series = _load(symbols)
    names = sorted(series)
    candidates: list[dict[str, Any]] = []

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = names[i]
            b = names[j]
            sa = series[a]
            sb = series[b]
            corr = _corr(sa, sb)
            r15a = _ret_bps(sa, 15)
            r15b = _ret_bps(sb, 15)

            if (
                corr <= NEG_CORR_MAX
                and abs(r15a) >= MIN_PAIR_MOMENTUM_BPS
                and abs(r15b) >= MIN_PAIR_MOMENTUM_BPS
                and r15a * r15b < 0
            ):
                long_symbol = a if r15a > 0 else b
                short_symbol = b if r15a > 0 else a
                score = abs(corr) * min(abs(r15a), abs(r15b))
                candidates.append({
                    "mode": "inverse_correlation",
                    "symbol_a": a,
                    "symbol_b": b,
                    "correlation": round(corr, 4),
                    "r15_a_bps": round(r15a, 2),
                    "r15_b_bps": round(r15b, 2),
                    "long_symbol": long_symbol,
                    "short_symbol": short_symbol,
                    "score": round(score, 3),
                })

            if corr >= POS_CORR_MIN:
                z = _spread_z(sa, sb)
                if abs(z) >= MIN_RV_ZSCORE:
                    # Positive z means A is rich versus B.
                    long_symbol = b if z > 0 else a
                    short_symbol = a if z > 0 else b
                    score = abs(z) * corr * 100.0
                    candidates.append({
                        "mode": "relative_value",
                        "symbol_a": a,
                        "symbol_b": b,
                        "correlation": round(corr, 4),
                        "spread_z": round(z, 3),
                        "r15_a_bps": round(r15a, 2),
                        "r15_b_bps": round(r15b, 2),
                        "long_symbol": long_symbol,
                        "short_symbol": short_symbol,
                        "score": round(score, 3),
                    })

    candidates.sort(key=lambda x: float(x["score"]), reverse=True)
    return {
        "ok": True,
        "universe_count": len(names),
        "candidate_count": len(candidates),
        "top": candidates[:20],
        "rules": {
            "negative_corr_max": NEG_CORR_MAX,
            "positive_corr_min": POS_CORR_MIN,
            "min_pair_momentum_bps": MIN_PAIR_MOMENTUM_BPS,
            "min_rv_zscore": MIN_RV_ZSCORE,
            "lookback_minutes": PAIR_LOOKBACK,
        },
    }

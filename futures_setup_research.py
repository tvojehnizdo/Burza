from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from historical_research import DEFAULT_COST_BPS, find_pair_file, load_ohlcvt

LOOKBACKS = (10, 15, 20)
BUFFERS_BPS = (3.0, 5.0, 8.0)
MIN_MOVES_BPS = (25.0, 35.0, 50.0)
STOPS_BPS = (35.0, 45.0, 60.0)
TRAIL_ACTIVATIONS_BPS = (35.0, 45.0, 60.0)
TRAIL_GAPS_BPS = (10.0, 14.0, 18.0)
MAX_HOLDS_MIN = (8, 15, 30)

NO_PROGRESS_MIN = 3
NO_PROGRESS_CURRENT_BPS = 22.0
NO_PROGRESS_MAX_FAV_BPS = 45.0
BACKUP_TP_BPS = 300.0


def _features(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    c = x["close"]
    x["ema6"] = c.ewm(span=6, adjust=False).mean()
    x["ema20"] = c.ewm(span=20, adjust=False).mean()
    for n in (5, 15, 30, 60):
        x[f"r{n}"] = c.pct_change(n)
    prev = c.shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev).abs(),
        (x["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    x["atr_bps"] = tr.rolling(14).mean() / c * 10000.0
    medv = x["volume"].rolling(30).median()
    x["vol_ratio"] = np.where(medv > 0, x["volume"] / medv, 1.0)
    return x.dropna().reset_index(drop=True)


def _base_side(row: pd.Series) -> str:
    close = float(row["close"])
    up = (
        close > float(row["ema6"]) > float(row["ema20"])
        and float(row["r5"]) > 0
        and float(row["r15"]) > 0
        and float(row["r30"]) > 0
        and float(row["r60"]) > 0
    )
    down = (
        close < float(row["ema6"]) < float(row["ema20"])
        and float(row["r5"]) < 0
        and float(row["r15"]) < 0
        and float(row["r30"]) < 0
        and float(row["r60"]) < 0
    )
    if float(row["vol_ratio"]) < 0.60:
        return "NONE"
    return "LONG" if up else "SHORT" if down else "NONE"


def _entry_events(
    x: pd.DataFrame,
    lookback: int,
    buffer_bps: float,
    min_move_bps: float,
) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    buffer = buffer_bps / 10000.0
    min_move = min_move_bps / 10000.0
    for i in range(max(60, lookback), len(x) - max(MAX_HOLDS_MIN) - 1):
        prior = x.iloc[i - lookback:i]
        row = x.iloc[i]
        side = _base_side(row)
        if side == "NONE":
            continue
        hi = float(prior["high"].max())
        lo = float(prior["low"].min())
        anchor = float(prior["open"].iloc[0])
        close = float(row["close"])
        if min(hi, lo, anchor, close) <= 0:
            continue
        move = close / anchor - 1.0
        if side == "LONG" and close > hi * (1.0 + buffer) and move >= min_move:
            events.append((i, side))
        elif side == "SHORT" and close < lo * (1.0 - buffer) and move <= -min_move:
            events.append((i, side))
    return events


def _pnl_bps(side: str, entry: float, px: float) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (px / entry - 1.0) * 10000.0


def _simulate_one(
    x: pd.DataFrame,
    i: int,
    side: str,
    *,
    stop_bps: float,
    trail_activation_bps: float,
    trail_gap_bps: float,
    max_hold_min: int,
    cost_bps: float,
) -> float:
    entry = float(x.iloc[i]["close"])
    max_fav = 0.0
    end = min(i + max_hold_min, len(x) - 1)

    for j in range(i + 1, end + 1):
        row = x.iloc[j]
        hi = float(row["high"])
        lo = float(row["low"])
        close = float(row["close"])

        if side == "LONG":
            favorable = (hi / entry - 1.0) * 10000.0
            adverse = (1.0 - lo / entry) * 10000.0
        else:
            favorable = (1.0 - lo / entry) * 10000.0
            adverse = (hi / entry - 1.0) * 10000.0

        # Pessimistic bar ordering: if the hard stop can have traded, assume it
        # did before crediting a favorable excursion from the same 1m bar.
        if adverse >= stop_bps:
            return -stop_bps - cost_bps

        max_fav = max(max_fav, favorable)
        if max_fav >= BACKUP_TP_BPS:
            return BACKUP_TP_BPS - cost_bps

        current = _pnl_bps(side, entry, close)
        if max_fav >= trail_activation_bps:
            floor = max(30.0, max_fav - trail_gap_bps)
            if current <= floor:
                return current - cost_bps

        age_min = j - i
        if (
            age_min >= NO_PROGRESS_MIN
            and current < NO_PROGRESS_CURRENT_BPS
            and max_fav < NO_PROGRESS_MAX_FAV_BPS
        ):
            return current - cost_bps

    return _pnl_bps(side, entry, float(x.iloc[end]["close"])) - cost_bps


def _stats(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0, "mean_net_bps": None, "hit_rate": None, "profit_factor": None}
    a = np.asarray(vals, dtype=float)
    wins = a[a > 0]
    losses = a[a < 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) else (99.0 if len(wins) else 0.0)
    return {
        "n": int(len(a)),
        "mean_net_bps": round(float(a.mean()), 4),
        "median_net_bps": round(float(np.median(a)), 4),
        "hit_rate": round(float(np.mean(a > 0)), 4),
        "profit_factor": round(pf, 4),
        "stdev_bps": round(float(a.std(ddof=1)), 4) if len(a) > 1 else 0.0,
        "net_sum_bps": round(float(a.sum()), 3),
    }


def _split(vals: list[tuple[pd.Timestamp, float]]) -> dict[str, dict[str, Any]]:
    vals = sorted(vals, key=lambda z: z[0])
    n = len(vals)
    a = int(n * 0.50)
    b = int(n * 0.75)
    return {
        "train": _stats([v for _, v in vals[:a]]),
        "validation": _stats([v for _, v in vals[a:b]]),
        "holdout": _stats([v for _, v in vals[b:]]),
    }


def research_symbol(
    df: pd.DataFrame,
    symbol: str,
    *,
    cost_bps: float = DEFAULT_COST_BPS,
    days: int = 180,
) -> dict[str, Any]:
    if days > 0 and len(df):
        cutoff = pd.Timestamp(df["ts"].max()) - pd.Timedelta(days=days)
        df = df[df["ts"] >= cutoff].reset_index(drop=True)
    x = _features(df)
    results: list[dict[str, Any]] = []

    for lookback, buffer_bps, min_move_bps in itertools.product(
        LOOKBACKS, BUFFERS_BPS, MIN_MOVES_BPS
    ):
        events = _entry_events(x, lookback, buffer_bps, min_move_bps)
        if len(events) < 40:
            continue
        for stop_bps, trail_activation_bps, trail_gap_bps, max_hold_min in itertools.product(
            STOPS_BPS, TRAIL_ACTIVATIONS_BPS, TRAIL_GAPS_BPS, MAX_HOLDS_MIN
        ):
            timed: list[tuple[pd.Timestamp, float]] = []
            for i, side in events:
                pnl = _simulate_one(
                    x, i, side,
                    stop_bps=stop_bps,
                    trail_activation_bps=trail_activation_bps,
                    trail_gap_bps=trail_gap_bps,
                    max_hold_min=max_hold_min,
                    cost_bps=cost_bps,
                )
                timed.append((pd.Timestamp(x.iloc[i]["ts"]), pnl))
            splits = _split(timed)
            train = splits["train"]
            valid = splits["validation"]
            hold = splits["holdout"]
            if min(train["n"], valid["n"], hold["n"]) < 10:
                continue
            pre_hold_robust = (
                float(train["mean_net_bps"] or -999.0) > 0
                and float(valid["mean_net_bps"] or -999.0) > 0
                and float(train["median_net_bps"] or -999.0) > 0
                and float(valid["median_net_bps"] or -999.0) > 0
                and float(train["profit_factor"] or 0.0) >= 1.15
                and float(valid["profit_factor"] or 0.0) >= 1.15
                and float(train["hit_rate"] or 0.0) >= 0.52
                and float(valid["hit_rate"] or 0.0) >= 0.52
            )
            holdout_pass = (
                float(hold["mean_net_bps"] or -999.0) > 0
                and float(hold["median_net_bps"] or -999.0) > 0
                and float(hold["profit_factor"] or 0.0) > 1.0
                and float(hold["hit_rate"] or 0.0) >= 0.50
            )
            worst_pre_hold = min(
                float(train["mean_net_bps"] or -999.0),
                float(valid["mean_net_bps"] or -999.0),
            )
            noise = max(
                float(train["stdev_bps"] or 0.0),
                float(valid["stdev_bps"] or 0.0),
                1.0,
            )
            n_eff = min(int(train["n"]), int(valid["n"]))
            robust_score = worst_pre_hold * math.sqrt(n_eff) / noise
            results.append({
                "symbol": symbol,
                "params": {
                    "lookback_min": lookback,
                    "buffer_bps": buffer_bps,
                    "min_move_bps": min_move_bps,
                    "stop_bps": stop_bps,
                    "trail_activation_bps": trail_activation_bps,
                    "trail_gap_bps": trail_gap_bps,
                    "max_hold_min": max_hold_min,
                },
                "train": train,
                "validation": valid,
                "holdout": hold,
                "pre_hold_robust": pre_hold_robust,
                "holdout_pass": holdout_pass,
                "promotion_candidate": bool(pre_hold_robust and holdout_pass),
                "robust_score": round(robust_score, 4),
            })

    promotable = [x for x in results if x["promotion_candidate"]]
    promotable.sort(
        key=lambda z: (
            float(z["robust_score"]),
            float(z["holdout"]["mean_net_bps"] or -999.0),
            int(z["holdout"]["n"]),
        ),
        reverse=True,
    )
    return {
        "symbol": symbol,
        "bars": len(x),
        "days": days,
        "cost_bps": cost_bps,
        "tested_configs": len(results),
        "promotion_candidates": len(promotable),
        "best": promotable[:20],
    }


def selftest() -> dict[str, Any]:
    n = 120
    ts = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    close = np.ones(n) * 100.0
    close[70:] = np.linspace(100.0, 102.5, n - 70)
    df = pd.DataFrame({
        "ts": ts,
        "open": close,
        "high": close * 1.0005,
        "low": close * 0.9995,
        "close": close,
        "volume": np.ones(n) * 100.0,
        "trades": np.ones(n),
    })
    x = _features(df)
    sample = _stats([10.0, -5.0, 20.0])
    checks = {
        "features_nonempty": len(x) > 0,
        "profit_factor_positive": float(sample["profit_factor"] or 0.0) > 1.0,
        "grid_has_current_range": 15 in LOOKBACKS and 5.0 in BUFFERS_BPS and 35.0 in MIN_MOVES_BPS,
        "grid_has_current_stop": 45.0 in STOPS_BPS and 45.0 in TRAIL_ACTIVATIONS_BPS,
    }
    return {"ok": all(checks.values()), "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/kraken_history/full")
    ap.add_argument("--symbols", nargs="+", default=["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD"])
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--out", default="reports/setup-v3-research.json")
    args = ap.parse_args()

    root = Path(args.root)
    report: dict[str, Any] = {
        "method": "chronological 50/25/25 setup grid; promotion requires positive train, validation and untouched holdout",
        "cost_bps": args.cost_bps,
        "days": args.days,
        "symbols": {},
    }
    for symbol in args.symbols:
        try:
            path = find_pair_file(root, symbol, 1)
            df = load_ohlcvt(path)
            report["symbols"][symbol] = research_symbol(
                df, symbol, cost_bps=args.cost_bps, days=args.days
            )
            report["symbols"][symbol]["file"] = str(path)
        except Exception as exc:
            report["symbols"][symbol] = {"error": f"{type(exc).__name__}: {exc}"}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({"output": str(out), "symbols": report["symbols"]}, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

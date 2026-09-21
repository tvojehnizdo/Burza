from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_COST_BPS = 14.0


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def find_pair_file(root: Path, symbol: str, interval: int = 1) -> Path:
    token = norm(symbol)
    candidates = []
    for p in root.rglob("*.csv"):
        st = norm(p.stem)
        if token not in st:
            continue
        # Kraken archives commonly encode interval in the filename. Prefer exact
        # endings such as XBTUSD_1.csv but accept other manifest-compatible forms.
        score = 0
        if st == token + str(interval):
            score += 100
        if p.stem.upper().endswith(f"_{interval}"):
            score += 80
        if re.search(rf"(^|[_-]){interval}($|[_-])", p.stem):
            score += 50
        if interval == 1:
            score += 5
        candidates.append((score, -len(str(p)), p))
    if not candidates:
        raise FileNotFoundError(f"No CSV found for {symbol} interval={interval} under {root}")
    candidates.sort(reverse=True)
    return candidates[0][2]


def load_ohlcvt(path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        header=None,
        names=["ts", "open", "high", "low", "close", "volume", "trades"],
        usecols=range(7),
    )
    for c in ["ts", "open", "high", "low", "close", "volume", "trades"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna()
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="s", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")

    # Kraken omits empty intervals. Reindexing prevents time-to-horizon labels
    # from silently treating a multi-minute gap as one bar.
    full = pd.date_range(df.index.min(), df.index.max(), freq="1min", tz="UTC")
    df = df.reindex(full)
    df["close"] = df["close"].ffill()
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0.0)
    df["trades"] = df["trades"].fillna(0.0)
    return df.dropna().reset_index(names="ts")


def ternary(x: pd.Series, threshold: float) -> pd.Series:
    return pd.Series(np.where(x > threshold, 1, np.where(x < -threshold, -1, 0)), index=x.index)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["ret1"] = x.close.pct_change()
    x["ret5_bps"] = x.close.pct_change(5) * 10000.0
    x["ret15_bps"] = x.close.pct_change(15) * 10000.0
    x["ret60_bps"] = x.close.pct_change(60) * 10000.0
    x["ema8"] = x.close.ewm(span=8, adjust=False).mean()
    x["ema24"] = x.close.ewm(span=24, adjust=False).mean()
    x["trend_bps"] = (x.ema8 / x.ema24 - 1.0) * 10000.0
    x["vol15_bps"] = x["ret1"].rolling(15).std(ddof=0) * 10000.0
    medv = x.volume.rolling(30).median()
    x["rv"] = np.where(medv > 0, x.volume / medv, 1.0)
    mu = x.close.rolling(30).mean()
    sd = x.close.rolling(30).std(ddof=0).replace(0, np.nan)
    x["z30"] = (x.close - mu) / sd
    x["hh20"] = x.high.shift(1).rolling(20).max()
    x["ll20"] = x.low.shift(1).rolling(20).min()
    x["breakout"] = np.where(x.close > x.hh20, 1, np.where(x.close < x.ll20, -1, 0))
    return x


def state_key(x: pd.DataFrame) -> pd.Series:
    trend = ternary(x.trend_bps, 5.0)
    r5 = ternary(x.ret5_bps, 8.0)
    r15 = ternary(x.ret15_bps, 15.0)
    btc = ternary(x.btc_ret5_bps, 8.0)
    rel = ternary(x.rel5_bps, 8.0)
    vol = np.where(x.vol15_bps > x.vol15_bps.rolling(240).median().fillna(x.vol15_bps), 1, 0)
    volume = np.where(x.rv > 1.4, 1, 0)
    meanrev = ternary(x.z30.fillna(0), 1.8)
    return (
        "t" + trend.astype(str)
        + "|r" + r5.astype(str)
        + "|q" + r15.astype(str)
        + "|b" + btc.astype(str)
        + "|x" + rel.astype(str)
        + "|k" + pd.Series(x.breakout, index=x.index).astype(int).astype(str)
        + "|v" + pd.Series(vol, index=x.index).astype(str)
        + "|u" + pd.Series(volume, index=x.index).astype(str)
        + "|z" + meanrev.astype(str)
    )


def prepare_symbol(df: pd.DataFrame, btc: pd.DataFrame | None = None) -> pd.DataFrame:
    x = add_features(df)
    if btc is None:
        x["btc_ret5_bps"] = x["ret5_bps"]
    else:
        b = add_features(btc)[["ts", "ret5_bps"]].rename(columns={"ret5_bps": "btc_ret5_bps"})
        x = pd.merge_asof(x.sort_values("ts"), b.sort_values("ts"), on="ts", direction="backward", tolerance=pd.Timedelta("2min"))
        x["btc_ret5_bps"] = x["btc_ret5_bps"].fillna(0.0)
    x["rel5_bps"] = x["ret5_bps"] - x["btc_ret5_bps"]
    x["state_key"] = state_key(x)
    return x


def metrics(vals: np.ndarray, side: str, cost_bps: float) -> dict[str, Any]:
    sign = 1.0 if side == "LONG" else -1.0
    gross = sign * vals
    net = gross - cost_bps
    return {
        "n": int(len(vals)),
        "gross_mean_bps": float(np.mean(gross)),
        "net_mean_bps": float(np.mean(net)),
        "hit_rate": float(np.mean(net > 0)),
        "stdev_bps": float(np.std(gross, ddof=1)) if len(vals) > 1 else 0.0,
    }


def discover(
    x: pd.DataFrame,
    horizon_min: int,
    cost_bps: float,
    min_train: int = 80,
    min_valid: int = 30,
    min_hold: int = 20,
) -> dict[str, Any]:
    y = x.copy()
    y["fwd_bps"] = (y.close.shift(-horizon_min) / y.close - 1.0) * 10000.0
    y = y.dropna(subset=["fwd_bps", "state_key"])
    n = len(y)
    if n < 1000:
        return {"status": "INSUFFICIENT_DATA", "rows": n, "robust": []}

    a, b = int(n * 0.60), int(n * 0.80)
    train, valid, hold = y.iloc[:a], y.iloc[a:b], y.iloc[b:]
    robust = []

    for state, tg in train.groupby("state_key"):
        if len(tg) < min_train:
            continue
        raw = tg.fwd_bps.to_numpy(float)
        side = "LONG" if float(np.mean(raw)) >= 0 else "SHORT"
        tm = metrics(raw, side, cost_bps)

        vg = valid[valid.state_key == state]
        hg = hold[hold.state_key == state]
        if len(vg) < min_valid or len(hg) < min_hold:
            continue
        vm = metrics(vg.fwd_bps.to_numpy(float), side, cost_bps)
        hm = metrics(hg.fwd_bps.to_numpy(float), side, cost_bps)

        if min(tm["net_mean_bps"], vm["net_mean_bps"], hm["net_mean_bps"]) <= 0:
            continue
        if min(tm["hit_rate"], vm["hit_rate"], hm["hit_rate"]) < 0.51:
            continue

        edge = min(tm["net_mean_bps"], vm["net_mean_bps"], hm["net_mean_bps"])
        noise = max(tm["stdev_bps"], vm["stdev_bps"], hm["stdev_bps"], 1.0)
        score = edge * math.sqrt(min(tm["n"], vm["n"], hm["n"])) / noise
        robust.append({
            "state_key": state,
            "side": side,
            "score": round(score, 5),
            "worst_split_edge_bps": round(edge, 4),
            "train": tm,
            "validation": vm,
            "holdout": hm,
        })

    robust.sort(key=lambda z: (z["score"], z["worst_split_edge_bps"]), reverse=True)
    return {
        "status": "ROBUST_EDGE_FOUND" if robust else "NO_ROBUST_EDGE",
        "rows": n,
        "horizon_min": horizon_min,
        "cost_bps": cost_bps,
        "robust": robust[:50],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/kraken_history/full")
    ap.add_argument("--symbols", nargs="+", default=["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD"])
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--out", default="reports/history-alpha.json")
    args = ap.parse_args()

    root = Path(args.root)
    btc_path = find_pair_file(root, "XBTUSD", 1)
    print(f"BTC baseline: {btc_path}")
    btc = load_ohlcvt(btc_path)

    report: dict[str, Any] = {
        "root": str(root),
        "cost_bps": args.cost_bps,
        "symbols": {},
        "robust_summary": [],
    }

    for symbol in args.symbols:
        try:
            path = find_pair_file(root, symbol, 1)
            print(f"{symbol}: {path}")
            df = load_ohlcvt(path)
            x = prepare_symbol(df, None if norm(symbol) == "XBTUSD" else btc)
            item = {"file": str(path), "bars": len(x), "horizons": {}}
            for h in (5, 15, 60):
                res = discover(x, h, args.cost_bps)
                item["horizons"][str(h)] = res
                if res.get("robust"):
                    report["robust_summary"].append({
                        "symbol": symbol, "horizon_min": h,
                        "best": res["robust"][0],
                    })
            report["symbols"][symbol] = item
        except Exception as exc:
            report["symbols"][symbol] = {"error": f"{type(exc).__name__}: {exc}"}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "output": str(out),
        "robust_candidates": len(report["robust_summary"]),
        "best": report["robust_summary"][:10],
    }, indent=2))


if __name__ == "__main__":
    main()

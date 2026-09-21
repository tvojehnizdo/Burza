from __future__ import annotations

import argparse
import json
from typing import Any

import requests

API = "https://api.kraken.com/0/public"
FALLBACK = [
    "BTC/USD","ETH/USD","SOL/USD","XRP/USD","DOGE/USD","ADA/USD",
    "LINK/USD","LTC/USD","BCH/USD","AVAX/USD","DOT/USD","XLM/USD",
]


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = requests.get(API + path, params=params or {}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError("; ".join(body["error"]))
    return body["result"]


def discover(max_pairs: int = 24, max_spread_bps: float = 20.0) -> list[dict[str, Any]]:
    pairs = _get("/AssetPairs")
    tickers = _get("/Ticker")

    rows: list[dict[str, Any]] = []
    for internal, meta in pairs.items():
        ws = str(meta.get("wsname") or "")
        alt = str(meta.get("altname") or "")
        if not ws or "/" not in ws or ".d" in alt.lower():
            continue
        if str(meta.get("status") or "online") != "online":
            continue
        base, quote = ws.split("/", 1)
        if quote not in {"USD","USDC"}:
            continue
        if base in {"USD","USDT","USDC","DAI","USDG","PYUSD","EUR"}:
            continue
        t = tickers.get(internal)
        if not isinstance(t, dict):
            continue
        try:
            bid = float(t["b"][0])
            ask = float(t["a"][0])
            v24 = float(t["v"][1])
            vwap = float(t["p"][1])
        except Exception:
            continue
        if bid <= 0 or ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10000.0
        if spread_bps > max_spread_bps:
            continue
        turnover = v24 * vwap
        if turnover <= 0:
            continue
        rows.append({
            "symbol": ws,
            "base": base,
            "quote": quote,
            "altname": alt,
            "turnover_24h_usd_proxy": turnover,
            "spread_bps": spread_bps,
            "ordermin": meta.get("ordermin"),
            "costmin": meta.get("costmin"),
            "lot_decimals": meta.get("lot_decimals"),
        })

    # Rank by liquidity/spread, but for the same base prefer a USDC quote so
    # existing USDC capital can be deployed without an extra conversion leg.
    rows.sort(
        key=lambda x: (
            x["turnover_24h_usd_proxy"],
            -x["spread_bps"],
            1 if x.get("quote") == "USDC" else 0,
        ),
        reverse=True,
    )
    best_by_base: dict[str, dict[str, Any]] = {}
    for row in rows:
        base = str(row.get("base") or "")
        prev = best_by_base.get(base)
        if prev is None:
            best_by_base[base] = row
            continue
        # If a USDC market exists and is not materially worse on spread/liquidity,
        # prefer it to avoid USDC->USD conversion churn.
        if row.get("quote") == "USDC":
            spread_ok = float(row["spread_bps"]) <= max(float(prev["spread_bps"]) * 1.35, float(prev["spread_bps"]) + 2.0)
            liquidity_ok = float(row["turnover_24h_usd_proxy"]) >= float(prev["turnover_24h_usd_proxy"]) * 0.20
            if spread_ok and liquidity_ok:
                best_by_base[base] = row
    picked = list(best_by_base.values())
    picked.sort(key=lambda x: (x["turnover_24h_usd_proxy"], -x["spread_bps"]), reverse=True)
    return picked[:max_pairs]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=24)
    ap.add_argument("--max-spread-bps", type=float, default=20.0)
    ap.add_argument("--format", choices=["csv","json"], default="csv")
    args = ap.parse_args()

    try:
        rows = discover(max(4, min(args.max, 50)), max(args.max_spread_bps, 1.0))
        symbols = [r["symbol"] for r in rows]
        if len(symbols) < 4:
            raise RuntimeError("Too few eligible Kraken USD pairs")
    except Exception:
        rows = [{"symbol": x, "fallback": True} for x in FALLBACK[:args.max]]
        symbols = [x["symbol"] for x in rows]

    if args.format == "json":
        print(json.dumps({"symbols": symbols, "details": rows}, indent=2))
    else:
        print(",".join(symbols))


if __name__ == "__main__":
    main()

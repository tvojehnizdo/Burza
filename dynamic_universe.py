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

    eligible: list[tuple[str, str, str, str, dict[str, Any]]] = []
    for internal, meta in pairs.items():
        ws = str(meta.get("wsname") or "")
        alt = str(meta.get("altname") or "")
        if not ws or "/" not in ws or not alt or ".d" in alt.lower():
            continue
        if str(meta.get("status") or "online") != "online":
            continue
        base, quote = ws.split("/", 1)
        if quote not in {"USD", "USDC"}:
            continue
        if base in {"USD", "USDT", "USDC", "DAI", "USDG", "PYUSD", "EUR"}:
            continue
        eligible.append((str(internal), alt, ws, base, meta))

    # Kraken can expose different pair aliases in AssetPairs vs Ticker result
    # keys. Query known altnames in batches and accept either key form.
    tickers: dict[str, Any] = {}
    for i in range(0, len(eligible), 40):
        batch = eligible[i:i + 40]
        names = ",".join(x[1] for x in batch)
        try:
            data = _get("/Ticker", {"pair": names})
            if isinstance(data, dict):
                tickers.update(data)
        except Exception:
            continue

    rows: list[dict[str, Any]] = []
    for internal, alt, ws, base, meta in eligible:
        quote = ws.split("/", 1)[1]
        t = tickers.get(internal) or tickers.get(alt)

        if not isinstance(t, dict):
            # Last-resort alias match. This is intentionally conservative and
            # only used when the exact Kraken keys differ.
            compact_ws = ws.replace("/", "").upper()
            candidates = {
                internal.upper(), alt.upper(), compact_ws,
                internal.upper().replace("X", "", 1),
            }
            for key, value in tickers.items():
                ku = str(key).upper()
                if ku in candidates or ku.endswith(alt.upper()):
                    t = value
                    break

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
            "internal": internal,
            "turnover_24h_usd_proxy": turnover,
            "spread_bps": spread_bps,
            "ordermin": meta.get("ordermin"),
            "costmin": meta.get("costmin"),
            "lot_decimals": meta.get("lot_decimals"),
        })

    # Do not collapse USD and USDC into a single base market here. Keeping both
    # allows the account-aware scanner to use whichever quote currency is
    # actually funded.
    rows.sort(
        key=lambda x: (
            1 if x.get("quote") == "USDC" else 0,
            x["turnover_24h_usd_proxy"],
            -x["spread_bps"],
        ),
        reverse=True,
    )

    # Reserve roughly half the slots for funded USDC routes when available and
    # fill the remainder by overall liquidity.
    usdc_rows = [x for x in rows if x.get("quote") == "USDC"]
    usd_rows = [x for x in rows if x.get("quote") == "USD"]
    keep_usdc = min(len(usdc_rows), max(4, max_pairs // 2))
    picked = usdc_rows[:keep_usdc]
    seen = {x["symbol"] for x in picked}
    for row in sorted(
        usd_rows + usdc_rows[keep_usdc:],
        key=lambda x: (x["turnover_24h_usd_proxy"], -x["spread_bps"]),
        reverse=True,
    ):
        if row["symbol"] in seen:
            continue
        picked.append(row)
        seen.add(row["symbol"])
        if len(picked) >= max_pairs:
            break
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

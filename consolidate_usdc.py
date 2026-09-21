from __future__ import annotations

import argparse
import json
from typing import Any

from kraken_private import KrakenPrivate, compact_balances, load_kraken_credentials


TARGET = "USDC"
FIAT_ALIASES = {"USD": {"USD", "ZUSD"}}
ASSET_ALIASES = {
    "BTC": {"XBT", "XXBT", "BTC"},
    "ETH": {"ETH", "XETH"},
    "DOT": {"DOT"},
    "USDC": {"USDC"},
}


def _asset_label(raw: str) -> str:
    u = str(raw).upper().split(".", 1)[0]
    for label, aliases in ASSET_ALIASES.items():
        if u in aliases:
            return label
    if u in FIAT_ALIASES["USD"]:
        return "USD"
    return u.lstrip("XZ")


def _public_pair_rows(client: KrakenPrivate) -> list[dict[str, Any]]:
    pairs = client.public("AssetPairs")
    tickers = client.public("Ticker")
    rows: list[dict[str, Any]] = []
    for internal, meta in pairs.items():
        ws = str(meta.get("wsname") or "")
        alt = str(meta.get("altname") or internal)
        if "/" not in ws or str(meta.get("status") or "online") != "online":
            continue
        base, quote = ws.split("/", 1)
        t = tickers.get(internal) or tickers.get(alt)
        if not isinstance(t, dict):
            continue
        try:
            bid = float(t["b"][0])
            ask = float(t["a"][0])
        except Exception:
            continue
        try:
            ordermin = float(meta.get("ordermin") or 0.0)
        except Exception:
            ordermin = 0.0
        try:
            costmin = float(meta.get("costmin") or 0.0)
        except Exception:
            costmin = 0.0
        rows.append({
            "internal": str(internal),
            "altname": alt,
            "wsname": ws,
            "base": base,
            "quote": quote,
            "bid": bid,
            "ask": ask,
            "ordermin": ordermin,
            "costmin": costmin,
        })
    return rows


def _find_direct_usdc_pair(rows: list[dict[str, Any]], asset_label: str) -> dict[str, Any] | None:
    for row in rows:
        if row["quote"] != TARGET:
            continue
        base_label = _asset_label(row["base"])
        if base_label == asset_label:
            return row
    return None


def plan(client: KrakenPrivate) -> dict[str, Any]:
    balances_raw = compact_balances(client.private("Balance"))
    rows = _public_pair_rows(client)
    steps: list[dict[str, Any]] = []

    for raw_asset, amount in balances_raw.items():
        label = _asset_label(raw_asset)
        if amount <= 0:
            continue
        if label == TARGET:
            steps.append({
                "asset": raw_asset,
                "label": label,
                "amount": amount,
                "action": "KEEP_TARGET",
                "target": TARGET,
                "executable_via_pro": False,
            })
            continue

        if label == "USD":
            steps.append({
                "asset": raw_asset,
                "label": label,
                "amount": amount,
                "action": "FREE_UI_CONVERT_USD_TO_USDC",
                "target": TARGET,
                "executable_via_pro": False,
                "note": "Kraken supports USD<->USDC 1:1 conversion with no spread/fee in Convert UI.",
            })
            continue

        pair = _find_direct_usdc_pair(rows, label)
        if not pair:
            steps.append({
                "asset": raw_asset,
                "label": label,
                "amount": amount,
                "action": "MANUAL_CONVERT_TO_USDC",
                "target": TARGET,
                "executable_via_pro": False,
                "reason": "NO_DIRECT_USDC_PRO_PAIR",
            })
            continue

        notional = amount * float(pair["bid"])
        min_notional = max(float(pair["costmin"]), float(pair["ordermin"]) * float(pair["bid"]))
        eligible = amount >= float(pair["ordermin"]) and notional >= float(pair["costmin"])

        if eligible:
            action = "PRO_SELL_TO_USDC"
        elif notional >= 1.0:
            action = "INSTANT_CONVERT_TO_USDC"
        else:
            action = "SMALL_BALANCE_CONVERT_TO_USDC"

        steps.append({
            "asset": raw_asset,
            "label": label,
            "amount": amount,
            "pair": pair["altname"],
            "wsname": pair["wsname"],
            "bid": pair["bid"],
            "ordermin": pair["ordermin"],
            "costmin": pair["costmin"],
            "minimum_notional_quote": min_notional,
            "estimated_value_usdc": notional,
            "action": action,
            "target": TARGET,
            "executable_via_pro": bool(eligible),
        })

    return {
        "target": TARGET,
        "steps": steps,
        "pro_executable_count": sum(1 for s in steps if s.get("executable_via_pro")),
        "manual_convert_count": sum(
            1 for s in steps
            if s.get("action") in {
                "INSTANT_CONVERT_TO_USDC",
                "SMALL_BALANCE_CONVERT_TO_USDC",
                "MANUAL_CONVERT_TO_USDC",
                "FREE_UI_CONVERT_USD_TO_USDC",
            }
        ),
        "actual_order_submitted": False,
    }


def execute_pro_eligible(client: KrakenPrivate, dry_run: bool = True) -> dict[str, Any]:
    p = plan(client)
    results: list[dict[str, Any]] = []
    for step in p["steps"]:
        if step.get("action") != "PRO_SELL_TO_USDC":
            continue
        payload = {
            "pair": step["pair"],
            "type": "sell",
            "ordertype": "market",
            "volume": f"{float(step['amount']):.12f}",
            "validate": "true" if dry_run else "false",
        }
        try:
            result = client.private("AddOrder", payload)
            results.append({
                "asset": step["asset"],
                "pair": step["pair"],
                "amount": step["amount"],
                "submitted_live": not dry_run,
                "result": result,
            })
        except Exception as exc:
            results.append({
                "asset": step["asset"],
                "pair": step["pair"],
                "amount": step["amount"],
                "submitted_live": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return {
        "target": TARGET,
        "dry_run": dry_run,
        "results": results,
        "remaining_manual_steps": [
            x for x in p["steps"] if x.get("action") != "PRO_SELL_TO_USDC" and x.get("action") != "KEEP_TARGET"
        ],
        "actual_order_submitted": any(bool(x.get("submitted_live")) for x in results),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args()

    if args.execute and args.confirm != "SJEDNOTIT-USDC":
        raise SystemExit("Live consolidation requires --confirm SJEDNOTIT-USDC")

    key, secret, _ = load_kraken_credentials(None)
    client = KrakenPrivate(key, secret)

    if args.execute:
        out = execute_pro_eligible(client, dry_run=False)
    else:
        out = {
            "plan": plan(client),
            "validation": execute_pro_eligible(client, dry_run=True),
        }
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

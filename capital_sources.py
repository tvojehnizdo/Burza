from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_REPORT = Path("reports/kraken-readiness-latest.json")


def fnum(d: dict[str, Any], key: str) -> float | None:
    try:
        if key not in d or d[key] in (None, ""):
            return None
        return float(d[key])
    except Exception:
        return None


def capital_from_readiness(report: dict[str, Any]) -> dict[str, Any]:
    balances = report.get("balance_nonzero") or {}
    tb = report.get("trade_balance") or {}

    equity = fnum(tb, "e")
    trade_balance = fnum(tb, "tb")
    used_margin = fnum(tb, "m")
    free_margin = fnum(tb, "mf")
    margin_level = fnum(tb, "ml")
    unrealized = fnum(tb, "n")

    # Kraken Balance returns wallet balances as fungible balances. If part of a
    # balance came from Flexline/Borrow, Balance alone does not identify which
    # units are borrowed. The debt obligation must therefore be tracked
    # separately from the wallet asset itself.
    stable_assets = {}
    for asset, value in balances.items():
        u = str(asset).upper()
        if u in {"ZUSD","USD","USDC","USDT","USDG","ZEUR","EUR","EURC"}:
            try:
                stable_assets[asset] = float(value)
            except Exception:
                pass

    leverage_capacity = {}
    if free_margin is not None and free_margin > 0:
        for lev in (2, 3, 5):
            leverage_capacity[f"{lev}x"] = round(free_margin * lev, 8)

    return {
        "safe_to_arm": bool(report.get("safe_to_arm")),
        "wallet_balances": balances,
        "stable_wallet_balances": stable_assets,
        "trade_balance_base": trade_balance,
        "equity": equity,
        "used_margin": used_margin,
        "free_margin": free_margin,
        "margin_level_pct": margin_level,
        "unrealized_pnl": unrealized,
        "estimated_margin_notional_by_leverage": leverage_capacity,
        "open_orders_count": report.get("open_orders_count"),
        "open_positions_count": report.get("open_positions_count"),
        "withdrawals": report.get("withdrawals"),
        "capital_policy": {
            "wallet_assets": "DEPLOYABLE_IF_STRATEGY_ACCEPTS_ASSET",
            "flexline_or_borrow_proceeds": "DEPLOYABLE_FROM_MAIN_WALLET_BUT_DEBT_REMAINS_DUE",
            "spot_margin": "DEPLOYABLE_AS_POSITION_CAPACITY_NOT_AS_FREE_WALLET_CASH",
            "withdrawals": "DISABLED",
            "force_trade": False,
        },
        "loan_obligation_visibility": {
            "wallet_api_can_distinguish_borrowed_units": False,
            "note": "Flexline/Borrow proceeds are fungible in the main wallet. Principal, rate and maturity should be tracked separately from Balance.",
        },
        "warning": "Estimated margin notional is free_margin × leverage. Kraken may apply buffers, pair-specific leverage and eligibility limits.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    args = ap.parse_args()
    path = Path(args.report)
    if not path.exists():
        raise SystemExit(f"Readiness report not found: {path}")
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    print(json.dumps(capital_from_readiness(report), indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

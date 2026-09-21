from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from kraken_private import KrakenPrivate, compact_balances, load_kraken_credentials
from futures_private import client_from_env as futures_client_from_env, load_policy as futures_policy

OUTDIR = Path("stav_reporty")


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _count_open_orders(obj: Any) -> int:
    if isinstance(obj, dict):
        if isinstance(obj.get("open"), dict):
            return len(obj["open"])
        if isinstance(obj.get("openOrders"), list):
            return len(obj["openOrders"])
        if isinstance(obj.get("orders"), list):
            return len(obj["orders"])
    return 0


def _spot_snapshot() -> dict[str, Any]:
    key, secret, cred = load_kraken_credentials(None)
    c = KrakenPrivate(key, secret)
    balances = compact_balances(c.private("Balance"))
    tb = c.private("TradeBalance")
    oo = c.private("OpenOrders")
    op = c.private("OpenPositions", {"docalcs": "true"})

    return {
        "credentials_source": cred.get("source"),
        "balances": balances,
        "trade_balance": tb,
        "open_orders_count": _count_open_orders(oo),
        "open_orders": oo.get("open", {}) if isinstance(oo, dict) else {},
        "open_positions_count": len(op) if isinstance(op, dict) else 0,
        "open_positions": op if isinstance(op, dict) else {},
    }


def _futures_snapshot() -> dict[str, Any]:
    try:
        c = futures_client_from_env()
        key_info = c.check_key()
        accounts = c.accounts()
        positions = c.open_positions()
        orders = c.open_orders()
        perms = key_info.get("permissions") or {}
        return {
            "available": True,
            "permissions": {
                "general": perms.get("general"),
                "transfer": perms.get("transfer"),
            },
            "accounts": accounts,
            "open_positions": positions,
            "open_orders": orders,
            "open_orders_count": _count_open_orders(orders),
            "policy": futures_policy(),
        }
    except Exception as exc:
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _trade_metrics(tb: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "trade_balance": "tb",
        "equity": "e",
        "used_margin": "m",
        "free_margin": "mf",
        "margin_level_pct": "ml",
        "unrealized_pnl": "n",
        "cost_basis": "c",
        "valuation": "v",
    }
    return {name: _f(tb.get(key)) for name, key in mapping.items()}


def snapshot() -> dict[str, Any]:
    spot = _spot_snapshot()
    fut = _futures_snapshot()
    metrics = _trade_metrics(spot.get("trade_balance") or {})

    stable = {}
    for asset, value in (spot.get("balances") or {}).items():
        u = str(asset).upper()
        if u in {"USD","ZUSD","USDT","USDC","USDG","EUR","ZEUR","EURC","CZK"}:
            stable[asset] = value

    return {
        "mode": "KRAKEN_ACCOUNT_INVENTORY_READ_ONLY",
        "spot": spot,
        "spot_metrics": metrics,
        "stable_balances": stable,
        "futures": fut,
        "actual_order_submitted": False,
        "withdrawal_or_transfer_action": False,
        "note": (
            "Read-only inventory. Wallet Balance can contain fungible proceeds from borrowing; "
            "this report does not infer which wallet units are borrowed unless Kraken exposes it "
            "through positions/account fields."
        ),
    }


def _txt(report: dict[str, Any]) -> str:
    s = report["spot"]
    m = report["spot_metrics"]
    f = report["futures"]
    lines = [
        "KRAKEN ACCOUNT INVENTORY",
        "==============================================",
        "",
        "SPOT / MAIN WALLET",
    ]
    balances = s.get("balances") or {}
    if balances:
        for asset, value in sorted(balances.items()):
            lines.append(f"  {asset}: {value}")
    else:
        lines.append("  bez nenulovych zustatku")

    lines += [
        "",
        "SPOT / MARGIN",
        f"  trade balance:   {m.get('trade_balance')}",
        f"  equity:          {m.get('equity')}",
        f"  used margin:     {m.get('used_margin')}",
        f"  free margin:     {m.get('free_margin')}",
        f"  margin level %:  {m.get('margin_level_pct')}",
        f"  unrealized PnL:  {m.get('unrealized_pnl')}",
        f"  open orders:     {s.get('open_orders_count')}",
        f"  open positions:  {s.get('open_positions_count')}",
        "",
        "FUTURES",
        f"  available:       {f.get('available')}",
    ]
    if f.get("available"):
        p = f.get("permissions") or {}
        lines += [
            f"  general perm:    {p.get('general')}",
            f"  transfer perm:   {p.get('transfer')}",
            f"  open orders:     {f.get('open_orders_count')}",
            "  accounts:        viz JSON",
            "  positions:       viz JSON",
        ]
    else:
        lines.append(f"  error:           {f.get('error')}")

    lines += [
        "",
        "SAFETY",
        "  actual order submitted: False",
        "  withdrawal/transfer action: False",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out")
    ap.add_argument("--txt-out")
    args = ap.parse_args()

    report = snapshot()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    json_path = Path(args.json_out) if args.json_out else OUTDIR / "kraken_inventory_latest.json"
    txt_path = Path(args.txt_out) if args.txt_out else OUTDIR / "kraken_inventory_latest.txt"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    txt_path.write_text(_txt(report), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "json": str(json_path),
        "txt": str(txt_path),
        "actual_order_submitted": False,
    }, indent=2))


if __name__ == "__main__":
    main()

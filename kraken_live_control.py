from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from kraken_private import KrakenPrivate, load_kraken_credentials

POLICY_PATH = Path(os.getenv("SUPERVISOR_POLICY", "data/supervisor_policy.json"))

DEFAULT_POLICY = {
    "live_execution": False,
    "allow_margin": True,
    "allow_cancel_all": True,
    "max_leverage": 2,
    "max_order_notional_pct_equity": 25.0,
    "max_total_open_orders": 4,
    "withdrawals": False,
    "wallet_transfer": False,
    "require_positive_consensus": True,
}


def load_policy() -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    if POLICY_PATH.exists():
        try:
            policy.update(json.loads(POLICY_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    return policy


def save_policy(patch: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DEFAULT_POLICY)
    current = load_policy()
    for k, v in patch.items():
        if k not in allowed:
            continue
        current[k] = v
    current["withdrawals"] = False
    current["wallet_transfer"] = False
    current["max_leverage"] = max(1, min(int(current.get("max_leverage", 2)), 5))
    current["max_order_notional_pct_equity"] = max(
        0.1, min(float(current.get("max_order_notional_pct_equity", 25.0)), 100.0)
    )
    POLICY_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def client_from_env() -> KrakenPrivate:
    key, secret, _ = load_kraken_credentials(None)
    return KrakenPrivate(key, secret)


def account_snapshot() -> dict[str, Any]:
    client = client_from_env()
    bal = client.private("Balance")
    tb = client.private("TradeBalance")
    oo = client.private("OpenOrders")
    op = client.private("OpenPositions", {"docalcs": "true"})
    return {
        "balances": bal,
        "trade_balance": tb,
        "open_orders": oo.get("open", {}) if isinstance(oo, dict) else {},
        "open_positions": op if isinstance(op, dict) else {},
        "policy": load_policy(),
    }


def _equity_from_trade_balance(tb: dict[str, Any]) -> float:
    for k in ("e", "tb"):
        try:
            v = float(tb.get(k))
            if v > 0:
                return v
        except Exception:
            pass
    return 0.0


def _last_price(client: KrakenPrivate, pair: str) -> float:
    t = client.public("Ticker", {"pair": pair})
    if not t:
        raise RuntimeError(f"No ticker for {pair}")
    row = next(iter(t.values()))
    return float(row["c"][0])


def validate_spot_margin_order(
    pair: str,
    side: str,
    volume: float,
    leverage: int = 1,
    ordertype: str = "market",
) -> dict[str, Any]:
    return place_spot_margin_order(
        pair=pair,
        side=side,
        volume=volume,
        leverage=leverage,
        ordertype=ordertype,
        force_validate=True,
    )


def place_spot_margin_order(
    pair: str,
    side: str,
    volume: float,
    leverage: int = 1,
    ordertype: str = "market",
    force_validate: bool = False,
) -> dict[str, Any]:
    policy = load_policy()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if ordertype not in {"market", "limit"}:
        raise ValueError("Only market/limit are supported by the control plane")
    if volume <= 0:
        raise ValueError("volume must be positive")
    if leverage < 1 or leverage > int(policy["max_leverage"]):
        raise ValueError("Requested leverage exceeds supervisor policy")
    if leverage > 1 and not policy.get("allow_margin", False):
        raise RuntimeError("Margin is disabled by policy")

    client = client_from_env()
    tb = client.private("TradeBalance")
    equity = _equity_from_trade_balance(tb)
    price = _last_price(client, pair)
    estimated_notional = price * float(volume)
    max_notional = equity * float(policy["max_order_notional_pct_equity"]) / 100.0
    if equity > 0 and estimated_notional > max_notional + 1e-9:
        raise RuntimeError(
            f"Order notional {estimated_notional:.8f} exceeds policy max {max_notional:.8f}"
        )

    oo = client.private("OpenOrders")
    open_count = len((oo.get("open") or {})) if isinstance(oo, dict) else 0
    if open_count >= int(policy["max_total_open_orders"]):
        raise RuntimeError("Too many open orders")

    live = bool(policy.get("live_execution")) and not force_validate
    payload: dict[str, Any] = {
        "pair": pair,
        "type": side,
        "ordertype": ordertype,
        "volume": f"{float(volume):.12f}",
        "validate": "false" if live else "true",
    }
    if leverage > 1:
        payload["leverage"] = str(leverage)

    result = client.private("AddOrder", payload)
    return {
        "submitted_live": live,
        "validated_only": not live,
        "pair": pair,
        "side": side,
        "volume": volume,
        "leverage": leverage,
        "estimated_notional_quote": estimated_notional,
        "result": result,
        "withdrawal_capability": False,
        "wallet_transfer_capability": False,
    }


def cancel_all_orders() -> dict[str, Any]:
    policy = load_policy()
    if not policy.get("allow_cancel_all", True):
        raise RuntimeError("Cancel-all is disabled by policy")
    client = client_from_env()
    result = client.private("CancelAll")
    return {"cancelled": result, "protective_action": True}


def set_live_execution(enabled: bool) -> dict[str, Any]:
    # This is intentionally a local policy switch only. It never grants API
    # permissions and never enables withdrawals or wallet transfer.
    return save_policy({"live_execution": bool(enabled)})

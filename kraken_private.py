from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values, load_dotenv

API = "https://api.kraken.com"


class KrakenPrivate:
    def __init__(self, api_key: str, api_secret: str):
        self.key = api_key.strip()
        self.secret = api_secret.strip()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ImpulseMax5K-Readiness/1.0"})
        self._last_nonce = 0

    def _nonce(self) -> str:
        n = int(time.time() * 1000)
        if n <= self._last_nonce:
            n = self._last_nonce + 1
        self._last_nonce = n
        return str(n)

    def _sign(self, path: str, data: dict[str, Any]) -> str:
        encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
        message = path.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def private(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path = f"/0/private/{endpoint}"
        data = dict(payload or {})
        data["nonce"] = self._nonce()
        headers = {
            "API-Key": self.key,
            "API-Sign": self._sign(path, data),
        }
        r = self.session.post(API + path, data=data, headers=headers, timeout=20)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError("; ".join(body["error"]))
        return body.get("result", {})

    def public(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.session.get(f"{API}/0/public/{endpoint}", params=params or {}, timeout=20)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError("; ".join(body["error"]))
        return body.get("result", {})


def load_kraken_credentials(env_file: str | None = None) -> tuple[str, str, dict[str, Any]]:
    source = None
    if env_file:
        path = Path(env_file)
        if not path.exists():
            raise FileNotFoundError(f"Env file not found: {path}")
        load_dotenv(path, override=False)
        source = str(path)

    pairs = [
        ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"),
        ("KRAKEN_KEY", "KRAKEN_SECRET"),
        ("KRAKEN_PUBLIC_KEY", "KRAKEN_PRIVATE_KEY"),
        ("API_KEY_KRAKEN", "API_SECRET_KRAKEN"),
    ]
    for k, s in pairs:
        if os.getenv(k) and os.getenv(s):
            return os.environ[k], os.environ[s], {"source": source or "environment", "key_var": k, "secret_var": s}

    # Fallback: inspect variable names only, never values.
    env = dict(os.environ)
    if env_file:
        env.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    key_names = [k for k in env if "KRAKEN" in k.upper() and ("KEY" in k.upper() or "PUBLIC" in k.upper()) and "SECRET" not in k.upper() and "PRIVATE" not in k.upper()]
    secret_names = [k for k in env if "KRAKEN" in k.upper() and ("SECRET" in k.upper() or "PRIVATE" in k.upper())]
    if len(key_names) == 1 and len(secret_names) == 1:
        return str(env[key_names[0]]), str(env[secret_names[0]]), {"source": source or "environment", "key_var": key_names[0], "secret_var": secret_names[0]}

    raise RuntimeError("Kraken API credentials not found. Expected KRAKEN_API_KEY/KRAKEN_API_SECRET or an equivalent unambiguous Kraken key/secret pair.")


def compact_balances(balance: dict[str, Any]) -> dict[str, float]:
    out = {}
    for asset, value in balance.items():
        try:
            v = float(value)
        except Exception:
            continue
        if abs(v) > 1e-12:
            out[asset] = v
    return out


def find_xbt_pair(client: KrakenPrivate) -> tuple[str, float]:
    pairs = client.public("AssetPairs")
    for _, meta in pairs.items():
        if meta.get("altname") == "XBTUSD":
            minimum = float(meta.get("ordermin") or 0.0001)
            return "XBTUSD", minimum
    return "XBTUSD", 0.0001


def readiness(env_file: str | None = None) -> dict[str, Any]:
    key, secret, cred = load_kraken_credentials(env_file)
    client = KrakenPrivate(key, secret)

    info = client.private("GetApiKeyInfo")
    permissions = set(info.get("permissions") or [])
    withdraw = "withdraw-funds" in permissions
    add_withdraw_addr = any("withdraw" in p and "address" in p for p in permissions)

    checks = {
        "auth_ok": True,
        "query_funds": "query-funds" in permissions,
        "query_open_trades": "query-open-trades" in permissions,
        "modify_trades": "modify-trades" in permissions,
        "withdraw_disabled": not withdraw,
        "withdraw_address_admin_disabled": not add_withdraw_addr,
    }

    balance = {}
    trade_balance = {}
    open_orders = {}
    open_positions = {}
    errors = {}

    for name, endpoint, payload in [
        ("balance", "Balance", {}),
        ("trade_balance", "TradeBalance", {}),
        ("open_orders", "OpenOrders", {}),
        ("open_positions", "OpenPositions", {"docalcs": "true"}),
    ]:
        try:
            result = client.private(endpoint, payload)
            if name == "balance":
                balance = compact_balances(result)
            elif name == "trade_balance":
                trade_balance = result
            elif name == "open_orders":
                open_orders = result
            else:
                open_positions = result
        except Exception as exc:
            errors[name] = str(exc)

    # Safe permission/execution-path test: validate=true means the order is
    # checked by Kraken but never sent to the matching engine.
    validate_order = {"ok": False}
    try:
        pair, minimum = find_xbt_pair(client)
        result = client.private("AddOrder", {
            "pair": pair,
            "type": "buy",
            "ordertype": "market",
            "volume": f"{minimum:.10f}",
            "leverage": "2",
            "validate": "true",
        })
        validate_order = {
            "ok": True,
            "pair": pair,
            "volume": minimum,
            "leverage": "2",
            "validate_only": True,
            "result": result,
        }
    except Exception as exc:
        validate_order = {"ok": False, "validate_only": True, "error": str(exc)}

    checks["validated_margin_order_path"] = bool(validate_order.get("ok"))
    checks["no_private_read_errors"] = not bool(errors)
    checks["required_trading_permissions"] = checks["query_funds"] and checks["query_open_trades"] and checks["modify_trades"]

    safe_to_arm = all([
        checks["auth_ok"],
        checks["withdraw_disabled"],
        checks["withdraw_address_admin_disabled"],
        checks["required_trading_permissions"],
        checks["validated_margin_order_path"],
    ])

    sanitized_info = {
        "apiKeyName": info.get("apiKeyName"),
        "permissions": sorted(permissions),
        "validUntil": info.get("validUntil"),
        "ipAllowlist": info.get("ipAllowlist"),
        "lastUsed": info.get("lastUsed"),
    }

    return {
        "mode": "PRIVATE_READINESS_VALIDATE_ONLY",
        "credentials": cred,
        "checks": checks,
        "safe_to_arm": safe_to_arm,
        "api_key_info": sanitized_info,
        "balance_nonzero": balance,
        "trade_balance": trade_balance,
        "open_orders_count": len((open_orders.get("open") or {})) if isinstance(open_orders, dict) else None,
        "open_positions_count": len(open_positions) if isinstance(open_positions, dict) else None,
        "validate_order": validate_order,
        "errors": errors,
        "withdrawals": "BLOCKED_BY_POLICY_AND_REQUIRED_API_SCOPE",
        "actual_order_submitted": False,
    }


def signature_selftest() -> dict[str, Any]:
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    expected = "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    data = {
        "nonce": "1616492376594",
        "ordertype": "limit",
        "pair": "XBTUSD",
        "price": 37500,
        "type": "buy",
        "volume": 1.25,
    }
    encoded = (str(data["nonce"]) + urllib.parse.urlencode(data)).encode()
    message = b"/0/private/AddOrder" + hashlib.sha256(encoded).digest()
    got = base64.b64encode(hmac.new(base64.b64decode(secret), message, hashlib.sha512).digest()).decode()
    return {"ok": got == expected, "expected": expected, "got": got}


def main() -> None:
    ap = argparse.ArgumentParser(description="Kraken private API readiness check. Never submits a live order.")
    ap.add_argument("--env-file", default=os.getenv("KRAKEN_ENV_FILE"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = readiness(args.env_file)
    print(json.dumps(result, indent=2, default=str))
    if not result["safe_to_arm"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

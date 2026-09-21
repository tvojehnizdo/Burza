from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

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


def _unquote(value: str) -> str:
    v = value.strip().rstrip(",").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
        v = v[1:-1]
    return v.strip()


def parse_env_tolerant(path: Path) -> tuple[dict[str, str], list[dict[str, Any]], list[tuple[int, str]]]:
    """Parse common .env / PowerShell / YAML-ish assignment formats.

    Returns values, assignment metadata and Kraken-context lines. Secret values
    are never logged by callers.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    values: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    context: list[tuple[int, str]] = []

    patterns = [
        re.compile(r"^\s*\$env:([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.+?)\s*$", re.I),
        re.compile(r"^\s*(?:export\s+|set\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.+?)\s*$", re.I),
        re.compile(r"^\\s*[\"']?([A-Za-z_][A-Za-z0-9_.-]*)[\"']?\\s*:\\s*(.+?)\\s*$", re.I),
    ]

    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if "KRAKEN" in stripped.upper():
            context.append((lineno, stripped[:160]))
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        matched = None
        for pat in patterns:
            m = pat.match(raw)
            if m:
                matched = m
                break
        if not matched:
            continue
        name = matched.group(1).strip()
        value = _unquote(matched.group(2))
        if not value or value.lower() in {"null", "none"}:
            continue
        values[name] = value
        entries.append({"name": name, "line": lineno})

    return values, entries, context


def _is_secret_name(name: str) -> bool:
    u = name.upper()
    return "SECRET" in u or "PRIVATE" in u


def _is_key_name(name: str) -> bool:
    u = name.upper()
    return ("KEY" in u or "PUBLIC" in u) and not _is_secret_name(name)


def _pick_by_context(values: dict[str, str], entries: list[dict[str, Any]], context: list[tuple[int, str]]) -> tuple[str | None, str | None]:
    # Prefer variables explicitly named for Kraken.
    kraken_keys = [e["name"] for e in entries if "KRAKEN" in e["name"].upper() and _is_key_name(e["name"])]
    kraken_secrets = [e["name"] for e in entries if "KRAKEN" in e["name"].upper() and _is_secret_name(e["name"])]
    if len(set(kraken_keys)) == 1 and len(set(kraken_secrets)) == 1:
        return kraken_keys[0], kraken_secrets[0]

    # Support vaults that use a Kraken heading followed by generic API_KEY /
    # API_SECRET variables. Only consider a tight local window.
    kraken_lines = [line for line, _ in context]
    near = []
    for e in entries:
        if any(abs(int(e["line"]) - line) <= 10 for line in kraken_lines):
            near.append(e["name"])
    near_keys = [n for n in near if _is_key_name(n)]
    near_secrets = [n for n in near if _is_secret_name(n)]
    if len(set(near_keys)) == 1 and len(set(near_secrets)) == 1:
        return near_keys[0], near_secrets[0]

    return None, None


def inspect_env_file(env_file: str) -> dict[str, Any]:
    path = Path(env_file)
    if not path.exists():
        raise FileNotFoundError(f"Env file not found: {path}")
    values, entries, context = parse_env_tolerant(path)
    key_name, secret_name = _pick_by_context(values, entries, context)
    # Sanitized diagnostic: names/line numbers only, never values.
    return {
        "file": str(path),
        "assignment_count": len(entries),
        "kraken_context_lines": [line for line, _ in context],
        "candidate_variable_names": [e["name"] for e in entries if "KRAKEN" in e["name"].upper()],
        "selected_key_var": key_name,
        "selected_secret_var": secret_name,
        "values_printed": False,
    }


def load_kraken_credentials(env_file: str | None = None) -> tuple[str, str, dict[str, Any]]:
    source = None
    parsed: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    context: list[tuple[int, str]] = []

    if env_file:
        path = Path(env_file)
        if not path.exists():
            raise FileNotFoundError(f"Env file not found: {path}")
        parsed, entries, context = parse_env_tolerant(path)
        source = str(path)

    env = dict(os.environ)
    env.update(parsed)

    pairs = [
        ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"),
        ("KRAKEN_KEY", "KRAKEN_SECRET"),
        ("KRAKEN_PUBLIC_KEY", "KRAKEN_PRIVATE_KEY"),
        ("API_KEY_KRAKEN", "API_SECRET_KRAKEN"),
        ("KRAKEN_APIKEY", "KRAKEN_APISECRET"),
    ]
    for k, s in pairs:
        if env.get(k) and env.get(s):
            return str(env[k]), str(env[s]), {
                "source": source or "environment",
                "key_var": k,
                "secret_var": s,
                "parser": "tolerant",
            }

    key_name, secret_name = _pick_by_context(env, entries, context)
    if key_name and secret_name and env.get(key_name) and env.get(secret_name):
        return str(env[key_name]), str(env[secret_name]), {
            "source": source or "environment",
            "key_var": key_name,
            "secret_var": secret_name,
            "parser": "tolerant-context",
        }

    # Last safe fallback: unambiguous Kraken-labelled environment variables.
    key_names = [k for k in env if "KRAKEN" in k.upper() and _is_key_name(k)]
    secret_names = [k for k in env if "KRAKEN" in k.upper() and _is_secret_name(k)]
    if len(set(key_names)) == 1 and len(set(secret_names)) == 1:
        k, s = key_names[0], secret_names[0]
        return str(env[k]), str(env[s]), {
            "source": source or "environment",
            "key_var": k,
            "secret_var": s,
            "parser": "tolerant-name-inference",
        }

    diag = inspect_env_file(env_file) if env_file else {
        "candidate_variable_names": [],
        "kraken_context_lines": [],
        "values_printed": False,
    }
    raise RuntimeError(
        "Kraken API credentials not found after tolerant parsing. "
        f"Sanitized diagnostic: {json.dumps(diag, ensure_ascii=False)}"
    )


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
        "close_trades": "close-trades" in permissions,
        "create_ws_token": "create-ws-token" in permissions,
        "withdraw_disabled": not withdraw,
        "withdraw_address_admin_disabled": not add_withdraw_addr,
    }

    balance = {}
    trade_balance = {}
    open_orders = {}
    open_positions = {}
    errors = {}
    ws_token_ok = False

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

    try:
        client.private("GetWebSocketsToken")
        ws_token_ok = True
    except Exception as exc:
        errors["websocket_token"] = str(exc)

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
    checks["websocket_token_ok"] = ws_token_ok
    checks["no_private_read_errors"] = not bool(errors)
    checks["required_trading_permissions"] = (
        checks["query_funds"]
        and checks["query_open_trades"]
        and checks["modify_trades"]
        and checks["close_trades"]
        and checks["create_ws_token"]
    )

    safe_to_arm = all([
        checks["auth_ok"],
        checks["withdraw_disabled"],
        checks["withdraw_address_admin_disabled"],
        checks["required_trading_permissions"],
        checks["validated_margin_order_path"],
        checks["websocket_token_ok"],
        checks["no_private_read_errors"],
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
    ap.add_argument("--inspect-env", action="store_true", help="Print sanitized variable-name diagnostics only.")
    args = ap.parse_args()
    if args.inspect_env:
        if not args.env_file:
            raise SystemExit("--inspect-env requires --env-file")
        print(json.dumps(inspect_env_file(args.env_file), indent=2, default=str))
        return
    result = readiness(args.env_file)
    print(json.dumps(result, indent=2, default=str))
    if not result["safe_to_arm"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

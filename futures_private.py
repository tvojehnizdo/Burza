from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

BASE = "https://futures.kraken.com"
POLICY_PATH = Path(os.getenv("FUTURES_POLICY", "data/futures_policy.json"))

MIN_LOT_BY_ROOT = {
    "XBTUSD": 0.0001,
    "ETHUSD": 0.001,
    "SOLUSD": 0.01,
}

DEFAULT_POLICY = {
    "live_execution": False,
    "allowed_roots": ["XBTUSD", "ETHUSD", "SOLUSD"],
    "max_order_notional_pct_equity": 10.0,
    "max_order_notional_usd": 15.0,
    "max_open_positions": 2,
    "deadman_timeout_s": 60,
    "require_transfer_no_access": True,
}


class KrakenFutures:
    def __init__(self, api_key: str, api_secret: str):
        self.key = api_key.strip()
        self.secret = api_secret.strip()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "ImpulseMax5K-Futures/2.0"})

    def _auth(self, post_data: str, nonce: str, endpoint_path: str) -> str:
        digest = hashlib.sha256((post_data + nonce + endpoint_path).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret), digest, hashlib.sha512).digest()
        return base64.b64encode(mac).decode()

    def _request_url(self, method: str, url_path: str, auth_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        nonce = str(int(time.time() * 1000))
        post_data = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        headers = {
            "APIKey": self.key,
            "apikey": self.key,
            "Authent": self._auth(post_data, nonce, auth_path),
            "authent": self._auth(post_data, nonce, auth_path),
            "Nonce": nonce,
            "Accept": "application/json",
        }
        url = BASE + url_path
        if method.upper() == "GET":
            r = self.s.get(url, params=params, headers=headers, timeout=20)
        else:
            r = self.s.post(url, data=post_data, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        r.raise_for_status()
        body = r.json()
        if body.get("result") == "error":
            raise RuntimeError(str(body.get("error") or body.get("errors") or body))
        return body

    def request(self, method: str, endpoint_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_url(method, "/derivatives" + endpoint_path, endpoint_path, params)

    def check_key(self) -> dict[str, Any]:
        path = "/api/auth/v1/api-keys/v3/check"
        return self._request_url("GET", path, path, {})

    def accounts(self) -> dict[str, Any]:
        return self.request("GET", "/api/v3/accounts")

    def open_positions(self) -> dict[str, Any]:
        return self.request("GET", "/api/v3/openpositions")

    def open_orders(self) -> dict[str, Any]:
        return self.request("GET", "/api/v3/openorders")

    def tickers(self) -> dict[str, Any]:
        r = self.s.get(BASE + "/derivatives/api/v3/tickers", timeout=20)
        r.raise_for_status()
        return r.json()

    def send_order(self, symbol: str, side: str, size: float, order_type: str = "mkt", reduce_only: bool = False) -> dict[str, Any]:
        return self.request("POST", "/api/v3/sendOrder", {
            "orderType": order_type,
            "symbol": symbol,
            "side": side,
            "size": size,
            "reduceOnly": str(bool(reduce_only)).lower(),
        })

    def deadman(self, timeout_s: int) -> dict[str, Any]:
        return self.request("POST", "/api/v3/cancelallordersafter", {"timeout": int(timeout_s)})


def load_policy() -> dict[str, Any]:
    p = dict(DEFAULT_POLICY)
    if POLICY_PATH.exists():
        try:
            p.update(json.loads(POLICY_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    p["live_execution"] = bool(p.get("live_execution", False))
    p["max_order_notional_pct_equity"] = min(max(float(p.get("max_order_notional_pct_equity", 10.0)), 0.1), 25.0)
    p["max_order_notional_usd"] = min(max(float(p.get("max_order_notional_usd", 15.0)), 1.0), 100.0)
    p["max_open_positions"] = min(max(int(p.get("max_open_positions", 2)), 1), 4)
    p["deadman_timeout_s"] = min(max(int(p.get("deadman_timeout_s", 60)), 20), 120)
    allowed = p.get("allowed_roots") or DEFAULT_POLICY["allowed_roots"]
    p["allowed_roots"] = [str(x).upper() for x in allowed if str(x).upper() in MIN_LOT_BY_ROOT]
    if not p["allowed_roots"]:
        p["allowed_roots"] = list(DEFAULT_POLICY["allowed_roots"])
    p["require_transfer_no_access"] = True
    return p


def save_policy(patch: dict[str, Any]) -> dict[str, Any]:
    current = load_policy()
    for key, value in patch.items():
        if key in DEFAULT_POLICY:
            current[key] = value
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    current = load_policy()
    POLICY_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def client_from_env() -> KrakenFutures:
    key = os.getenv("KRAKEN_FUTURES_API_KEY", "").strip()
    secret = os.getenv("KRAKEN_FUTURES_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_API_SECRET not configured")
    return KrakenFutures(key, secret)


def _root(symbol: str) -> str | None:
    s = str(symbol).upper()
    if s.startswith("PF_"):
        return s[3:]
    if s.startswith("FF_"):
        return s[3:].split("_", 1)[0]
    return None


def _position_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("openPositions") or []
    return [x for x in rows if isinstance(x, dict)]


def position_map(payload: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in _position_rows(payload):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            size = float(row.get("size") or 0.0)
        except Exception:
            continue
        side = str(row.get("side") or "").lower()
        out[symbol] = -abs(size) if side == "short" else abs(size)
    return out


def _flex_equity_usd(accounts_payload: dict[str, Any]) -> float:
    # Account layout can differ between Flex / multi-collateral configurations.
    # Search known equity-like fields recursively instead of requiring one wallet layout.
    wanted = ("marginEquity", "portfolioValue", "collateralValue", "balanceValue")
    best = 0.0

    def walk(obj: Any) -> None:
        nonlocal best
        if isinstance(obj, dict):
            for key in wanted:
                try:
                    value = float(obj.get(key) or 0.0)
                except Exception:
                    value = 0.0
                if value > best:
                    best = value
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                if isinstance(value, (dict, list)):
                    walk(value)

    walk(accounts_payload.get("accounts") or accounts_payload)
    return best


def _ticker_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("tickers") or []
    return {str(x.get("symbol") or "").upper(): x for x in rows if isinstance(x, dict) and x.get("symbol")}


def _mid_price(ticker: dict[str, Any] | None) -> float | None:
    if not ticker:
        return None
    try:
        bid = float(ticker.get("bid"))
        ask = float(ticker.get("ask"))
        if bid > 0 and ask >= bid:
            return (bid + ask) / 2.0
    except Exception:
        pass
    for key in ("markPrice", "last", "indexPrice"):
        try:
            v = float(ticker.get(key))
            if v > 0:
                return v
        except Exception:
            continue
    return None


def min_lot(symbol: str) -> float:
    root = _root(symbol)
    if root not in MIN_LOT_BY_ROOT:
        raise RuntimeError(f"Unsupported live root for {symbol}")
    return float(MIN_LOT_BY_ROOT[root])


def round_size_down(symbol: str, size: float) -> float:
    step = min_lot(symbol)
    units = math.floor((float(size) + 1e-15) / step)
    rounded = units * step
    decimals = max(0, len(str(step).split(".")[1].rstrip("0")) if "." in str(step) else 0)
    return round(rounded, decimals)


def order_preflight(symbol: str, side: str, size: float, reduce_only: bool = False, client: KrakenFutures | None = None) -> dict[str, Any]:
    p = load_policy()
    s = str(symbol).upper()
    root = _root(s)
    if root not in set(p["allowed_roots"]):
        raise RuntimeError(f"Symbol root {root} is outside live allow-list")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy/sell")
    if size <= 0:
        raise ValueError("size must be positive")

    minimum = min_lot(s)
    rounded = round_size_down(s, size)
    if rounded < minimum - 1e-15:
        raise RuntimeError(f"Requested size {size} is below min lot {minimum} for {s}")
    if abs(rounded - float(size)) > max(minimum * 1e-6, 1e-12):
        raise RuntimeError(f"Requested size {size} is not aligned to min-lot step {minimum}; use {rounded}")

    c = client or client_from_env()
    key_info = c.check_key()
    perms = key_info.get("permissions") or {}
    general = str(perms.get("general") or "").upper()
    transfer = str(perms.get("transfer") or "").upper()
    if general != "FULL_ACCESS":
        raise RuntimeError("Futures API key does not have FULL_ACCESS trading permission")
    if transfer != "NO_ACCESS":
        raise RuntimeError("Futures API key transfer permission must be NO_ACCESS")

    tickers = _ticker_map(c.tickers())
    px = _mid_price(tickers.get(s))
    if px is None:
        raise RuntimeError(f"No usable live ticker for {s}")
    notional = float(size) * px

    accounts = c.accounts()
    equity = _flex_equity_usd(accounts)
    abs_cap = float(p["max_order_notional_usd"])
    pct_cap = equity * float(p["max_order_notional_pct_equity"]) / 100.0 if equity > 0 else abs_cap
    cap = min(abs_cap, pct_cap) if pct_cap > 0 else abs_cap
    if not reduce_only and notional > cap + 1e-9:
        raise RuntimeError(f"Order notional USD {notional:.4f} exceeds live cap USD {cap:.4f}")

    positions = _position_rows(c.open_positions())
    if not reduce_only and len(positions) >= int(p["max_open_positions"]):
        raise RuntimeError(f"Open position count {len(positions)} reached cap {p['max_open_positions']}")

    return {
        "ok": True,
        "symbol": s,
        "root": root,
        "side": side,
        "size": float(size),
        "min_lot": minimum,
        "mid_price": px,
        "estimated_notional_usd": notional,
        "equity_usd": equity,
        "notional_cap_usd": cap,
        "open_positions": len(positions),
        "reduce_only": bool(reduce_only),
        "general_permission": general,
        "transfer_permission": transfer,
    }


def readiness() -> dict[str, Any]:
    c = client_from_env()
    key_info = c.check_key()
    perms = key_info.get("permissions") or {}
    general = str(perms.get("general") or "").upper()
    transfer = str(perms.get("transfer") or "").upper()
    accounts = c.accounts()
    positions = c.open_positions()
    open_orders = c.open_orders()
    policy = load_policy()
    equity = _flex_equity_usd(accounts)
    rows = _position_rows(positions)
    safe = general == "FULL_ACCESS" and transfer == "NO_ACCESS" and equity > 0 and len(rows) <= int(policy["max_open_positions"])
    return {
        "ok": True,
        "safe_to_arm": safe,
        "key_permissions": {"general": general, "transfer": transfer},
        "equity_usd": equity,
        "open_position_count": len(rows),
        "open_positions": positions,
        "open_orders": open_orders,
        "policy": policy,
        "live_execution": bool(policy.get("live_execution")),
        "withdrawal_or_transfer_capability_requested": False,
    }


def place_order(symbol: str, side: str, size: float, reduce_only: bool = False) -> dict[str, Any]:
    p = load_policy()
    if not p.get("live_execution"):
        return {
            "submitted_live": False,
            "reason": "futures live_execution policy is false",
            "symbol": symbol,
            "side": side,
            "size": size,
        }
    c = client_from_env()
    preflight = order_preflight(symbol, side, size, reduce_only=reduce_only, client=c)
    c.deadman(int(p.get("deadman_timeout_s", 60)))
    result = c.send_order(str(symbol).upper(), side, float(size), order_type="mkt", reduce_only=reduce_only)
    return {"submitted_live": True, "preflight": preflight, "result": result}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readiness", action="store_true")
    args = ap.parse_args()
    if args.readiness:
        r = readiness()
        print(json.dumps(r, indent=2, default=str))
        if not r.get("safe_to_arm"):
            raise SystemExit(2)


if __name__ == "__main__":
    main()

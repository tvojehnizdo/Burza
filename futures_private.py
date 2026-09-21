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

BASE = "https://futures.kraken.com"
POLICY_PATH = Path(os.getenv("FUTURES_POLICY", "data/futures_policy.json"))

DEFAULT_POLICY = {
    "live_execution": False,
    "max_order_notional_pct_equity": 25.0,
    "max_open_orders": 4,
    "deadman_timeout_s": 60,
}


class KrakenFutures:
    def __init__(self, api_key: str, api_secret: str):
        self.key = api_key.strip()
        self.secret = api_secret.strip()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "ImpulseMax5K-Futures/1.0"})

    def _auth(self, post_data: str, nonce: str, endpoint_path: str) -> str:
        digest = hashlib.sha256((post_data + nonce + endpoint_path).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret), digest, hashlib.sha512).digest()
        return base64.b64encode(mac).decode()

    def request(self, method: str, endpoint_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        nonce = str(int(time.time() * 1000))
        post_data = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        headers = {
            "APIKey": self.key,
            "Authent": self._auth(post_data, nonce, endpoint_path),
            "Nonce": nonce,
            "Accept": "application/json",
        }
        url = BASE + "/derivatives" + endpoint_path
        if method.upper() == "GET":
            r = self.s.get(url, params=params, headers=headers, timeout=20)
        else:
            r = self.s.post(url, data=post_data, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        r.raise_for_status()
        body = r.json()
        if body.get("result") == "error":
            raise RuntimeError(str(body.get("error") or body.get("errors") or body))
        return body

    def accounts(self) -> dict[str, Any]:
        return self.request("GET", "/api/v3/accounts")

    def open_positions(self) -> dict[str, Any]:
        return self.request("GET", "/api/v3/openpositions")

    def send_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "mkt",
        reduce_only: bool = False,
    ) -> dict[str, Any]:
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
    return p


def client_from_env() -> KrakenFutures:
    key = os.getenv("KRAKEN_FUTURES_API_KEY", "").strip()
    secret = os.getenv("KRAKEN_FUTURES_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_API_SECRET not configured")
    return KrakenFutures(key, secret)


def readiness() -> dict[str, Any]:
    c = client_from_env()
    accounts = c.accounts()
    positions = c.open_positions()
    return {
        "ok": True,
        "accounts": accounts,
        "open_positions": positions,
        "live_execution": bool(load_policy().get("live_execution")),
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
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy/sell")
    if size <= 0:
        raise ValueError("size must be positive")
    c = client_from_env()
    # Safety: arm a dead-man switch before every live futures order.
    c.deadman(int(p.get("deadman_timeout_s", 60)))
    result = c.send_order(symbol, side, size, order_type="mkt", reduce_only=reduce_only)
    return {"submitted_live": True, "result": result}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readiness", action="store_true")
    args = ap.parse_args()
    if args.readiness:
        print(json.dumps(readiness(), indent=2, default=str))


if __name__ == "__main__":
    main()

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
from functools import lru_cache
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

FALLBACK_TICK_BY_ROOT = {
    "XBTUSD": 1.0,
    "ETHUSD": 0.1,
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

    def instruments(self) -> dict[str, Any]:
        r = self.s.get(BASE + "/derivatives/api/v3/instruments", timeout=20)
        r.raise_for_status()
        return r.json()

    def send_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "mkt",
        reduce_only: bool = False,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trigger_signal: str | None = None,
        cli_ord_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "orderType": order_type,
            "symbol": symbol,
            "side": side,
            "size": size,
            "reduceOnly": str(bool(reduce_only)).lower(),
        }
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if trigger_signal:
            payload["triggerSignal"] = trigger_signal
        if cli_ord_id:
            payload["cliOrdId"] = cli_ord_id
        return self.request("POST", "/api/v3/sendorder", payload)

    def deadman(self, timeout_s: int) -> dict[str, Any]:
        return self.request("POST", "/api/v3/cancelallordersafter", {"timeout": int(timeout_s)})

    def cancel_all_orders(self) -> dict[str, Any]:
        return self.request("POST", "/api/v3/cancelallorders", {})


def load_policy() -> dict[str, Any]:
    p = dict(DEFAULT_POLICY)
    if POLICY_PATH.exists():
        try:
            p.update(json.loads(POLICY_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    p["live_execution"] = bool(p.get("live_execution", False))
    p["max_order_notional_pct_equity"] = min(max(float(p.get("max_order_notional_pct_equity", 10.0)), 0.1), 35.0)
    p["max_order_notional_usd"] = min(max(float(p.get("max_order_notional_usd", 15.0)), 1.0), 100.0)
    p["max_open_positions"] = min(max(int(p.get("max_open_positions", 2)), 1), 4)
    p["deadman_timeout_s"] = min(max(int(p.get("deadman_timeout_s", 60)), 20), 120)
    allowed = [str(x).upper() for x in (p.get("allowed_roots") or DEFAULT_POLICY["allowed_roots"])]
    if "*" in allowed:
        p["allowed_roots"] = ["*"]
    else:
        p["allowed_roots"] = [x for x in allowed if x]
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


@lru_cache(maxsize=1)
def instrument_specs() -> dict[str, dict[str, Any]]:
    s = requests.Session()
    s.headers.update({"User-Agent": "ImpulseMax5K-Futures/2.1"})
    r = s.get(BASE + "/derivatives/api/v3/instruments", timeout=20)
    r.raise_for_status()
    body = r.json()
    rows = body.get("instruments") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            precision = int(float(row.get("contractValueTradePrecision") or 0))
        except Exception:
            precision = 0
        qty_step = 10.0 ** (-max(0, precision))
        try:
            tick = float(row.get("tickSize") or 0.0)
        except Exception:
            tick = 0.0
        try:
            contract_size = float(row.get("contractSize") or 1.0)
        except Exception:
            contract_size = 1.0
        out[symbol] = {
            "symbol": symbol,
            "type": str(row.get("type") or ""),
            "underlying": str(row.get("underlying") or ""),
            "tradeable": bool(row.get("tradeable", False)),
            "tick_size": tick,
            "qty_step": qty_step,
            "contract_size": contract_size,
            "precision": precision,
            "category": str(row.get("category") or ""),
            "tags": row.get("tags") or [],
        }
    return out


def instrument_spec(symbol: str) -> dict[str, Any]:
    s = str(symbol).upper()
    try:
        spec = instrument_specs().get(s)
    except Exception:
        spec = None
    if spec:
        return spec
    root = _root(s)
    if root in MIN_LOT_BY_ROOT:
        return {
            "symbol": s,
            "tradeable": True,
            "tick_size": FALLBACK_TICK_BY_ROOT[root],
            "qty_step": MIN_LOT_BY_ROOT[root],
            "contract_size": 1.0,
            "precision": max(0, len(str(MIN_LOT_BY_ROOT[root]).split(".")[1].rstrip("0")) if "." in str(MIN_LOT_BY_ROOT[root]) else 0),
        }
    raise RuntimeError(f"No Kraken instrument spec for {s}")


def min_lot(symbol: str) -> float:
    return float(instrument_spec(symbol)["qty_step"])


def contract_size(symbol: str) -> float:
    value = float(instrument_spec(symbol).get("contract_size") or 1.0)
    return value if value > 0 else 1.0


def tick_size(symbol: str) -> float:
    tick = float(instrument_spec(symbol).get("tick_size") or 0.0)
    if tick <= 0:
        raise RuntimeError(f"No positive tick size for {symbol}")
    return tick


def round_price_to_tick(symbol: str, price: float, mode: str = "nearest") -> float:
    tick = tick_size(symbol)
    units = float(price) / tick
    if mode == "down":
        units = math.floor(units + 1e-12)
    elif mode == "up":
        units = math.ceil(units - 1e-12)
    else:
        units = round(units)
    value = units * tick
    decimals = max(0, len(str(tick).split(".")[1].rstrip("0")) if "." in str(tick) else 0)
    return round(value, decimals)


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
    spec = instrument_spec(s)
    if not s.startswith("PF_") or not bool(spec.get("tradeable")):
        raise RuntimeError(f"{s} is not a tradeable perpetual Futures instrument")
    allowed = set(p["allowed_roots"])
    if "*" not in allowed and root not in allowed:
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
    notional = float(size) * px * contract_size(s)

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


def place_order(
    symbol: str,
    side: str,
    size: float,
    reduce_only: bool = False,
    order_type: str = "mkt",
    limit_price: float | None = None,
    stop_price: float | None = None,
    trigger_signal: str | None = None,
    cli_ord_id: str | None = None,
    use_deadman: bool = False,
) -> dict[str, Any]:
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
    if use_deadman:
        c.deadman(int(p.get("deadman_timeout_s", 60)))
    result = c.send_order(
        str(symbol).upper(),
        side,
        float(size),
        order_type=order_type,
        reduce_only=reduce_only,
        limit_price=limit_price,
        stop_price=stop_price,
        trigger_signal=trigger_signal,
        cli_ord_id=cli_ord_id,
    )
    send_status = result.get("sendStatus") or {}
    status = str(send_status.get("status") or "").strip()
    accepted = status.lower() in {"placed", "filled"}
    return {
        "submitted_live": bool(accepted),
        "request_sent": True,
        "exchange_status": status,
        "preflight": preflight,
        "result": result,
    }


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

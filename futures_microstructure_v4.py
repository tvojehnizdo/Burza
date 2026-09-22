from __future__ import annotations

import json
import math
from typing import Any

import requests

ORDERBOOK_URL = "https://futures.kraken.com/derivatives/api/v3/orderbook"
BOOK_LEVELS = 8
BOOK_CONFIRM_IMBALANCE = 0.10
BOOK_VETO_IMBALANCE = -0.30
BOOK_MAX_SPREAD_BPS = 20.0


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def orderbook_profile(symbol: str, direction: str) -> dict[str, Any]:
    symbol = str(symbol).upper()
    direction = str(direction).upper()
    try:
        r = requests.get(ORDERBOOK_URL, params={"symbol": symbol}, timeout=8)
        r.raise_for_status()
        book = r.json().get("orderBook") or {}
        bids = [x for x in (book.get("bids") or []) if isinstance(x, list) and len(x) >= 2]
        asks = [x for x in (book.get("asks") or []) if isinstance(x, list) and len(x) >= 2]
    except Exception as exc:
        return {
            "available": False,
            "symbol": symbol,
            "direction": direction,
            "error": f"{type(exc).__name__}: {exc}",
        }

    bids = sorted(
        [(_num(x[0]), abs(_num(x[1]))) for x in bids if _num(x[0]) > 0 and _num(x[1]) > 0],
        key=lambda z: z[0],
        reverse=True,
    )
    asks = sorted(
        [(_num(x[0]), abs(_num(x[1]))) for x in asks if _num(x[0]) > 0 and _num(x[1]) > 0],
        key=lambda z: z[0],
    )
    if not bids or not asks:
        return {
            "available": False,
            "symbol": symbol,
            "direction": direction,
            "error": "EMPTY_ORDERBOOK_SIDE",
        }

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask / best_bid - 1.0) * 10000.0 if best_bid > 0 else 9999.0

    top_bids = bids[:BOOK_LEVELS]
    top_asks = asks[:BOOK_LEVELS]
    bid_proxy = sum(px * qty for px, qty in top_bids)
    ask_proxy = sum(px * qty for px, qty in top_asks)
    total = bid_proxy + ask_proxy
    imbalance = (bid_proxy - ask_proxy) / total if total > 0 else 0.0

    # Near-touch depth is more relevant to a small account than deep-book size.
    band = 10.0 / 10000.0
    near_bid = sum(px * qty for px, qty in bids if px >= mid * (1.0 - band))
    near_ask = sum(px * qty for px, qty in asks if px <= mid * (1.0 + band))
    near_total = near_bid + near_ask
    near_imbalance = (near_bid - near_ask) / near_total if near_total > 0 else imbalance

    aligned = near_imbalance if direction == "LONG" else -near_imbalance
    veto = bool(aligned <= BOOK_VETO_IMBALANCE)
    confirmed = bool(aligned >= BOOK_CONFIRM_IMBALANCE and spread_bps <= BOOK_MAX_SPREAD_BPS)

    return {
        "available": True,
        "symbol": symbol,
        "direction": direction,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": round(spread_bps, 4),
        "top_levels": BOOK_LEVELS,
        "imbalance": round(imbalance, 4),
        "near_10bps_imbalance": round(near_imbalance, 4),
        "aligned_imbalance": round(aligned, 4),
        "confirmed": confirmed,
        "veto": veto,
    }


def leader_alignment(rows: list[dict[str, Any]], symbol: str, direction: str) -> dict[str, Any]:
    symbol = str(symbol).upper()
    direction = str(direction).upper()
    if symbol in {"PF_XBTUSD", "PF_ETHUSD"}:
        return {"alignment": 0, "leaders": {}, "reason": "LEADER_SYMBOL"}

    leaders: dict[str, str] = {}
    for leader in ("PF_XBTUSD", "PF_ETHUSD"):
        row = next((x for x in rows if str(x.get("symbol") or "").upper() == leader), None)
        if not row:
            continue
        side = str(row.get("side") or "").upper()
        if side in {"LONG", "SHORT"} and bool(row.get("trend_persistent")):
            leaders[leader] = side

    if len(leaders) < 2:
        return {"alignment": 0, "leaders": leaders, "reason": "INSUFFICIENT_LEADERS"}

    vals = list(leaders.values())
    if vals[0] != vals[1]:
        return {"alignment": 0, "leaders": leaders, "reason": "LEADERS_SPLIT"}

    same = vals[0] == direction
    return {
        "alignment": 1 if same else -1,
        "leaders": leaders,
        "reason": "LEADERS_CONFIRM" if same else "LEADERS_OPPOSE",
    }


def selftest() -> dict[str, Any]:
    rows = [
        {"symbol": "PF_XBTUSD", "side": "LONG", "trend_persistent": True},
        {"symbol": "PF_ETHUSD", "side": "LONG", "trend_persistent": True},
        {"symbol": "PF_SOLUSD", "side": "LONG", "trend_persistent": True},
    ]
    a = leader_alignment(rows, "PF_SOLUSD", "LONG")
    b = leader_alignment(rows, "PF_SOLUSD", "SHORT")
    checks = {
        "leaders_confirm": a["alignment"] == 1,
        "leaders_oppose": b["alignment"] == -1,
        "threshold_order": BOOK_VETO_IMBALANCE < 0 < BOOK_CONFIRM_IMBALANCE,
        "levels_positive": BOOK_LEVELS > 0,
    }
    return {"ok": all(checks.values()), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(selftest(), indent=2))

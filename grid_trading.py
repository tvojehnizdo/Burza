"""
grid_trading.py - Grid trading logic for sideways markets.

Grid strategy
-------------
A grid is centred on the current mid-price and has GRID_LEVELS levels above
and below, spaced GRID_SPACING_PCT apart.  Buy limit orders are placed below
the mid-price; sell limit orders above.  When an order fills the bot places a
new order on the opposite side one level further away, locking in the spread.

State is persisted to a JSON file so the grid survives bot restarts.
"""

import json
import logging
import os
import time
from typing import Optional

import ccxt

import config
from exchanges import (
    cancel_order,
    fetch_open_orders,
    fetch_ticker,
    place_limit_order,
)

logger = logging.getLogger(__name__)

_STATE_FILE = "logs/grid_state.json"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


# ---------------------------------------------------------------------------
# Grid management
# ---------------------------------------------------------------------------

def _build_grid_prices(mid_price: float) -> dict:
    """Return dict with 'buys' and 'sells' price lists centred on mid_price."""
    spacing = config.GRID_SPACING_PCT
    levels = config.GRID_LEVELS
    buys = [mid_price * (1 - spacing * (i + 1)) for i in range(levels)]
    sells = [mid_price * (1 + spacing * (i + 1)) for i in range(levels)]
    return {"buys": buys, "sells": sells}


def setup_grid(exchange: ccxt.Exchange, symbol: str) -> dict:
    """
    Cancel any existing grid orders and place a fresh grid centred on the
    current mid-price.

    Returns the new grid state dict.
    """
    # Cancel old orders
    try:
        open_orders = fetch_open_orders(exchange, symbol)
        for order in open_orders:
            try:
                cancel_order(exchange, order["id"], symbol)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Could not cancel old orders: %s", exc)

    ticker = fetch_ticker(exchange, symbol)
    mid_price = ticker.get("last") or (
        (ticker.get("bid", 0) + ticker.get("ask", 0)) / 2
    )
    if not mid_price:
        raise ValueError(f"Cannot determine mid-price for {symbol}")

    grid = _build_grid_prices(mid_price)
    order_size_usd = config.GRID_ORDER_SIZE_USD

    placed_orders: list = []

    for price in grid["buys"]:
        amount = order_size_usd / price
        try:
            market = exchange.market(symbol)
            amount = float(exchange.amount_to_precision(symbol, amount))
        except Exception:
            pass
        if amount > 0:
            try:
                order = place_limit_order(exchange, symbol, "buy", amount, price)
                placed_orders.append(
                    {
                        "id": order["id"],
                        "side": "buy",
                        "price": price,
                        "amount": amount,
                    }
                )
            except Exception as exc:
                logger.warning("Could not place grid buy @ %.4f: %s", price, exc)

    for price in grid["sells"]:
        amount = order_size_usd / price
        try:
            market = exchange.market(symbol)
            amount = float(exchange.amount_to_precision(symbol, amount))
        except Exception:
            pass
        if amount > 0:
            try:
                order = place_limit_order(exchange, symbol, "sell", amount, price)
                placed_orders.append(
                    {
                        "id": order["id"],
                        "side": "sell",
                        "price": price,
                        "amount": amount,
                    }
                )
            except Exception as exc:
                logger.warning("Could not place grid sell @ %.4f: %s", price, exc)

    state = {
        "symbol": symbol,
        "exchange": exchange.id,
        "mid_price": mid_price,
        "created_at": time.time(),
        "orders": placed_orders,
    }
    _save_state(state)
    logger.info(
        "Grid set up for %s on %s | mid=%.4f | %d orders placed",
        symbol,
        exchange.id,
        mid_price,
        len(placed_orders),
    )
    return state


def check_grid_fills(exchange: ccxt.Exchange, symbol: str) -> list:
    """
    Compare tracked grid orders against open orders on the exchange.
    For each order that has been filled, log it and place a counter-order.

    Returns a list of dicts describing each completed fill.
    """
    state = _load_state()
    if not state or state.get("symbol") != symbol:
        return []

    try:
        open_order_ids = {o["id"] for o in fetch_open_orders(exchange, symbol)}
    except Exception as exc:
        logger.warning("Could not fetch open orders: %s", exc)
        return []

    fills = []
    remaining_orders = []

    for order in state.get("orders", []):
        if order["id"] in open_order_ids:
            remaining_orders.append(order)
            continue

        # Order is no longer open → it was filled (or cancelled)
        logger.info(
            "Grid order FILLED: %s %s %.6f @ %.4f",
            order["side"],
            symbol,
            order["amount"],
            order["price"],
        )

        # Place counter-order
        counter_side = "sell" if order["side"] == "buy" else "buy"
        spacing = config.GRID_SPACING_PCT
        if counter_side == "sell":
            counter_price = order["price"] * (1 + spacing)
        else:
            counter_price = order["price"] * (1 - spacing)

        try:
            new_order = place_limit_order(
                exchange, symbol, counter_side, order["amount"], counter_price
            )
            remaining_orders.append(
                {
                    "id": new_order["id"],
                    "side": counter_side,
                    "price": counter_price,
                    "amount": order["amount"],
                }
            )
            logger.info(
                "Counter-order placed: %s %.6f @ %.4f",
                counter_side,
                order["amount"],
                counter_price,
            )
        except Exception as exc:
            logger.warning("Could not place counter-order: %s", exc)

        fills.append(
            {
                "type": "grid_fill",
                "symbol": symbol,
                "exchange": exchange.id,
                "side": order["side"],
                "amount": order["amount"],
                "price": order["price"],
                "profit_per_unit": order["price"] * (spacing - config.BINANCE_FEE * 2),
                "timestamp": time.time(),
            }
        )

    state["orders"] = remaining_orders
    _save_state(state)
    return fills


def needs_refresh(last_grid_time: float) -> bool:
    """Return True if the grid should be rebuilt (price has drifted too far)."""
    return (time.time() - last_grid_time) >= config.GRID_REFRESH_INTERVAL_SEC

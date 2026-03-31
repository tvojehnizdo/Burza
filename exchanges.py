"""
exchanges.py - Binance and Kraken connection management via ccxt.
"""

import os
import logging
import ccxt
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _build_exchange(exchange_id: str, api_key: str, api_secret: str) -> ccxt.Exchange:
    """Instantiate and test connectivity for an exchange."""
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    return exchange


def get_binance() -> ccxt.Exchange:
    """Return an authenticated Binance exchange instance."""
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env")
    exchange = _build_exchange("binance", api_key, api_secret)
    logger.info("Binance exchange initialised")
    return exchange


def get_kraken() -> ccxt.Exchange:
    """Return an authenticated Kraken exchange instance."""
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")
    if not api_key or not api_secret:
        raise ValueError("KRAKEN_API_KEY and KRAKEN_API_SECRET must be set in .env")
    exchange = _build_exchange("kraken", api_key, api_secret)
    logger.info("Kraken exchange initialised")
    return exchange


def fetch_ticker(exchange: ccxt.Exchange, symbol: str) -> dict:
    """
    Fetch the latest ticker for *symbol* from *exchange*.
    Returns a dict with at minimum 'bid', 'ask', 'last', 'symbol'.
    """
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker
    except ccxt.NetworkError as exc:
        logger.warning("Network error fetching %s from %s: %s", symbol, exchange.id, exc)
        raise
    except ccxt.ExchangeError as exc:
        logger.error("Exchange error fetching %s from %s: %s", symbol, exchange.id, exc)
        raise


def fetch_balance(exchange: ccxt.Exchange) -> dict:
    """Return the full balance dict for the exchange."""
    try:
        return exchange.fetch_balance()
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error("Error fetching balance from %s: %s", exchange.id, exc)
        raise


def place_market_order(
    exchange: ccxt.Exchange, symbol: str, side: str, amount: float
) -> dict:
    """
    Place a market order.

    Parameters
    ----------
    exchange : ccxt.Exchange
    symbol  : str   e.g. 'BTC/USDT'
    side    : str   'buy' or 'sell'
    amount  : float Base currency amount (e.g. BTC quantity)

    Returns
    -------
    dict  Order response from the exchange
    """
    try:
        order = exchange.create_market_order(symbol, side, amount)
        logger.info(
            "[%s] %s %s %.6f  →  order id %s",
            exchange.id,
            side.upper(),
            symbol,
            amount,
            order.get("id"),
        )
        return order
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(
            "Order failed on %s (%s %s %.6f): %s", exchange.id, side, symbol, amount, exc
        )
        raise


def place_limit_order(
    exchange: ccxt.Exchange, symbol: str, side: str, amount: float, price: float
) -> dict:
    """
    Place a limit order.

    Parameters
    ----------
    exchange : ccxt.Exchange
    symbol  : str
    side    : str   'buy' or 'sell'
    amount  : float Base currency amount
    price   : float Limit price in quote currency

    Returns
    -------
    dict  Order response from the exchange
    """
    try:
        order = exchange.create_limit_order(symbol, side, amount, price)
        logger.info(
            "[%s] LIMIT %s %s %.6f @ %.4f  →  order id %s",
            exchange.id,
            side.upper(),
            symbol,
            amount,
            price,
            order.get("id"),
        )
        return order
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(
            "Limit order failed on %s (%s %s %.6f @ %.4f): %s",
            exchange.id,
            side,
            symbol,
            amount,
            price,
            exc,
        )
        raise


def cancel_order(exchange: ccxt.Exchange, order_id: str, symbol: str) -> dict:
    """Cancel an open order by id."""
    try:
        result = exchange.cancel_order(order_id, symbol)
        logger.info("[%s] Cancelled order %s (%s)", exchange.id, order_id, symbol)
        return result
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.warning("Could not cancel order %s on %s: %s", order_id, exchange.id, exc)
        raise


def fetch_open_orders(exchange: ccxt.Exchange, symbol: str) -> list:
    """Return list of open orders for *symbol*."""
    try:
        return exchange.fetch_open_orders(symbol)
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error("Error fetching open orders from %s: %s", exchange.id, exc)
        raise

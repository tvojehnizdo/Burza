"""
arbitrage.py - Arbitrage detection and execution between Binance and Kraken.

Algorithm
---------
1. Fetch ask price on Binance (buy side) and bid price on Kraken (sell side).
2. Fetch ask price on Kraken and bid price on Binance for the reverse direction.
3. Calculate net profit after fees + slippage for both directions.
4. If net profit > ARBITRAGE_MIN_PROFIT_PCT, execute the trade.
"""

import logging
import time
from typing import Optional

import ccxt

import config
from exchanges import fetch_ticker, place_market_order

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _total_fee(pct_a: float, pct_b: float) -> float:
    """Combined round-trip fee for two exchanges (buy on A, sell on B)."""
    return pct_a + pct_b + 2 * config.SLIPPAGE_PCT


def _net_profit_pct(buy_price: float, sell_price: float, fee_total: float) -> float:
    """Return net profit as a fraction of the buy price."""
    gross = (sell_price - buy_price) / buy_price
    return gross - fee_total


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_arbitrage(
    binance: ccxt.Exchange,
    kraken: ccxt.Exchange,
    symbol: str,
) -> Optional[dict]:
    """
    Check whether a profitable arbitrage opportunity exists for *symbol*.

    Returns a dict describing the opportunity, or None if no opportunity.

    Returned dict keys
    ------------------
    direction : str          'binance_to_kraken' or 'kraken_to_binance'
    buy_exchange             ccxt.Exchange instance to buy on
    sell_exchange            ccxt.Exchange instance to sell on
    buy_price  : float       Ask price on the buying exchange
    sell_price : float       Bid price on the selling exchange
    net_profit_pct : float   Net profit as a fraction (e.g. 0.005 = 0.5%)
    symbol     : str
    """
    try:
        binance_ticker = fetch_ticker(binance, symbol)
        kraken_ticker = fetch_ticker(kraken, symbol)
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.warning("Could not fetch tickers for %s: %s", symbol, exc)
        return None

    binance_ask = binance_ticker.get("ask")
    binance_bid = binance_ticker.get("bid")
    kraken_ask = kraken_ticker.get("ask")
    kraken_bid = kraken_ticker.get("bid")

    if None in (binance_ask, binance_bid, kraken_ask, kraken_bid):
        logger.warning("Incomplete ticker data for %s, skipping", symbol)
        return None

    fee_total = _total_fee(config.BINANCE_FEE, config.KRAKEN_FEE)
    min_profit = config.ARBITRAGE_MIN_PROFIT_PCT

    # Direction 1: Buy on Binance, sell on Kraken
    profit_b2k = _net_profit_pct(binance_ask, kraken_bid, fee_total)
    # Direction 2: Buy on Kraken, sell on Binance
    profit_k2b = _net_profit_pct(kraken_ask, binance_bid, fee_total)

    best_profit = max(profit_b2k, profit_k2b)
    if best_profit < min_profit:
        logger.debug(
            "%s – no arbitrage (best=%.4f%% < min=%.4f%%)",
            symbol,
            best_profit * 100,
            min_profit * 100,
        )
        return None

    if profit_b2k >= profit_k2b:
        direction = "binance_to_kraken"
        buy_exchange = binance
        sell_exchange = kraken
        buy_price = binance_ask
        sell_price = kraken_bid
        net_profit_pct = profit_b2k
    else:
        direction = "kraken_to_binance"
        buy_exchange = kraken
        sell_exchange = binance
        buy_price = kraken_ask
        sell_price = binance_bid
        net_profit_pct = profit_k2b

    logger.info(
        "ARBITRAGE DETECTED %s | direction=%s | buy=%.4f sell=%.4f | net=%.4f%%",
        symbol,
        direction,
        buy_price,
        sell_price,
        net_profit_pct * 100,
    )
    return {
        "direction": direction,
        "buy_exchange": buy_exchange,
        "sell_exchange": sell_exchange,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "net_profit_pct": net_profit_pct,
        "symbol": symbol,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_arbitrage(
    opportunity: dict,
    trade_size_usd: float,
) -> Optional[dict]:
    """
    Execute an arbitrage trade.

    Parameters
    ----------
    opportunity   : dict  Result from detect_arbitrage()
    trade_size_usd: float USD value to trade (respects risk limits)

    Returns
    -------
    dict with trade details, or None if execution failed.
    """
    symbol = opportunity["symbol"]
    buy_exchange: ccxt.Exchange = opportunity["buy_exchange"]
    sell_exchange: ccxt.Exchange = opportunity["sell_exchange"]
    buy_price: float = opportunity["buy_price"]

    # Convert USD size to base currency amount
    amount = trade_size_usd / buy_price

    # Round amount to exchange precision
    try:
        market = buy_exchange.market(symbol)
        amount = buy_exchange.amount_to_precision(symbol, amount)
        amount = float(amount)
    except Exception:
        pass

    if amount <= 0:
        logger.warning("Calculated trade amount is zero for %s, skipping", symbol)
        return None

    buy_order = None
    sell_order = None

    try:
        logger.info(
            "Executing BUY %.6f %s on %s", amount, symbol, buy_exchange.id
        )
        buy_order = place_market_order(buy_exchange, symbol, "buy", amount)

        logger.info(
            "Executing SELL %.6f %s on %s", amount, symbol, sell_exchange.id
        )
        sell_order = place_market_order(sell_exchange, symbol, "sell", amount)

    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error("Arbitrage execution failed: %s", exc)
        # If buy succeeded but sell failed, we have an unhedged position.
        # Log clearly for manual intervention.
        if buy_order and not sell_order:
            logger.critical(
                "UNHEDGED POSITION: Bought %.6f %s on %s but sell on %s FAILED. "
                "Manual intervention required!",
                amount,
                symbol,
                buy_exchange.id,
                sell_exchange.id,
            )
        return None

    net_profit_usd = trade_size_usd * opportunity["net_profit_pct"]
    result = {
        "type": "arbitrage",
        "symbol": symbol,
        "direction": opportunity["direction"],
        "amount": amount,
        "buy_exchange": buy_exchange.id,
        "sell_exchange": sell_exchange.id,
        "buy_price": buy_price,
        "sell_price": opportunity["sell_price"],
        "trade_size_usd": trade_size_usd,
        "net_profit_pct": opportunity["net_profit_pct"],
        "net_profit_usd": net_profit_usd,
        "buy_order_id": buy_order.get("id"),
        "sell_order_id": sell_order.get("id"),
        "timestamp": opportunity["timestamp"],
    }
    logger.info(
        "Arbitrage complete | profit=%.4f USD (%.4f%%)",
        net_profit_usd,
        opportunity["net_profit_pct"] * 100,
    )
    return result

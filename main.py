"""
main.py - 24/7 crypto trading bot main loop.

Run:
    python main.py

Stop:
    Ctrl+C  (the bot saves state and exits cleanly)
"""

import logging
import signal
import sys
import threading
import time

import ccxt

import config
from exchanges import fetch_balance, get_binance, get_kraken, place_market_order
from arbitrage import detect_arbitrage, execute_arbitrage
from grid_trading import check_grid_fills, needs_refresh, setup_grid
from risk_manager import RiskManager
from logger import MetricsTracker, log_trade, setup_logging

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_stop_event = threading.Event()

def _handle_signal(signum, frame):  # noqa: ANN001
    _stop_event.set()
    print("\n[Bot] Shutdown signal received. Exiting cleanly…")


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ---------------------------------------------------------------------------
# Balance helpers
# ---------------------------------------------------------------------------

def _usdc_balance(exchange: ccxt.Exchange) -> float:
    """Return the free USDC (or USDT) balance on the exchange."""
    try:
        bal = fetch_balance(exchange)
        free = bal.get("free", {})
        return float(free.get("USDC", 0) or free.get("USDT", 0) or 0)
    except Exception:
        return 0.0


def _total_usd_balance(binance: ccxt.Exchange, kraken: ccxt.Exchange) -> float:
    return _usdc_balance(binance) + _usdc_balance(kraken)


# ---------------------------------------------------------------------------
# Reconnection wrapper
# ---------------------------------------------------------------------------

def _with_retry(fn, *args, retries: int = 3, delay: float = 5.0, **kwargs):
    """Call *fn* up to *retries* times with *delay* seconds between attempts."""
    last_exc = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            last_exc = exc
            logging.warning("Network error (attempt %d/%d): %s", attempt + 1, retries, exc)
            time.sleep(delay)
        except ccxt.ExchangeError as exc:
            # Non-transient errors should not be retried
            raise exc from None
    raise last_exc


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    log = logging.getLogger("main")
    log.info("Starting 24/7 Crypto Trading Bot")
    log.info("Pairs: %s", config.TRADING_PAIRS)
    log.info(
        "Risk: max_trade=$%.0f | stop_loss=%.0f%% | daily_limit=$%.0f",
        config.MAX_TRADE_SIZE_USD,
        config.STOP_LOSS_PCT * 100,
        config.DAILY_LOSS_LIMIT_USD,
    )

    # Connect to exchanges
    try:
        binance = get_binance()
        kraken = get_kraken()
        log.info("Connected to Binance and Kraken")
    except ValueError as exc:
        log.critical("Exchange init failed: %s", exc)
        sys.exit(1)

    risk = RiskManager()
    metrics = MetricsTracker()

    last_grid_setup: dict = {}   # {symbol: timestamp}
    grid_exchange = binance       # Grid trading runs on Binance (larger balance)

    iteration = 0
    dashboard_interval = 60      # Print dashboard every N seconds
    last_dashboard_time = 0.0

    log.info("Bot is running. Press Ctrl+C to stop.")

    while not _stop_event.is_set():
        iteration += 1
        loop_start = time.time()

        try:
            # ---------------------------------------------------------------
            # 1. Fetch current prices
            # ---------------------------------------------------------------
            current_prices: dict = {}
            for symbol in config.TRADING_PAIRS:
                try:
                    ticker = _with_retry(binance.fetch_ticker, symbol)
                    current_prices[symbol] = ticker.get("last") or ticker.get("ask", 0)
                except Exception as exc:
                    log.warning("Could not fetch price for %s: %s", symbol, exc)

            # ---------------------------------------------------------------
            # 2. Check stop-losses for any open positions
            # ---------------------------------------------------------------
            triggered = risk.check_stop_losses(current_prices)
            for symbol in triggered:
                price = current_prices.get(symbol, 0)
                log.warning("Closing position %s at stop-loss price %.4f", symbol, price)
                for pos in list(risk.open_positions):
                    if pos["symbol"] == symbol:
                        try:
                            _with_retry(
                                place_market_order,
                                binance,
                                symbol,
                                "sell",
                                pos["amount"],
                            )
                            pnl = (price - pos["entry_price"]) * pos["amount"]
                            risk.close_position(symbol, price, pnl)
                        except Exception as exc:
                            log.error("Stop-loss sell failed for %s: %s", symbol, exc)

            # ---------------------------------------------------------------
            # 3. Strategy A: Arbitrage detection & execution
            # ---------------------------------------------------------------
            if risk.status()["trading_allowed"]:
                available_usd = _total_usd_balance(binance, kraken)

                for symbol in config.TRADING_PAIRS:
                    if _stop_event.is_set():
                        break
                    try:
                        opportunity = _with_retry(
                            detect_arbitrage, binance, kraken, symbol
                        )
                    except Exception as exc:
                        log.warning("Arbitrage check failed for %s: %s", symbol, exc)
                        continue

                    if opportunity is None:
                        continue

                    trade_size = risk.clamp_trade_size(
                        config.MAX_TRADE_SIZE_USD, available_usd
                    )
                    if not risk.can_trade(trade_size, available_usd):
                        break

                    try:
                        trade = execute_arbitrage(opportunity, trade_size)
                    except Exception as exc:
                        log.error("Arbitrage execution error for %s: %s", symbol, exc)
                        continue

                    if trade:
                        log_trade(trade)
                        metrics.record(trade)
                        risk.record_trade_result(trade.get("net_profit_usd", 0))
                        available_usd -= trade_size  # rough balance update

            # ---------------------------------------------------------------
            # 4. Strategy B: Grid trading (fallback on primary exchange)
            # ---------------------------------------------------------------
            if risk.status()["trading_allowed"]:
                grid_symbol = config.TRADING_PAIRS[0]   # Trade grid on first pair

                last_setup = last_grid_setup.get(grid_symbol, 0.0)
                if needs_refresh(last_setup):
                    try:
                        available = _usdc_balance(grid_exchange)
                        if available > config.EMERGENCY_RESERVE_USD + config.GRID_ORDER_SIZE_USD:
                            setup_grid(grid_exchange, grid_symbol)
                            last_grid_setup[grid_symbol] = time.time()
                    except Exception as exc:
                        log.warning("Grid setup failed: %s", exc)

                try:
                    fills = check_grid_fills(grid_exchange, grid_symbol)
                    for fill in fills:
                        log_trade(fill)
                        metrics.record(fill)
                except Exception as exc:
                    log.warning("Grid fill check failed: %s", exc)

            # ---------------------------------------------------------------
            # 5. Periodic dashboard
            # ---------------------------------------------------------------
            now = time.time()
            if now - last_dashboard_time >= dashboard_interval:
                metrics.print_dashboard(risk.status())
                last_dashboard_time = now

        except Exception as exc:
            log.exception("Unexpected error in main loop: %s", exc)
            time.sleep(10)
            continue

        # Wait until next iteration
        elapsed = time.time() - loop_start
        sleep_time = max(0.0, config.PRICE_CHECK_INTERVAL_SEC - elapsed)
        if sleep_time > 0 and not _stop_event.is_set():
            time.sleep(sleep_time)

    log.info("Bot stopped. Final metrics:")
    metrics.print_dashboard(risk.status())


if __name__ == "__main__":
    main()

"""
risk_manager.py - Position sizing, stop-loss enforcement and daily loss limits.
"""

import logging
import time

import config

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Central risk management object shared between strategies.

    Attributes
    ----------
    daily_loss_usd        : float  Accumulated loss for today (positive = loss)
    open_positions        : list   Currently open positions
    session_start_time    : float  Unix timestamp of bot start (used to reset daily counters)
    """

    def __init__(self) -> None:
        self.daily_loss_usd: float = 0.0
        self.open_positions: list = []
        self._session_date: str = _today()

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def _maybe_reset_daily(self) -> None:
        today = _today()
        if today != self._session_date:
            logger.info("New day detected – resetting daily loss counter")
            self.daily_loss_usd = 0.0
            self._session_date = today

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def can_trade(self, trade_size_usd: float, available_balance_usd: float) -> bool:
        """
        Return True if all risk checks pass and the trade is safe to place.

        Checks
        ------
        1. Daily loss limit not reached
        2. Max concurrent positions not reached
        3. Trade size within maximum
        4. Sufficient balance minus emergency reserve
        """
        self._maybe_reset_daily()

        if self.daily_loss_usd >= config.DAILY_LOSS_LIMIT_USD:
            logger.warning(
                "Daily loss limit reached (%.2f >= %.2f). No more trades today.",
                self.daily_loss_usd,
                config.DAILY_LOSS_LIMIT_USD,
            )
            return False

        if len(self.open_positions) >= config.MAX_CONCURRENT_POSITIONS:
            logger.warning(
                "Max concurrent positions reached (%d). Skipping trade.",
                config.MAX_CONCURRENT_POSITIONS,
            )
            return False

        if trade_size_usd > config.MAX_TRADE_SIZE_USD:
            logger.warning(
                "Trade size %.2f > max %.2f. Clamping.",
                trade_size_usd,
                config.MAX_TRADE_SIZE_USD,
            )
            return False

        usable_balance = available_balance_usd - config.EMERGENCY_RESERVE_USD
        if trade_size_usd > usable_balance:
            logger.warning(
                "Insufficient usable balance (%.2f after %.2f reserve). Trade=%.2f",
                usable_balance,
                config.EMERGENCY_RESERVE_USD,
                trade_size_usd,
            )
            return False

        return True

    def clamp_trade_size(self, requested_usd: float, available_usd: float) -> float:
        """Return the largest safe trade size (<= requested_usd)."""
        usable = available_usd - config.EMERGENCY_RESERVE_USD
        return min(requested_usd, config.MAX_TRADE_SIZE_USD, max(usable, 0))

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------

    def open_position(self, trade: dict) -> None:
        """Register a newly opened position."""
        buy_price = trade.get("buy_price")
        if not buy_price:
            logger.error(
                "Cannot open position for %s: buy_price missing in trade record",
                trade.get("symbol"),
            )
            return
        position = {
            "symbol": trade.get("symbol"),
            "entry_price": buy_price,
            "amount": trade.get("amount"),
            "trade_size_usd": trade.get("trade_size_usd"),
            "stop_loss_price": buy_price * (1 - config.STOP_LOSS_PCT),
            "opened_at": time.time(),
            "trade": trade,
        }
        self.open_positions.append(position)
        logger.info(
            "Position opened: %s | entry=%.4f | stop=%.4f",
            position["symbol"],
            position["entry_price"],
            position["stop_loss_price"],
        )

    def close_position(self, symbol: str, exit_price: float, pnl_usd: float) -> None:
        """Mark a position as closed and update the daily loss counter."""
        self.open_positions = [
            p for p in self.open_positions if p["symbol"] != symbol
        ]
        if pnl_usd < 0:
            self.daily_loss_usd += abs(pnl_usd)
            logger.warning(
                "Position closed at loss: %s | loss=%.4f USD | daily_loss=%.4f USD",
                symbol,
                abs(pnl_usd),
                self.daily_loss_usd,
            )
        else:
            logger.info(
                "Position closed at profit: %s | profit=%.4f USD",
                symbol,
                pnl_usd,
            )

    def record_trade_result(self, pnl_usd: float) -> None:
        """Update daily loss counter for a completed trade (arbitrage / grid fill)."""
        self._maybe_reset_daily()
        if pnl_usd < 0:
            self.daily_loss_usd += abs(pnl_usd)

    # ------------------------------------------------------------------
    # Stop-loss checks
    # ------------------------------------------------------------------

    def check_stop_losses(self, current_prices: dict) -> list:
        """
        Check all open positions against their stop-loss prices.

        Parameters
        ----------
        current_prices : dict  {symbol: current_price}

        Returns
        -------
        list of symbols that hit their stop-loss (caller must close them)
        """
        triggered = []
        for position in self.open_positions:
            symbol = position["symbol"]
            price = current_prices.get(symbol)
            if price is None:
                continue
            if price <= position["stop_loss_price"]:
                logger.warning(
                    "STOP-LOSS triggered: %s | current=%.4f <= stop=%.4f",
                    symbol,
                    price,
                    position["stop_loss_price"],
                )
                triggered.append(symbol)
        return triggered

    # ------------------------------------------------------------------
    # Status summary
    # ------------------------------------------------------------------

    def status(self) -> dict:
        self._maybe_reset_daily()
        return {
            "daily_loss_usd": self.daily_loss_usd,
            "daily_loss_limit_usd": config.DAILY_LOSS_LIMIT_USD,
            "open_positions": len(self.open_positions),
            "max_positions": config.MAX_CONCURRENT_POSITIONS,
            "trading_allowed": self.daily_loss_usd < config.DAILY_LOSS_LIMIT_USD,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    from datetime import date
    return date.today().isoformat()

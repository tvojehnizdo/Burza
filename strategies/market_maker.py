"""Market making strategy."""
import logging
from typing import Dict, Optional
from .base import BaseStrategy

logger = logging.getLogger(__name__)


class MarketMakerStrategy(BaseStrategy):
    """Market maker strategy that provides liquidity and profits from spread."""
    
    def __init__(self, spread_percent: float = 0.5, order_size: float = 50):
        """Initialize market maker strategy.
        
        Args:
            spread_percent: Target spread percentage
            order_size: Size of each order in USDT
        """
        super().__init__('MarketMaker')
        self.spread_percent = spread_percent
        self.order_size = order_size
    
    def analyze(self, exchanges: Dict, symbol: str) -> Optional[Dict]:
        """Analyze market and place buy/sell orders.
        
        Args:
            exchanges: Dict of exchange name to exchange instance
            symbol: Trading pair symbol
        
        Returns:
            Trading signal for market making
        """
        if not self.enabled:
            return None
        
        if not exchanges:
            return None
        
        try:
            # Use first available exchange for market making
            exchange_name = list(exchanges.keys())[0]
            exchange = exchanges[exchange_name]
            
            # Get current market price
            ticker = exchange.get_ticker(symbol)
            mid_price = (ticker['bid'] + ticker['ask']) / 2
            
            # Calculate buy and sell prices with spread
            buy_price = mid_price * (1 - self.spread_percent / 200)
            sell_price = mid_price * (1 + self.spread_percent / 200)
            
            logger.info(
                f"Market making on {exchange_name}: "
                f"Buy at {buy_price:.2f}, Sell at {sell_price:.2f}, "
                f"Spread: {self.spread_percent}%"
            )
            
            return {
                'strategy': self.name,
                'action': 'market_make',
                'exchange': exchange_name,
                'symbol': symbol,
                'buy_price': buy_price,
                'sell_price': sell_price,
                'amount': self.order_size / mid_price,  # Convert USDT to base currency
                'mid_price': mid_price
            }
            
        except Exception as e:
            logger.error(f"Error in market maker analysis: {e}")
            return None

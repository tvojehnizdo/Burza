"""Arbitrage trading strategy."""
import logging
from typing import Dict, Optional
from .base import BaseStrategy

logger = logging.getLogger(__name__)


class ArbitrageStrategy(BaseStrategy):
    """Arbitrage strategy that profits from price differences between exchanges."""
    
    def __init__(self, min_profit_threshold: float = 0.5, max_trade_amount: float = 100):
        """Initialize arbitrage strategy.
        
        Args:
            min_profit_threshold: Minimum profit percentage to execute trade
            max_trade_amount: Maximum amount to trade in USDT
        """
        super().__init__('Arbitrage')
        self.min_profit_threshold = min_profit_threshold
        self.max_trade_amount = max_trade_amount
    
    def analyze(self, exchanges: Dict, symbol: str) -> Optional[Dict]:
        """Analyze price differences between exchanges.
        
        Args:
            exchanges: Dict of exchange name to exchange instance
            symbol: Trading pair symbol
        
        Returns:
            Trading signal if arbitrage opportunity exists
        """
        if not self.enabled:
            return None
        
        if len(exchanges) < 2:
            return None
        
        try:
            # Get prices from all exchanges
            prices = {}
            for name, exchange in exchanges.items():
                try:
                    ticker = exchange.get_ticker(symbol)
                    prices[name] = {
                        'bid': ticker['bid'],
                        'ask': ticker['ask'],
                        'exchange': exchange
                    }
                except Exception as e:
                    logger.warning(f"Failed to get price from {name}: {e}")
            
            if len(prices) < 2:
                return None
            
            # Find best buy and sell opportunities
            best_buy = min(prices.items(), key=lambda x: x[1]['ask'])
            best_sell = max(prices.items(), key=lambda x: x[1]['bid'])
            
            buy_exchange_name = best_buy[0]
            buy_price = best_buy[1]['ask']
            sell_exchange_name = best_sell[0]
            sell_price = best_sell[1]['bid']
            
            # Calculate profit percentage
            profit_percent = ((sell_price - buy_price) / buy_price) * 100
            
            if profit_percent > self.min_profit_threshold:
                logger.info(
                    f"Arbitrage opportunity: Buy on {buy_exchange_name} at {buy_price}, "
                    f"sell on {sell_exchange_name} at {sell_price}, "
                    f"profit: {profit_percent:.2f}%"
                )
                
                return {
                    'strategy': self.name,
                    'action': 'arbitrage',
                    'buy_exchange': buy_exchange_name,
                    'sell_exchange': sell_exchange_name,
                    'buy_price': buy_price,
                    'sell_price': sell_price,
                    'profit_percent': profit_percent,
                    'symbol': symbol,
                    'amount': self.max_trade_amount / buy_price  # Convert USDT to base currency
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Error in arbitrage analysis: {e}")
            return None

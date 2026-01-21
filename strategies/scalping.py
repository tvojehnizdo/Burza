"""Scalping strategy for high-frequency small profits."""
import logging
from typing import Dict, Optional
from .base import BaseStrategy

logger = logging.getLogger(__name__)


class ScalpingStrategy(BaseStrategy):
    """Scalping strategy for quick small profits through high-frequency trading."""
    
    # Constants
    SPREAD_MULTIPLIER = 2.0  # Spread must be less than profit_target * this
    MICRO_ARBITRAGE_THRESHOLD = 0.05  # Minimum profit % for micro-arbitrage
    
    def __init__(self, 
                 profit_target: float = 0.15,
                 min_trade_amount: float = 10,
                 max_trade_amount: float = 20):
        """Initialize scalping strategy.
        
        Args:
            profit_target: Target profit percentage per trade (very small)
            min_trade_amount: Minimum trade amount in USDT
            max_trade_amount: Maximum trade amount in USDT
        """
        super().__init__('Scalping')
        self.profit_target = profit_target
        self.min_trade_amount = min_trade_amount
        self.max_trade_amount = max_trade_amount
    
    def analyze(self, exchanges: Dict, symbol: str) -> Optional[Dict]:
        """Analyze for quick scalping opportunities.
        
        This strategy looks for ANY positive price movement and attempts
        to capture small profits frequently.
        
        Args:
            exchanges: Dict of exchange name to exchange instance
            symbol: Trading pair symbol
        
        Returns:
            Trading signal if opportunity exists
        """
        if not self.enabled:
            return None
        
        if not exchanges:
            return None
        
        try:
            # Get all exchange prices
            prices = {}
            for name, exchange in exchanges.items():
                try:
                    ticker = exchange.get_ticker(symbol)
                    order_book = exchange.get_order_book(symbol, limit=3)
                    
                    prices[name] = {
                        'bid': ticker['bid'],
                        'ask': ticker['ask'],
                        'last': ticker['last'],
                        'spread': ticker['ask'] - ticker['bid'],
                        'spread_percent': ((ticker['ask'] - ticker['bid']) / ticker['bid']) * 100,
                        'order_book': order_book,
                        'exchange': exchange
                    }
                except Exception as e:
                    logger.warning(f"Failed to get data from {name}: {e}")
            
            if not prices:
                return None
            
            # Strategy 1: Find tight spreads for quick in-and-out
            for name, data in prices.items():
                spread_percent = data['spread_percent']
                
                # If spread is very tight, we can potentially make quick profit
                if spread_percent > 0 and spread_percent < self.profit_target * self.SPREAD_MULTIPLIER:
                    mid_price = (data['bid'] + data['ask']) / 2
                    
                    # Validate price
                    if mid_price <= 0 or mid_price < 0.0001:
                        continue
                    
                    # Calculate trade size - use min_trade_amount for quick fills
                    trade_amount = self.min_trade_amount
                    quantity = trade_amount / mid_price
                    
                    logger.info(
                        f"Scalping opportunity on {name}: "
                        f"Spread {spread_percent:.3f}%, "
                        f"Quick profit potential ~{self.profit_target:.2f}%"
                    )
                    
                    return {
                        'strategy': self.name,
                        'action': 'scalp',
                        'exchange': name,
                        'symbol': symbol,
                        'buy_price': data['ask'],
                        'target_sell_price': data['ask'] * (1 + self.profit_target / 100),
                        'amount': quantity,
                        'trade_amount': trade_amount,
                        'expected_profit': trade_amount * (self.profit_target / 100)
                    }
            
            # Strategy 2: Arbitrage between exchanges (if multiple available)
            if len(prices) >= 2:
                best_buy = min(prices.items(), key=lambda x: x[1]['ask'])
                best_sell = max(prices.items(), key=lambda x: x[1]['bid'])
                
                buy_exchange_name = best_buy[0]
                buy_price = best_buy[1]['ask']
                sell_exchange_name = best_sell[0]
                sell_price = best_sell[1]['bid']
                
                # Calculate profit
                if buy_price > 0 and buy_price > 0.0001:
                    profit_percent = ((sell_price - buy_price) / buy_price) * 100
                    
                    # Take ANY positive arbitrage for scalping
                    if profit_percent > self.MICRO_ARBITRAGE_THRESHOLD:
                        trade_amount = self.min_trade_amount
                        quantity = trade_amount / buy_price
                        
                        logger.info(
                            f"Micro-arbitrage: Buy {buy_exchange_name} at {buy_price}, "
                            f"sell {sell_exchange_name} at {sell_price}, "
                            f"profit: {profit_percent:.3f}%"
                        )
                        
                        return {
                            'strategy': self.name,
                            'action': 'micro_arbitrage',
                            'buy_exchange': buy_exchange_name,
                            'sell_exchange': sell_exchange_name,
                            'buy_price': buy_price,
                            'sell_price': sell_price,
                            'profit_percent': profit_percent,
                            'symbol': symbol,
                            'amount': quantity,
                            'expected_profit': trade_amount * (profit_percent / 100)
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"Error in scalping analysis: {e}")
            return None

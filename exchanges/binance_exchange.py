"""Binance exchange implementation."""
import ccxt
import logging
from typing import Dict, Optional
from .base import BaseExchange


logger = logging.getLogger(__name__)


class BinanceExchange(BaseExchange):
    """Binance exchange implementation using CCXT."""
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        """Initialize Binance exchange.
        
        Args:
            api_key: Binance API key
            api_secret: Binance API secret
            testnet: Use testnet if True
        """
        super().__init__('Binance')
        
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            }
        })
        
        if testnet:
            self.exchange.set_sandbox_mode(True)
        
        logger.info(f"Initialized {self.name} exchange")
    
    def get_ticker(self, symbol: str) -> Dict:
        """Get current ticker price.
        
        Args:
            symbol: Trading pair symbol (e.g., 'BTC/USDT')
        
        Returns:
            Dict with 'bid', 'ask', and 'last' prices
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                'bid': ticker['bid'],
                'ask': ticker['ask'],
                'last': ticker['last'],
                'timestamp': ticker['timestamp']
            }
        except Exception as e:
            logger.error(f"Error fetching ticker from {self.name}: {e}")
            raise
    
    def get_balance(self, currency: str) -> float:
        """Get balance for a currency.
        
        Args:
            currency: Currency code (e.g., 'BTC', 'USDT')
        
        Returns:
            Available balance
        """
        try:
            balance = self.exchange.fetch_balance()
            return balance.get(currency, {}).get('free', 0.0)
        except Exception as e:
            logger.error(f"Error fetching balance from {self.name}: {e}")
            raise
    
    def create_order(self, symbol: str, order_type: str, side: str,
                    amount: float, price: Optional[float] = None) -> Dict:
        """Create an order.
        
        Args:
            symbol: Trading pair symbol
            order_type: 'market' or 'limit'
            side: 'buy' or 'sell'
            amount: Order amount
            price: Price for limit orders
        
        Returns:
            Order information
        """
        try:
            if order_type == 'market':
                order = self.exchange.create_market_order(symbol, side, amount)
            else:
                if price is None:
                    raise ValueError("Price is required for limit orders")
                order = self.exchange.create_limit_order(symbol, side, amount, price)
            
            logger.info(f"Created {side} {order_type} order on {self.name}: {order['id']}")
            return order
        except Exception as e:
            logger.error(f"Error creating order on {self.name}: {e}")
            raise
    
    def get_order_book(self, symbol: str, limit: int = 5) -> Dict:
        """Get order book.
        
        Args:
            symbol: Trading pair symbol
            limit: Number of orders to fetch
        
        Returns:
            Dict with 'bids' and 'asks'
        """
        try:
            order_book = self.exchange.fetch_order_book(symbol, limit)
            return {
                'bids': order_book['bids'][:limit],
                'asks': order_book['asks'][:limit],
                'timestamp': order_book['timestamp']
            }
        except Exception as e:
            logger.error(f"Error fetching order book from {self.name}: {e}")
            raise

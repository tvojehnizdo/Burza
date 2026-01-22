"""Base exchange interface."""
from abc import ABC, abstractmethod
from typing import Dict, Optional


class BaseExchange(ABC):
    """Abstract base class for exchange implementations."""
    
    def __init__(self, name: str):
        """Initialize exchange.
        
        Args:
            name: Exchange name
        """
        self.name = name
    
    @abstractmethod
    def get_ticker(self, symbol: str) -> Dict:
        """Get current ticker price.
        
        Args:
            symbol: Trading pair symbol (e.g., 'BTC/USDT')
        
        Returns:
            Dict with 'bid', 'ask', and 'last' prices
        """
        pass
    
    @abstractmethod
    def get_balance(self, currency: str) -> float:
        """Get balance for a currency.
        
        Args:
            currency: Currency code (e.g., 'BTC', 'USDT')
        
        Returns:
            Available balance
        """
        pass
    
    @abstractmethod
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
        pass
    
    @abstractmethod
    def get_order_book(self, symbol: str, limit: int = 5) -> Dict:
        """Get order book.
        
        Args:
            symbol: Trading pair symbol
            limit: Number of orders to fetch
        
        Returns:
            Dict with 'bids' and 'asks'
        """
        pass

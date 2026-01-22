"""Base trading strategy interface."""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""
    
    def __init__(self, name: str):
        """Initialize strategy.
        
        Args:
            name: Strategy name
        """
        self.name = name
        self.enabled = True
    
    @abstractmethod
    def analyze(self, exchanges: Dict, symbol: str) -> Optional[Dict]:
        """Analyze markets and return trading signal.
        
        Args:
            exchanges: Dict of exchange name to exchange instance
            symbol: Trading pair symbol
        
        Returns:
            Trading signal dict with 'action', 'exchange', 'amount', 'price', etc.
            or None if no action needed
        """
        pass
    
    def enable(self):
        """Enable the strategy."""
        self.enabled = True
        logger.info(f"Strategy {self.name} enabled")
    
    def disable(self):
        """Disable the strategy."""
        self.enabled = False
        logger.info(f"Strategy {self.name} disabled")

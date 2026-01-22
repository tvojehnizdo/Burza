"""Trading strategies."""
from .base import BaseStrategy
from .arbitrage import ArbitrageStrategy
from .market_maker import MarketMakerStrategy
from .scalping import ScalpingStrategy

__all__ = ['BaseStrategy', 'ArbitrageStrategy', 'MarketMakerStrategy', 'ScalpingStrategy']

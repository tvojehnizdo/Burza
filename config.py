"""Configuration management for the trading bot."""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Config:
    """Trading bot configuration."""
    
    # Binance Configuration
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    
    # Kraken Configuration
    KRAKEN_API_KEY = os.getenv('KRAKEN_API_KEY', '')
    KRAKEN_API_SECRET = os.getenv('KRAKEN_API_SECRET', '')
    
    # Trading Configuration
    TRADING_PAIR = os.getenv('TRADING_PAIR', 'BTC/USDT')
    # Multi-pair trading: trade all pairs with this quote currency
    MULTI_PAIR_MODE = os.getenv('MULTI_PAIR_MODE', 'false').lower() == 'true'
    QUOTE_CURRENCY = os.getenv('QUOTE_CURRENCY', 'USDC')  # Base currency (USDT, USDC, EUR, etc.)
    MIN_PROFIT_THRESHOLD = float(os.getenv('MIN_PROFIT_THRESHOLD', '0.5'))
    MAX_TRADE_AMOUNT = float(os.getenv('MAX_TRADE_AMOUNT', '100'))
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '10'))
    
    # Risk Management
    MAX_POSITION_SIZE = float(os.getenv('MAX_POSITION_SIZE', '1000'))
    STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '2.0'))
    
    # Scalping Configuration
    SCALPING_MODE = os.getenv('SCALPING_MODE', 'false').lower() == 'true'
    SCALPING_PROFIT_TARGET = float(os.getenv('SCALPING_PROFIT_TARGET', '0.15'))
    SCALPING_MIN_TRADE = float(os.getenv('SCALPING_MIN_TRADE', '10'))
    
    @classmethod
    def validate(cls):
        """Validate that required configuration is present."""
        errors = []
        
        if not cls.BINANCE_API_KEY and not cls.KRAKEN_API_KEY:
            errors.append("At least one exchange API key must be configured")
        
        if cls.BINANCE_API_KEY and not cls.BINANCE_API_SECRET:
            errors.append("BINANCE_API_SECRET is required when BINANCE_API_KEY is set")
        
        if cls.KRAKEN_API_KEY and not cls.KRAKEN_API_SECRET:
            errors.append("KRAKEN_API_SECRET is required when KRAKEN_API_KEY is set")
        
        return errors

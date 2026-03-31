"""
config.py - Strategy parameters and risk settings for the trading bot.
Optimised for a ~$59 account (47 USDC on Binance, 12 USDT on Kraken).
"""

# ---------------------------------------------------------------------------
# Exchange fees
# ---------------------------------------------------------------------------
BINANCE_FEE = 0.001   # 0.1% maker/taker
KRAKEN_FEE = 0.0016   # 0.16% maker/taker

# ---------------------------------------------------------------------------
# Trading pairs to monitor (must be available on both exchanges)
# ---------------------------------------------------------------------------
TRADING_PAIRS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
]

# ---------------------------------------------------------------------------
# Arbitrage settings
# ---------------------------------------------------------------------------
# Minimum net profit margin (after all fees + slippage) to trigger a trade
ARBITRAGE_MIN_PROFIT_PCT = 0.003   # 0.3% net profit minimum
# Slippage estimate per side
SLIPPAGE_PCT = 0.0005              # 0.05%

# ---------------------------------------------------------------------------
# Grid trading settings
# ---------------------------------------------------------------------------
GRID_LEVELS = 4           # Number of grid levels above and below mid-price
GRID_SPACING_PCT = 0.02   # 2% between each grid level
GRID_ORDER_SIZE_USD = 5.0 # USD value per grid order

# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------
MAX_TRADE_SIZE_USD = 10.0        # Maximum position size per trade
STOP_LOSS_PCT = 0.05             # Stop-loss at -5% per position
DAILY_LOSS_LIMIT_USD = 5.0       # Stop trading if daily loss exceeds $5
MAX_CONCURRENT_POSITIONS = 2     # Maximum simultaneous open positions
EMERGENCY_RESERVE_USD = 5.0      # Always keep at least $5 unused

# ---------------------------------------------------------------------------
# Bot timing
# ---------------------------------------------------------------------------
PRICE_CHECK_INTERVAL_SEC = 5     # How often to fetch prices (seconds)
GRID_REFRESH_INTERVAL_SEC = 300   # How often to refresh grid orders (seconds)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = "logs"
TRADES_CSV = "logs/trades.csv"
METRICS_JSON = "logs/metrics.json"

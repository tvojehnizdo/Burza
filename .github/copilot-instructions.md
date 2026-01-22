# Copilot Instructions for Burza

## Project Overview

**Burza** is an automated cryptocurrency trading bot for Binance and Kraken exchanges. It implements high-frequency scalping, arbitrage, and market-making strategies optimized for the Czech market (USDC/EUR pairs).

Core architecture: `bot.py` orchestrates exchanges and strategies in a main loop. Exchange implementations ([exchanges/](exchanges/)) wrap CCXT library, strategies ([strategies/](strategies/)) analyze market data and generate signals.

## Architecture & Data Flow

### Component Structure
```
bot.py (orchestrator) 
  ├── exchanges/ (CCXT wrappers)
  │   ├── base.py (abstract interface)
  │   ├── binance_exchange.py
  │   └── kraken_exchange.py
  └── strategies/ (signal generators)
      ├── base.py (abstract strategy)
      ├── arbitrage.py (cross-exchange)
      ├── market_maker.py (liquidity provision)
      └── scalping.py (high-frequency)
```

### Critical Execution Flow
1. **Bot initialization**: Validates config → initializes exchanges → loads strategies
2. **Main loop** (every `CHECK_INTERVAL` seconds):
   - Fetch tickers/order books from all exchanges
   - Pass data to each enabled strategy
   - Execute trades if signals meet thresholds
3. **Trade execution**: Validates prices → checks balances → places orders → logs results

### Multi-Mode Operation
- **Single-pair mode**: Trade one pair (e.g., `BTC/USDC`)
- **Multi-pair mode**: (`MULTI_PAIR_MODE=true`) Dynamically discovers and trades ALL pairs with `QUOTE_CURRENCY`
- **Aggressive scalping**: Continuous loop - immediately sells after every buy for maximum frequency

## Configuration Patterns

### Environment-Based Config
All configuration via [.env files](config.py):
- `.env.aggressive` - High-frequency scalping (0.15% profit target, 10s interval)
- `.env.multipair` - Trade all USDC pairs dynamically
- `.env.usdc` - Czech-optimized single-pair BTC/USDC

**Never hardcode API keys** - always use environment variables loaded by `python-dotenv`.

### Safety-First Defaults
```python
# CRITICAL: Dry-run is default mode
self.dry_run = True  # Only --live flag enables real trades
```
All new features must preserve dry-run mode. Test in dry-run before live execution.

## Strategy Implementation

### Scalping Strategy Pattern
[strategies/scalping.py](strategies/scalping.py) demonstrates the project's core approach:

```python
# Look for tight spreads relative to profit target
if spread_percent < profit_target * SPREAD_MULTIPLIER:
    # Execute quick buy-sell cycle
    # Use MIN_TRADE_AMOUNT for fastest fills
```

**Key insight**: Profit from spread compression, not direction. Strategies return signals as dicts:
```python
return {
    'strategy': 'Scalping',
    'action': 'scalp',  # or 'arbitrage', 'market_make'
    'exchange': 'Binance',
    'symbol': 'BTC/USDC',
    'side': 'buy',
    'amount': 0.001,
    'price': 50000.0
}
```

### Validation Requirements
Every trade execution must validate:
1. `price > 0 and price > 0.0001` (prevent invalid prices)
2. Check successful buy before executing sell (arbitrage)
3. Verify balance sufficiency before order placement

## Development Workflows

### Testing Workflow
```bash
# 1. Test mode (no real trades)
./start_dryrun.sh

# 2. Validate with test suite
python test_bot.py

# 3. Live trading (after thorough testing)
./start_live.sh
```

### VPS Deployment Automation
[deploy_vps.sh](deploy_vps.sh) handles complete deployment:
- Installs Python dependencies
- Interactively configures API keys
- Sets up systemd service or screen session
- See [VPS_DEPLOYMENT.md](VPS_DEPLOYMENT.md) for architecture

### Reset and Restart Pattern
[reset_and_setup.sh](reset_and_setup.sh) demonstrates critical workflow:
```bash
# Cancel ALL open orders on both exchanges
# Reconfigure for new trading pair
# Restart with fresh state
```
Use this pattern when switching trading configurations.

## Project-Specific Conventions

### Czech Market Optimization
- Default to **USDC** (not USDT) - better CZ bank support
- Prefer **BTC/USDC** pair for reliability
- Document in Czech for local users (comments can be English)

### Logging Standards
```python
logger.info(f"{strategy}: {exchange} {symbol} - {action} signal")
logger.warning("Non-critical issue: {details}")  # Keep running
logger.error("Critical error: {details}")  # May require intervention
```
All trades logged to `trading_bot.log` AND console.

### File Naming
- Shell scripts: `verb_noun.sh` (e.g., `start_dryrun.sh`, `setup_vps.sh`)
- Strategy modules: `lowercase.py` (e.g., `scalping.py`, `arbitrage.py`)
- Documentation: `UPPERCASE.md` for guides (e.g., `QUICKSTART.md`)

## Critical Integration Points

### CCXT Exchange Abstraction
Exchanges inherit from [exchanges/base.py](exchanges/base.py):
```python
class BaseExchange(ABC):
    @abstractmethod
    def get_ticker(symbol: str) -> Dict
    def create_order(symbol, type, side, amount, price)
    def get_order_book(symbol, limit)
```
**All exchange-specific code isolated** - easy to add new exchanges.

### Strategy Plugin System
Strategies inherit from [strategies/base.py](strategies/base.py):
```python
class BaseStrategy(ABC):
    def enable()/disable()  # Runtime control
    @abstractmethod
    def analyze(exchanges, symbol) -> Optional[Dict]
```
Bot automatically discovers and runs all enabled strategies.

## Common Pitfalls to Avoid

1. **Division by zero**: Always validate `mid_price > 0` before calculating quantities
2. **Missing rollback**: Arbitrage must handle failed second leg (cancel first order)
3. **Rate limits**: CCXT handles this, but don't disable `enableRateLimit: True`
4. **Testnet caveat**: Binance testnet has stale data - use low thresholds for dry-run testing

## Version Control Practices

Follow [Conventional Commits](https://www.conventionalcommits.org/):
- `feat:` New strategies or exchange integrations
- `fix:` Bug fixes in trading logic
- `chore:` Dependency updates, config changes
- `docs:` Documentation updates (guides, comments)
- `refactor:` Code restructuring without behavior change

## Quick Reference

**Start developing**: Read [IMPLEMENTATION.md](IMPLEMENTATION.md) for system overview  
**Deploy**: Use [QUICKSTART.md](QUICKSTART.md) or [VPS_DEPLOYMENT.md](VPS_DEPLOYMENT.md)  
**Predict returns**: Run [profit_prediction.py](profit_prediction.py) before live trading  
**Emergency stop**: `./close_all_positions.py` cancels all orders across exchanges

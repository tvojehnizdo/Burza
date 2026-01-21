"""Main trading bot orchestrator."""
import logging
import time
from typing import Dict, List
from config import Config
from exchanges import BinanceExchange, KrakenExchange
from strategies import ArbitrageStrategy, MarketMakerStrategy, ScalpingStrategy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


class TradingBot:
    """Main trading bot that orchestrates exchanges and strategies."""
    
    def __init__(self, dry_run: bool = True):
        """Initialize trading bot.
        
        Args:
            dry_run: If True, only simulate trades without executing
        """
        self.dry_run = dry_run
        self.exchanges = {}
        self.strategies = []
        self.running = False
        
        logger.info(f"Initializing trading bot (dry_run={dry_run})")
        
        # Validate configuration
        errors = Config.validate()
        if errors:
            for error in errors:
                logger.error(error)
            raise ValueError("Configuration validation failed")
        
        # Initialize exchanges
        self._initialize_exchanges()
        
        # Initialize strategies
        self._initialize_strategies()
    
    def _initialize_exchanges(self):
        """Initialize exchange connections."""
        if Config.BINANCE_API_KEY and Config.BINANCE_API_SECRET:
            try:
                self.exchanges['Binance'] = BinanceExchange(
                    Config.BINANCE_API_KEY,
                    Config.BINANCE_API_SECRET,
                    testnet=self.dry_run
                )
                logger.info("Binance exchange initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Binance: {e}")
        
        if Config.KRAKEN_API_KEY and Config.KRAKEN_API_SECRET:
            try:
                self.exchanges['Kraken'] = KrakenExchange(
                    Config.KRAKEN_API_KEY,
                    Config.KRAKEN_API_SECRET
                )
                logger.info("Kraken exchange initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Kraken: {e}")
        
        if not self.exchanges:
            raise ValueError("No exchanges were initialized successfully")
    
    def _initialize_strategies(self):
        """Initialize trading strategies."""
        # Scalping strategy (highest priority for frequent small profits)
        if Config.SCALPING_MODE:
            self.strategies.append(
                ScalpingStrategy(
                    profit_target=Config.SCALPING_PROFIT_TARGET,
                    min_trade_amount=Config.SCALPING_MIN_TRADE,
                    max_trade_amount=Config.MAX_TRADE_AMOUNT
                )
            )
            logger.info("Scalping strategy added (HIGH FREQUENCY MODE)")
        
        # Arbitrage strategy (works with multiple exchanges)
        if len(self.exchanges) >= 2:
            self.strategies.append(
                ArbitrageStrategy(
                    min_profit_threshold=Config.MIN_PROFIT_THRESHOLD,
                    max_trade_amount=Config.MAX_TRADE_AMOUNT
                )
            )
            logger.info("Arbitrage strategy added")
        
        # Market maker strategy
        if not Config.SCALPING_MODE:  # Don't use market maker in scalping mode
            self.strategies.append(
                MarketMakerStrategy(
                    spread_percent=0.5,
                    order_size=Config.MAX_TRADE_AMOUNT / 2
                )
            )
            logger.info("Market maker strategy added")
    
    def _execute_signal(self, signal: Dict):
        """Execute a trading signal.
        
        Args:
            signal: Trading signal from strategy
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would execute: {signal}")
            return
        
        try:
            action = signal.get('action')
            
            if action in ['arbitrage', 'micro_arbitrage']:
                self._execute_arbitrage(signal)
            elif action == 'market_make':
                self._execute_market_making(signal)
            elif action == 'scalp':
                self._execute_scalp(signal)
            else:
                logger.warning(f"Unknown action: {action}")
                
        except Exception as e:
            logger.error(f"Error executing signal: {e}")
    
    def _execute_arbitrage(self, signal: Dict):
        """Execute arbitrage trade.
        
        Args:
            signal: Arbitrage signal
        """
        buy_exchange_name = signal['buy_exchange']
        sell_exchange_name = signal['sell_exchange']
        symbol = signal['symbol']
        amount = signal['amount']
        
        buy_exchange = self.exchanges[buy_exchange_name]
        sell_exchange = self.exchanges[sell_exchange_name]
        
        # Execute buy order
        logger.info(f"Buying {amount} {symbol} on {buy_exchange_name}")
        try:
            buy_order = buy_exchange.create_order(symbol, 'market', 'buy', amount)
            
            # Only execute sell if buy was successful
            if buy_order and buy_order.get('status') != 'canceled':
                logger.info(f"Selling {amount} {symbol} on {sell_exchange_name}")
                sell_order = sell_exchange.create_order(symbol, 'market', 'sell', amount)
                logger.info(f"Arbitrage executed: Buy order {buy_order['id']}, Sell order {sell_order['id']}")
            else:
                logger.error(f"Buy order failed or was canceled, aborting arbitrage")
        except Exception as e:
            logger.error(f"Arbitrage execution failed: {e}")
    
    def _execute_market_making(self, signal: Dict):
        """Execute market making orders.
        
        Args:
            signal: Market making signal
        """
        exchange_name = signal['exchange']
        symbol = signal['symbol']
        amount = signal['amount']
        buy_price = signal['buy_price']
        sell_price = signal['sell_price']
        
        exchange = self.exchanges[exchange_name]
        
        # Place buy limit order
        logger.info(f"Placing buy order: {amount} {symbol} at {buy_price}")
        buy_order = exchange.create_order(symbol, 'limit', 'buy', amount, buy_price)
        
        # Place sell limit order
        logger.info(f"Placing sell order: {amount} {symbol} at {sell_price}")
        sell_order = exchange.create_order(symbol, 'limit', 'sell', amount, sell_price)
        
        logger.info(f"Market making orders placed: Buy {buy_order['id']}, Sell {sell_order['id']}")
    
    def _execute_scalp(self, signal: Dict):
        """Execute scalp trade.
        
        Args:
            signal: Scalp signal
        """
        exchange_name = signal['exchange']
        symbol = signal['symbol']
        amount = signal['amount']
        buy_price = signal['buy_price']
        target_sell_price = signal.get('target_sell_price')
        
        exchange = self.exchanges[exchange_name]
        
        try:
            # Execute market buy for immediate entry
            logger.info(f"Scalping: Buying {amount} {symbol} at market price ~{buy_price}")
            buy_order = exchange.create_order(symbol, 'market', 'buy', amount)
            
            # If buy successful, place limit sell at target
            if buy_order and buy_order.get('status') != 'canceled':
                if target_sell_price:
                    logger.info(f"Scalping: Placing sell limit at {target_sell_price}")
                    sell_order = exchange.create_order(symbol, 'limit', 'sell', amount, target_sell_price)
                    logger.info(f"Scalp executed: Buy {buy_order['id']}, Sell limit {sell_order['id']}")
                else:
                    # Immediate sell at market for quick profit
                    logger.info(f"Scalping: Immediate sell at market")
                    sell_order = exchange.create_order(symbol, 'market', 'sell', amount)
                    logger.info(f"Quick scalp: Buy {buy_order['id']}, Sell {sell_order['id']}")
            else:
                logger.error("Scalp buy order failed or was canceled, aborting")
        except Exception as e:
            logger.error(f"Error executing scalp trade: {e}")
    
    def run(self):
        """Run the trading bot."""
        self.running = True
        logger.info("Trading bot started")
        
        try:
            while self.running:
                logger.info("=== Trading cycle start ===")
                
                # Run each strategy
                for strategy in self.strategies:
                    try:
                        signal = strategy.analyze(self.exchanges, Config.TRADING_PAIR)
                        if signal:
                            self._execute_signal(signal)
                    except Exception as e:
                        logger.error(f"Error in strategy {strategy.name}: {e}")
                
                logger.info(f"Sleeping for {Config.CHECK_INTERVAL} seconds...")
                time.sleep(Config.CHECK_INTERVAL)
                
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Bot error: {e}")
        finally:
            self.stop()
    
    def stop(self):
        """Stop the trading bot."""
        self.running = False
        logger.info("Trading bot stopped")


def main():
    """Main entry point."""
    import sys
    
    # Check if dry-run mode
    dry_run = '--live' not in sys.argv
    
    if dry_run:
        logger.warning("Running in DRY RUN mode. Use --live flag for real trading.")
    else:
        logger.warning("Running in LIVE mode. Real trades will be executed!")
        response = input("Are you sure you want to continue with LIVE trading? (yes/no): ")
        if response.lower() != 'yes':
            logger.info("Exiting...")
            return
    
    # Create and run bot
    bot = TradingBot(dry_run=dry_run)
    bot.run()


if __name__ == '__main__':
    main()

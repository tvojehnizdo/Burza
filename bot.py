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
        self.trading_pairs = []
        
        # Risk Management Tracking
        self.daily_profit_loss = 0.0  # Track P&L
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.consecutive_losses = 0
        self.max_consecutive_losses = 5  # Stop after 5 losses in a row
        self.daily_loss_limit = Config.MAX_TRADE_AMOUNT * 5  # Max loss = 5x trade amount
        self.last_trade_price = {}  # Track entry prices for stop-loss
        
        logger.info(f"Initializing trading bot (dry_run={dry_run})")
        logger.info(f"🛡️  Risk Management: Max daily loss: ${self.daily_loss_limit:.2f}, Max consecutive losses: {self.max_consecutive_losses}")
        
        # Validate configuration
        errors = Config.validate()
        if errors:
            for error in errors:
                logger.error(error)
            raise ValueError("Configuration validation failed")
        
        # Initialize exchanges
        self._initialize_exchanges()
        
        # Initialize trading pairs
        self._initialize_trading_pairs()
        
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
    
    def _initialize_trading_pairs(self):
        """Initialize trading pairs to monitor."""
        if Config.MULTI_PAIR_MODE:
            # Multi-pair mode: scan all pairs with the quote currency
            logger.info(f"Multi-pair mode enabled - scanning all {Config.QUOTE_CURRENCY} pairs")
            pairs_set = set()
            
            for exchange_name, exchange in self.exchanges.items():
                try:
                    markets = exchange.exchange.load_markets()
                    for symbol in markets:
                        # Filter for quote currency pairs (e.g., */USDC)
                        if f"/{Config.QUOTE_CURRENCY}" in symbol:
                            pairs_set.add(symbol)
                except Exception as e:
                    logger.error(f"Failed to load markets from {exchange_name}: {e}")
            
            self.trading_pairs = sorted(list(pairs_set))
            logger.info(f"Found {len(self.trading_pairs)} {Config.QUOTE_CURRENCY} pairs: {', '.join(self.trading_pairs[:10])}{'...' if len(self.trading_pairs) > 10 else ''}")
        else:
            # Single pair mode: use configured trading pair
            self.trading_pairs = [Config.TRADING_PAIR]
            logger.info(f"Single pair mode: {Config.TRADING_PAIR}")
    
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
        # Risk Management Checks
        if not self._check_risk_limits():
            logger.warning("⛔ Risk limits exceeded, skipping trade")
            return
        
        if self.dry_run:
            logger.info(f"DRY RUN: Would execute {signal}")
            self._simulate_trade_result(signal)
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
    
    def _check_risk_limits(self) -> bool:
        """Check if trading is allowed based on risk management rules.
        
        Returns:
            True if trading is allowed, False otherwise
        """
        # Check daily loss limit
        if self.daily_profit_loss < -self.daily_loss_limit:
            logger.error(f"🚨 DAILY LOSS LIMIT REACHED: ${self.daily_profit_loss:.2f} < -${self.daily_loss_limit:.2f}")
            logger.error("🛑 STOPPING TRADING FOR TODAY")
            return False
        
        # Check consecutive losses
        if self.consecutive_losses >= self.max_consecutive_losses:
            logger.error(f"🚨 MAX CONSECUTIVE LOSSES REACHED: {self.consecutive_losses}")
            logger.error("🛑 STOPPING TRADING - Wait for market conditions to improve")
            return False
        
        return True
    
    def _simulate_trade_result(self, signal: Dict):
        """Simulate trade result for dry-run mode.
        
        Args:
            signal: Trading signal
        """
        # Simulate 60% win rate for testing
        import random
        is_win = random.random() < 0.6
        
        if is_win:
            profit = signal.get('expected_profit', signal.get('trade_amount', 10) * 0.002)
            self.daily_profit_loss += profit
            self.winning_trades += 1
            self.consecutive_losses = 0
            logger.info(f"✅ Simulated WIN: +${profit:.2f} | Total P&L: ${self.daily_profit_loss:.2f}")
        else:
            loss = signal.get('trade_amount', 10) * 0.01  # 1% loss
            self.daily_profit_loss -= loss
            self.losing_trades += 1
            self.consecutive_losses += 1
            logger.warning(f"❌ Simulated LOSS: -${loss:.2f} | Consecutive: {self.consecutive_losses} | Total P&L: ${self.daily_profit_loss:.2f}")
        
        self.total_trades += 1
    
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
            
            # Track entry price for stop-loss
            self.last_trade_price[symbol] = buy_order.get('average', buy_price)
            
            # If buy successful, immediately sell at market for continuous loop trading
            if buy_order and buy_order.get('status') != 'canceled':
                actual_buy_price = buy_order.get('average', buy_price)
                trade_cost = amount * actual_buy_price
                
                if target_sell_price:
                    logger.info(f"Scalping: Placing sell limit at {target_sell_price}")
                    sell_order = exchange.create_order(symbol, 'limit', 'sell', amount, target_sell_price)
                    logger.info(f"Scalp executed: Buy {buy_order['id']}, Sell limit {sell_order['id']}")
                else:
                    # Immediate sell at market for continuous loop trading
                    logger.info(f"🔁 Scalping LOOP: Immediate market sell for continuous trading")
                    sell_order = exchange.create_order(symbol, 'market', 'sell', amount)
                    
                    # Calculate P&L
                    actual_sell_price = sell_order.get('average', actual_buy_price)
                    trade_revenue = amount * actual_sell_price
                    profit_loss = trade_revenue - trade_cost
                    
                    # Update tracking
                    self.daily_profit_loss += profit_loss
                    self.total_trades += 1
                    
                    if profit_loss > 0:
                        self.winning_trades += 1
                        self.consecutive_losses = 0
                        logger.info(f"✅ PROFIT: ${profit_loss:.4f} | Total: ${self.daily_profit_loss:.2f} | W/L: {self.winning_trades}/{self.losing_trades}")
                    else:
                        self.losing_trades += 1
                        self.consecutive_losses += 1
                        logger.warning(f"❌ LOSS: ${profit_loss:.4f} | Consecutive: {self.consecutive_losses} | Total: ${self.daily_profit_loss:.2f}")
                    
                    logger.info(f"✅ Scalp cycle complete: Buy {buy_order['id']} → Sell {sell_order['id']} | Ready for next trade")
            else:
                logger.error("Scalp buy order failed or was canceled, aborting")
        except Exception as e:
            logger.error(f"Error executing scalp trade: {e}")
            self.consecutive_losses += 1
    
    def run(self):
        """Run the trading bot."""
        self.running = True
        logger.info("Trading bot started")
        logger.info(f"Monitoring {len(self.trading_pairs)} trading pair(s)")
        logger.info("=" * 80)
        
        try:
            while self.running:
                logger.info("=== Trading cycle start ===")
                
                # Print status
                if self.total_trades > 0:
                    win_rate = (self.winning_trades / self.total_trades) * 100
                    logger.info(f"📊 Session Stats: Trades: {self.total_trades} | Win Rate: {win_rate:.1f}% | P&L: ${self.daily_profit_loss:.2f}")
                
                # Run strategies for each trading pair
                for symbol in self.trading_pairs:
                    for strategy in self.strategies:
                        try:
                            signal = strategy.analyze(self.exchanges, symbol)
                            if signal:
                                self._execute_signal(signal)
                        except Exception as e:
                            logger.error(f"Error in strategy {strategy.name} for {symbol}: {e}")
                
                logger.info(f"Sleeping for {Config.CHECK_INTERVAL} seconds...")
                time.sleep(Config.CHECK_INTERVAL)
                
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            self._print_final_report()
        except Exception as e:
            logger.error(f"Bot error: {e}")
        finally:
            self.stop()
    
    def _print_final_report(self):
        """Print final trading session report."""
        logger.info("=" * 80)
        logger.info("📈 FINAL TRADING REPORT")
        logger.info("=" * 80)
        logger.info(f"Total Trades: {self.total_trades}")
        logger.info(f"Winning Trades: {self.winning_trades}")
        logger.info(f"Losing Trades: {self.losing_trades}")
        if self.total_trades > 0:
            win_rate = (self.winning_trades / self.total_trades) * 100
            logger.info(f"Win Rate: {win_rate:.1f}%")
        logger.info(f"Total P&L: ${self.daily_profit_loss:.2f}")
        if self.daily_profit_loss > 0:
            logger.info("🎉 Profitable session!")
        else:
            logger.info("📉 Loss session - review strategy")
        logger.info("=" * 80)
    
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

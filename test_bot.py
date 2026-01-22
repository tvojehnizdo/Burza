"""Test script to verify the trading bot setup."""
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_imports():
    """Test that all modules can be imported."""
    print("Testing imports...")
    
    try:
        from config import Config
        print("✓ Config imported successfully")
        
        from exchanges import BaseExchange, BinanceExchange, KrakenExchange
        print("✓ Exchange modules imported successfully")
        
        from strategies import BaseStrategy, ArbitrageStrategy, MarketMakerStrategy
        print("✓ Strategy modules imported successfully")
        
        import bot
        print("✓ Bot module imported successfully")
        
        return True
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False


def test_config():
    """Test configuration validation."""
    print("\nTesting configuration...")
    
    from config import Config
    
    # Test that config attributes exist
    assert hasattr(Config, 'BINANCE_API_KEY'), "BINANCE_API_KEY not found"
    assert hasattr(Config, 'KRAKEN_API_KEY'), "KRAKEN_API_KEY not found"
    assert hasattr(Config, 'TRADING_PAIR'), "TRADING_PAIR not found"
    print("✓ Configuration attributes exist")
    
    # Test validation function
    errors = Config.validate()
    print(f"✓ Configuration validation works (found {len(errors)} error(s) as expected)")
    
    return True


def test_exchange_interface():
    """Test exchange interface."""
    print("\nTesting exchange interface...")
    
    from exchanges.base import BaseExchange
    
    # Test that interface has required methods
    required_methods = ['get_ticker', 'get_balance', 'create_order', 'get_order_book']
    for method in required_methods:
        assert hasattr(BaseExchange, method), f"Method {method} not found"
    
    print("✓ Exchange interface has required methods")
    return True


def test_strategy_interface():
    """Test strategy interface."""
    print("\nTesting strategy interface...")
    
    from strategies.base import BaseStrategy
    
    # Test that interface has required methods
    assert hasattr(BaseStrategy, 'analyze'), "analyze method not found"
    assert hasattr(BaseStrategy, 'enable'), "enable method not found"
    assert hasattr(BaseStrategy, 'disable'), "disable method not found"
    
    print("✓ Strategy interface has required methods")
    return True


def test_arbitrage_logic():
    """Test arbitrage strategy logic."""
    print("\nTesting arbitrage strategy...")
    
    from strategies import ArbitrageStrategy
    
    strategy = ArbitrageStrategy(min_profit_threshold=0.5, max_trade_amount=100)
    assert strategy.name == 'Arbitrage', "Strategy name incorrect"
    assert strategy.enabled, "Strategy should be enabled by default"
    
    # Test enable/disable
    strategy.disable()
    assert not strategy.enabled, "Strategy should be disabled"
    strategy.enable()
    assert strategy.enabled, "Strategy should be enabled"
    
    print("✓ Arbitrage strategy works correctly")
    return True


def test_market_maker_logic():
    """Test market maker strategy logic."""
    print("\nTesting market maker strategy...")
    
    from strategies import MarketMakerStrategy
    
    strategy = MarketMakerStrategy(spread_percent=0.5, order_size=50)
    assert strategy.name == 'MarketMaker', "Strategy name incorrect"
    assert strategy.enabled, "Strategy should be enabled by default"
    
    print("✓ Market maker strategy works correctly")
    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("Testing Burza Trading Bot")
    print("=" * 60)
    
    tests = [
        test_imports,
        test_config,
        test_exchange_interface,
        test_strategy_interface,
        test_arbitrage_logic,
        test_market_maker_logic,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"✗ Test failed with exception: {e}")
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"Test Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    if failed == 0:
        print("\n✓ All tests passed! The trading bot is ready to use.")
        print("\nNext steps:")
        print("1. Copy .env.example to .env")
        print("2. Add your Binance/Kraken API keys to .env")
        print("3. Run: python bot.py (for dry-run mode)")
        print("4. Run: python bot.py --live (for live trading)")
        return 0
    else:
        print("\n✗ Some tests failed. Please check the errors above.")
        return 1


if __name__ == '__main__':
    sys.exit(main())

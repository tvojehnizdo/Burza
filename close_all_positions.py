#!/usr/bin/env python3
"""Close all open positions and pending orders - clean slate for continuous trading."""
import os
from dotenv import load_dotenv
from exchanges import BinanceExchange, KrakenExchange

load_dotenv()

def close_all_positions():
    """Cancel all orders and close all positions on both exchanges."""
    print("=" * 60)
    print(" CLOSING ALL POSITIONS & ORDERS")
    print("=" * 60)
    
    # Binance
    if os.getenv('BINANCE_API_KEY') and os.getenv('BINANCE_API_SECRET'):
        print("\n🔄 Binance: Canceling orders and closing positions...")
        try:
            binance = BinanceExchange(
                os.getenv('BINANCE_API_KEY'),
                os.getenv('BINANCE_API_SECRET'),
                testnet=False
            )
            
            # Cancel all open orders
            markets = binance.exchange.load_markets()
            canceled_count = 0
            
            for symbol in markets:
                try:
                    orders = binance.exchange.fetch_open_orders(symbol)
                    for order in orders:
                        binance.exchange.cancel_order(order['id'], symbol)
                        canceled_count += 1
                        print(f"  ✓ Canceled order {order['id']} for {symbol}")
                except:
                    pass
            
            print(f"✅ Binance: Canceled {canceled_count} orders")
            
            # Get all balances and sell non-USDC assets
            balances = binance.exchange.fetch_balance()
            sold_count = 0
            
            for currency, balance in balances['free'].items():
                if currency != 'USDC' and balance > 0:
                    try:
                        symbol = f"{currency}/USDC"
                        if symbol in markets:
                            # Sell at market price
                            binance.exchange.create_market_sell_order(symbol, balance)
                            sold_count += 1
                            print(f"  ✓ Sold {balance} {currency} to USDC")
                    except Exception as e:
                        print(f"  ⚠ Could not sell {currency}: {e}")
            
            print(f"✅ Binance: Closed {sold_count} positions")
            
        except Exception as e:
            print(f"❌ Binance error: {e}")
    
    # Kraken
    if os.getenv('KRAKEN_API_KEY') and os.getenv('KRAKEN_API_SECRET'):
        print("\n🔄 Kraken: Canceling orders and closing positions...")
        try:
            kraken = KrakenExchange(
                os.getenv('KRAKEN_API_KEY'),
                os.getenv('KRAKEN_API_SECRET')
            )
            
            # Cancel all open orders
            try:
                result = kraken.exchange.cancel_all_orders()
                print(f"✅ Kraken: Canceled all orders")
            except Exception as e:
                print(f"  ⚠ Cancel orders: {e}")
            
            # Get all balances and sell non-USDC assets
            markets = kraken.exchange.load_markets()
            balances = kraken.exchange.fetch_balance()
            sold_count = 0
            
            for currency, balance in balances['free'].items():
                if currency != 'USDC' and balance > 0:
                    try:
                        symbol = f"{currency}/USDC"
                        if symbol in markets:
                            # Sell at market price
                            kraken.exchange.create_market_sell_order(symbol, balance)
                            sold_count += 1
                            print(f"  ✓ Sold {balance} {currency} to USDC")
                    except Exception as e:
                        print(f"  ⚠ Could not sell {currency}: {e}")
            
            print(f"✅ Kraken: Closed {sold_count} positions")
            
        except Exception as e:
            print(f"❌ Kraken error: {e}")
    
    print("\n" + "=" * 60)
    print("✅ All positions closed - ready for continuous loop trading!")
    print("=" * 60)

if __name__ == '__main__':
    close_all_positions()

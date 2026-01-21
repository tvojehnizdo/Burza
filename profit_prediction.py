"""Profit prediction tool for trading bot."""
import sys
from datetime import datetime, timedelta


class ProfitPredictor:
    """Estimates potential profits based on capital and market conditions."""
    
    def __init__(self, binance_capital: float, kraken_capital: float):
        """Initialize profit predictor.
        
        Args:
            binance_capital: Available capital on Binance (USDT)
            kraken_capital: Available capital on Kraken (USDT)
        """
        self.binance_capital = binance_capital
        self.kraken_capital = kraken_capital
        self.total_capital = binance_capital + kraken_capital
    
    def estimate_arbitrage_profit(self, 
                                  avg_spread_percent: float = 0.3,
                                  trades_per_day: int = 5,
                                  success_rate: float = 0.8,
                                  exchange_fee_percent: float = 0.1):
        """Estimate arbitrage profits.
        
        Args:
            avg_spread_percent: Average price spread between exchanges (%)
            trades_per_day: Number of profitable opportunities per day
            success_rate: Percentage of successful trades
            exchange_fee_percent: Total exchange fees per trade (%)
        
        Returns:
            Dict with profit estimates
        """
        # Net profit per trade after fees
        net_profit_percent = avg_spread_percent - (2 * exchange_fee_percent)
        
        if net_profit_percent <= 0:
            return {
                'feasible': False,
                'reason': 'Spread ne pokrývá poplatky burz'
            }
        
        # Use smaller capital as it limits arbitrage size
        effective_capital = min(self.binance_capital, self.kraken_capital)
        
        # Daily profit
        profit_per_trade = effective_capital * (net_profit_percent / 100)
        daily_profit = profit_per_trade * trades_per_day * success_rate
        
        # Monthly and yearly projections
        monthly_profit = daily_profit * 30
        yearly_profit = daily_profit * 365
        
        # ROI calculations
        daily_roi = (daily_profit / self.total_capital) * 100
        monthly_roi = (monthly_profit / self.total_capital) * 100
        yearly_roi = (yearly_profit / self.total_capital) * 100
        
        return {
            'feasible': True,
            'effective_capital': effective_capital,
            'net_profit_percent': net_profit_percent,
            'profit_per_trade': profit_per_trade,
            'trades_per_day': trades_per_day,
            'daily_profit': daily_profit,
            'monthly_profit': monthly_profit,
            'yearly_profit': yearly_profit,
            'daily_roi': daily_roi,
            'monthly_roi': monthly_roi,
            'yearly_roi': yearly_roi
        }
    
    def estimate_market_making_profit(self,
                                      spread_percent: float = 0.5,
                                      fills_per_day: int = 10,
                                      success_rate: float = 0.7,
                                      exchange_fee_percent: float = 0.1):
        """Estimate market making profits.
        
        Args:
            spread_percent: Your spread on orders (%)
            fills_per_day: Number of filled orders per day
            success_rate: Percentage of profitable fills
            exchange_fee_percent: Exchange fees per trade (%)
        
        Returns:
            Dict with profit estimates
        """
        # Net profit per fill after fees
        net_profit_percent = spread_percent - exchange_fee_percent
        
        if net_profit_percent <= 0:
            return {
                'feasible': False,
                'reason': 'Spread ne pokrývá poplatky'
            }
        
        # Can use full capital for market making
        effective_capital = self.total_capital
        
        # Assume each fill is a fraction of capital
        capital_per_fill = effective_capital * 0.2  # 20% per order
        
        # Daily profit
        profit_per_fill = capital_per_fill * (net_profit_percent / 100)
        daily_profit = profit_per_fill * fills_per_day * success_rate
        
        # Monthly and yearly projections
        monthly_profit = daily_profit * 30
        yearly_profit = daily_profit * 365
        
        # ROI calculations
        daily_roi = (daily_profit / effective_capital) * 100
        monthly_roi = (monthly_profit / effective_capital) * 100
        yearly_roi = (yearly_profit / effective_capital) * 100
        
        return {
            'feasible': True,
            'effective_capital': effective_capital,
            'net_profit_percent': net_profit_percent,
            'profit_per_fill': profit_per_fill,
            'fills_per_day': fills_per_day,
            'daily_profit': daily_profit,
            'monthly_profit': monthly_profit,
            'yearly_profit': yearly_profit,
            'daily_roi': daily_roi,
            'monthly_roi': monthly_roi,
            'yearly_roi': yearly_roi
        }
    
    def print_report(self):
        """Print comprehensive profit prediction report."""
        print("=" * 70)
        print("PREDIKCE ZISKU - BURZA TRADING BOT")
        print("=" * 70)
        print()
        print(f"💰 Váš kapitál:")
        print(f"   Binance:        ${self.binance_capital:.2f}")
        print(f"   Kraken:         ${self.kraken_capital:.2f}")
        print(f"   Celkem:         ${self.total_capital:.2f}")
        print()
        print("=" * 70)
        
        # Conservative scenario (realistic)
        print("\n📊 KONZERVATIVNÍ SCÉNÁŘ (Realistický)")
        print("-" * 70)
        
        arb_conservative = self.estimate_arbitrage_profit(
            avg_spread_percent=0.3,
            trades_per_day=3,
            success_rate=0.8,
            exchange_fee_percent=0.1
        )
        
        if arb_conservative['feasible']:
            print(f"\n🔄 Arbitráž:")
            print(f"   Efektivní kapitál:  ${arb_conservative['effective_capital']:.2f}")
            print(f"   Čistý zisk/obchod:  {arb_conservative['net_profit_percent']:.2f}%")
            print(f"   Obchodů denně:      {arb_conservative['trades_per_day']}")
            print(f"   Zisk/obchod:        ${arb_conservative['profit_per_trade']:.2f}")
            print()
            print(f"   📈 Denní zisk:      ${arb_conservative['daily_profit']:.2f} ({arb_conservative['daily_roi']:.2f}% ROI)")
            print(f"   📈 Měsíční zisk:    ${arb_conservative['monthly_profit']:.2f} ({arb_conservative['monthly_roi']:.1f}% ROI)")
            print(f"   📈 Roční zisk:      ${arb_conservative['yearly_profit']:.2f} ({arb_conservative['yearly_roi']:.0f}% ROI)")
        
        mm_conservative = self.estimate_market_making_profit(
            spread_percent=0.5,
            fills_per_day=5,
            success_rate=0.7,
            exchange_fee_percent=0.1
        )
        
        if mm_conservative['feasible']:
            print(f"\n📊 Market Making:")
            print(f"   Efektivní kapitál:  ${mm_conservative['effective_capital']:.2f}")
            print(f"   Čistý zisk/fill:    {mm_conservative['net_profit_percent']:.2f}%")
            print(f"   Fillů denně:        {mm_conservative['fills_per_day']}")
            print(f"   Zisk/fill:          ${mm_conservative['profit_per_fill']:.2f}")
            print()
            print(f"   📈 Denní zisk:      ${mm_conservative['daily_profit']:.2f} ({mm_conservative['daily_roi']:.2f}% ROI)")
            print(f"   📈 Měsíční zisk:    ${mm_conservative['monthly_profit']:.2f} ({mm_conservative['monthly_roi']:.1f}% ROI)")
            print(f"   📈 Roční zisk:      ${mm_conservative['yearly_profit']:.2f} ({mm_conservative['yearly_roi']:.0f}% ROI)")
        
        # Optimistic scenario
        print("\n\n📊 OPTIMISTICKÝ SCÉNÁŘ (Příznivé podmínky)")
        print("-" * 70)
        
        arb_optimistic = self.estimate_arbitrage_profit(
            avg_spread_percent=0.6,
            trades_per_day=8,
            success_rate=0.85,
            exchange_fee_percent=0.1
        )
        
        if arb_optimistic['feasible']:
            print(f"\n🔄 Arbitráž:")
            print(f"   📈 Denní zisk:      ${arb_optimistic['daily_profit']:.2f} ({arb_optimistic['daily_roi']:.2f}% ROI)")
            print(f"   📈 Měsíční zisk:    ${arb_optimistic['monthly_profit']:.2f} ({arb_optimistic['monthly_roi']:.1f}% ROI)")
            print(f"   📈 Roční zisk:      ${arb_optimistic['yearly_profit']:.2f} ({arb_optimistic['yearly_roi']:.0f}% ROI)")
        
        mm_optimistic = self.estimate_market_making_profit(
            spread_percent=0.5,
            fills_per_day=15,
            success_rate=0.8,
            exchange_fee_percent=0.1
        )
        
        if mm_optimistic['feasible']:
            print(f"\n📊 Market Making:")
            print(f"   📈 Denní zisk:      ${mm_optimistic['daily_profit']:.2f} ({mm_optimistic['daily_roi']:.2f}% ROI)")
            print(f"   📈 Měsíční zisk:    ${mm_optimistic['monthly_profit']:.2f} ({mm_optimistic['monthly_roi']:.1f}% ROI)")
            print(f"   📈 Roční zisk:      ${mm_optimistic['yearly_profit']:.2f} ({mm_optimistic['yearly_roi']:.0f}% ROI)")
        
        # Combined estimate (both strategies)
        print("\n\n📊 KOMBINOVANÁ STRATEGIE (Arbitráž + Market Making)")
        print("-" * 70)
        
        combined_daily = arb_conservative['daily_profit'] + mm_conservative['daily_profit']
        combined_monthly = arb_conservative['monthly_profit'] + mm_conservative['monthly_profit']
        combined_yearly = arb_conservative['yearly_profit'] + mm_conservative['yearly_profit']
        combined_monthly_roi = (combined_monthly / self.total_capital) * 100
        combined_yearly_roi = (combined_yearly / self.total_capital) * 100
        
        print(f"\n   📈 Denní zisk:      ${combined_daily:.2f}")
        print(f"   📈 Měsíční zisk:    ${combined_monthly:.2f} ({combined_monthly_roi:.1f}% ROI)")
        print(f"   📈 Roční zisk:      ${combined_yearly:.2f} ({combined_yearly_roi:.0f}% ROI)")
        
        print("\n" + "=" * 70)
        print("⚠️  DŮLEŽITÉ POZNÁMKY:")
        print("=" * 70)
        print("""
1. Tyto odhady jsou TEORETICKÉ a založené na průměrných podmínkách
2. Skutečné zisky závisí na:
   - Volatilitě trhu (více volatility = více příležitostí)
   - Likviditě (velké obchody mohou ovlivnit ceny)
   - Konkurenci (ostatní boti hledají stejné příležitosti)
   - Rychlosti exekuce (pomalé připojení = ztracené příležitosti)
   - Poplatcích burzy (liší se podle VIP úrovně)

3. S vaším kapitálem ($50 Binance + $30 Kraken):
   - ✅ Můžete začít a testovat strategie
   - ⚠️  Arbitráž je omezena nižším kapitálem ($30)
   - ✅ Market making může využít celý kapitál
   - 💡 Doporučení: Začít s market makingem na jedné burze

4. Pro zvýšení zisků:
   - 📈 Zvětšit kapitál na obou burzách
   - ⚡ Použít VPS s rychlým připojením
   - 🔧 Optimalizovat parametry (MIN_PROFIT_THRESHOLD, CHECK_INTERVAL)
   - 📊 Sledovat a analyzovat výsledky

5. Začněte s DRY-RUN módem a sledujte příležitosti několik dní!
        """)
        
        print("=" * 70)
        print(f"📅 Predikce vygenerována: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)


def main():
    """Main entry point."""
    print("\n")
    
    # Default values from user's comment
    binance_capital = 50.0
    kraken_capital = 30.0
    
    # Check for command line arguments
    if len(sys.argv) >= 3:
        try:
            binance_capital = float(sys.argv[1])
            kraken_capital = float(sys.argv[2])
        except ValueError:
            print("Použití: python profit_prediction.py [binance_kapital] [kraken_kapital]")
            print(f"Příklad: python profit_prediction.py 50 30")
            sys.exit(1)
    
    predictor = ProfitPredictor(binance_capital, kraken_capital)
    predictor.print_report()
    
    print("\n💡 Tip: Pro vlastní kapitál použijte:")
    print(f"   python profit_prediction.py [binance_USDT] [kraken_USDT]")
    print(f"   Příklad: python profit_prediction.py 100 200\n")


if __name__ == '__main__':
    main()

#!/bin/bash
# KOMPLETNÍ RESET A NASTAVENÍ - Burza Trading Bot
# Zruší všechny otevřené obchody a nastaví vše pro USDC trading

set -e  # Exit on error

echo "================================================================="
echo "   BURZA BOT - KOMPLETNÍ RESET A FRESH START"
echo "================================================================="
echo ""
echo "Tento skript provede:"
echo "  1. Zruší VŠECHNY otevřené obchody na Binance a Kraken"
echo "  2. Nastaví konfiguraci pro BTC/USDC trading"
echo "  3. Vytvoří .env soubor s vašimi API klíči"
echo "  4. Spustí bot v agresivním režimu"
echo ""
read -p "Pokračovat? (y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Přerušeno."
    exit 1
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_error() { echo -e "${RED}✗ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠ $1${NC}"; }

echo ""
echo "================================================================="
echo "KROK 1: Zadání API klíčů"
echo "================================================================="

# Binance API keys
echo ""
echo "--- Binance API ---"
read -p "Binance API Key: " BINANCE_KEY
read -s -p "Binance API Secret (nebude vidět): " BINANCE_SECRET
echo ""

# Kraken API keys  
echo ""
echo "--- Kraken API ---"
read -p "Kraken API Key: " KRAKEN_KEY
read -s -p "Kraken API Secret (nebude vidět): " KRAKEN_SECRET
echo ""
echo ""

# Validate keys are not empty
if [[ -z "$BINANCE_KEY" ]] || [[ -z "$BINANCE_SECRET" ]]; then
    print_error "Binance klíče nesmí být prázdné!"
    exit 1
fi

if [[ -z "$KRAKEN_KEY" ]] || [[ -z "$KRAKEN_SECRET" ]]; then
    print_warning "Kraken klíče prázdné - bude použit pouze Binance"
    KRAKEN_KEY=""
    KRAKEN_SECRET=""
fi

print_success "API klíče zadány"

echo ""
echo "================================================================="
echo "KROK 2: Kontrola Python prostředí"
echo "================================================================="

# Check for virtual environment
if [ ! -d "venv" ]; then
    print_warning "Virtuální prostředí neexistuje, vytvářím..."
    python3 -m venv venv
    print_success "Virtuální prostředí vytvořeno"
fi

# Activate venv
source venv/bin/activate

# Install/update requirements
print_warning "Kontroluji závislosti..."
pip install -q -r requirements.txt
print_success "Závislosti aktuální"

echo ""
echo "================================================================="
echo "KROK 3: Vytvoření konfiguračního souboru"
echo "================================================================="

# Create .env file for USDC trading
cat > .env << EOF
# KONFIGURACE PRO USDC TRADING - FRESH START
# Automaticky vytvořeno: $(date)

# Binance API Configuration
BINANCE_API_KEY=$BINANCE_KEY
BINANCE_API_SECRET=$BINANCE_SECRET

# Kraken API Configuration
KRAKEN_API_KEY=$KRAKEN_KEY
KRAKEN_API_SECRET=$KRAKEN_SECRET

# AGRESIVNÍ Trading Configuration pro USDC
MIN_PROFIT_THRESHOLD=0.1
MAX_TRADE_AMOUNT=15
CHECK_INTERVAL=2

# MULTI-PAIR MODE - obchoduje VŠECHNY páry s USDC!
MULTI_PAIR_MODE=true
QUOTE_CURRENCY=USDC
TRADING_PAIR=BTC/USDC

# Risk Management
MAX_POSITION_SIZE=80
STOP_LOSS_PERCENT=1.5

# SCALPING MODE - pro časté malé zisky
SCALPING_MODE=true
SCALPING_PROFIT_TARGET=0.15
SCALPING_MIN_TRADE=10
EOF

print_success "Konfigurační soubor .env vytvořen pro MULTI-PAIR USDC trading"
print_info "Bot bude obchodovat VŠECHNY dostupné páry s USDC!"

echo ""
echo "================================================================="
echo "KROK 4: Zrušení všech otevřených obchodů"
echo "================================================================="

# Create Python script to cancel all orders
cat > cancel_all_orders.py << 'EOFPYTHON'
#!/usr/bin/env python3
"""
Zruší všechny otevřené obchody na Binance a Kraken
"""
import os
import sys
from dotenv import load_dotenv
import ccxt

load_dotenv()

def cancel_all_orders():
    """Cancel all open orders on both exchanges"""
    cancelled_count = 0
    
    # Binance
    print("\n--- BINANCE ---")
    binance_key = os.getenv('BINANCE_API_KEY')
    binance_secret = os.getenv('BINANCE_API_SECRET')
    
    if binance_key and binance_secret:
        try:
            binance = ccxt.binance({
                'apiKey': binance_key,
                'secret': binance_secret,
                'enableRateLimit': True,
            })
            
            # Get all open orders
            open_orders = binance.fetch_open_orders()
            
            if len(open_orders) == 0:
                print("✓ Žádné otevřené obchody na Binance")
            else:
                print(f"Nalezeno {len(open_orders)} otevřených obchodů")
                
                for order in open_orders:
                    try:
                        symbol = order['symbol']
                        order_id = order['id']
                        binance.cancel_order(order_id, symbol)
                        print(f"  ✓ Zrušen: {symbol} (ID: {order_id})")
                        cancelled_count += 1
                    except Exception as e:
                        print(f"  ✗ Chyba při rušení {symbol}: {e}")
                        
        except Exception as e:
            print(f"✗ Chyba Binance: {e}")
    else:
        print("⚠ Binance klíče nejsou nastaveny")
    
    # Kraken
    print("\n--- KRAKEN ---")
    kraken_key = os.getenv('KRAKEN_API_KEY')
    kraken_secret = os.getenv('KRAKEN_API_SECRET')
    
    if kraken_key and kraken_secret:
        try:
            kraken = ccxt.kraken({
                'apiKey': kraken_key,
                'secret': kraken_secret,
                'enableRateLimit': True,
            })
            
            # Get all open orders
            open_orders = kraken.fetch_open_orders()
            
            if len(open_orders) == 0:
                print("✓ Žádné otevřené obchody na Kraken")
            else:
                print(f"Nalezeno {len(open_orders)} otevřených obchodů")
                
                for order in open_orders:
                    try:
                        symbol = order['symbol']
                        order_id = order['id']
                        kraken.cancel_order(order_id, symbol)
                        print(f"  ✓ Zrušen: {symbol} (ID: {order_id})")
                        cancelled_count += 1
                    except Exception as e:
                        print(f"  ✗ Chyba při rušení {symbol}: {e}")
                        
        except Exception as e:
            print(f"✗ Chyba Kraken: {e}")
    else:
        print("⚠ Kraken klíče nejsou nastaveny")
    
    print(f"\n✓ Celkem zrušeno: {cancelled_count} obchodů")
    return cancelled_count

if __name__ == "__main__":
    try:
        cancel_all_orders()
    except Exception as e:
        print(f"\n✗ Kritická chyba: {e}")
        sys.exit(1)
EOFPYTHON

chmod +x cancel_all_orders.py

# Run the cancellation script
print_warning "Ruším všechny otevřené obchody..."
python cancel_all_orders.py

if [ $? -eq 0 ]; then
    print_success "Všechny obchody zrušeny"
else
    print_error "Chyba při rušení obchodů - pokračuji"
fi

# Clean up the temporary script
rm -f cancel_all_orders.py

echo ""
echo "================================================================="
echo "KROK 5: Test konfigurace"
echo "================================================================="

print_warning "Testuji připojení k burzám..."
python test_bot.py

if [ $? -ne 0 ]; then
    print_error "Testy selhaly!"
    echo ""
    echo "Zkontrolujte:"
    echo "  1. API klíče jsou správně"
    echo "  2. API mají povolení pro trading"
    echo "  3. BTC/USDC je dostupné na obou burzách"
    echo ""
    exit 1
fi

print_success "Vše funguje správně!"

echo ""
echo "================================================================="
echo "KROK 6: Spuštění bota"
echo "================================================================="

echo ""
echo "Vyberte režim spuštění:"
echo "  1) DRY-RUN (simulace, bez skutečných obchodů) - DOPORUČENO"
echo "  2) LIVE TRADING (skutečné obchody s penězi!)"
echo "  3) Nespouštět, jen přejdu k dokončení"
echo ""
read -p "Volba (1/2/3): " -n 1 -r RUN_MODE
echo ""

case $RUN_MODE in
    1)
        echo ""
        print_success "Spouštím v DRY-RUN módu..."
        echo ""
        echo "Bot běží! Sledujte výstup:"
        echo "  - Žádné skutečné obchody nebudou provedeny"
        echo "  - Uvidíte co by bot dělal"
        echo "  - Pro zastavení: Ctrl+C"
        echo ""
        python bot.py
        ;;
    2)
        echo ""
        print_warning "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        print_warning "!! POZOR: Spouštíte LIVE TRADING se skutečnými penězi !!"
        print_warning "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        echo ""
        read -p "Opravdu spustit LIVE trading? Zadejte 'YES' pro potvrzení: " CONFIRM
        
        if [ "$CONFIRM" = "YES" ]; then
            print_success "Spouštím LIVE trading..."
            echo ""
            echo "Bot běží v LIVE režimu! Provádí skutečné obchody!"
            echo "  - Sledujte obchody na burzách"
            echo "  - Pro zastavení: Ctrl+C"
            echo ""
            python bot.py --live
        else
            print_error "Potvrzení nebylo zadáno správně. Přerušeno."
            exit 1
        fi
        ;;
    3)
        echo ""
        print_info "Bot nebyl spuštěn."
        ;;
    *)
        print_error "Neplatná volba"
        exit 1
        ;;
esac

echo ""
echo "================================================================="
echo "HOTOVO!"
echo "================================================================="
echo ""
echo "✓ Všechny obchody zrušeny"
echo "✓ Konfigurace nastavena pro BTC/USDC"
echo "✓ Bot připraven k použití"
echo ""
echo "Pro ruční spuštění:"
echo "  source venv/bin/activate"
echo "  python bot.py              # dry-run"
echo "  python bot.py --live       # live trading"
echo ""
echo "Pro sledování logů:"
echo "  tail -f logs/bot.log"
echo ""

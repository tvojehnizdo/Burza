#!/bin/bash
# Quick start script for AGGRESSIVE HIGH-FREQUENCY trading
# For maximum returns starting TODAY

echo "=========================================="
echo "AGRESIVNÍ REŽIM - MAXIMÁLNÍ VÝNOS"
echo "=========================================="
echo ""

# Check if .env exists
if [ -f .env ]; then
    echo "⚠️  .env soubor již existuje."
    read -p "Přepsat agresivní konfigurací? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Zachováno stávající .env"
    else
        cp .env.aggressive .env
        echo "✓ Nahrána agresivní konfigurace"
    fi
else
    if [ -f .env.aggressive ]; then
        cp .env.aggressive .env
        echo "✓ Vytvořen .env z agresivní šablony"
    else
        cp .env.example .env
        echo "⚠️  Použita standardní .env.example"
    fi
fi

echo ""
echo "=========================================="
echo "KONFIGURACE"
echo "=========================================="
echo ""
echo "Aktuální nastavení v .env:"
echo "---"
grep -E "^(MIN_PROFIT_THRESHOLD|MAX_TRADE_AMOUNT|CHECK_INTERVAL|SCALPING)" .env 2>/dev/null || echo "Konfigurace nenalezena"
echo "---"
echo ""

# Check API keys
if grep -q "your_.*_api_key_here" .env 2>/dev/null; then
    echo "❌ API klíče nejsou nastaveny!"
    echo ""
    echo "Upravte .env soubor a vložte své API klíče:"
    echo "  nano .env"
    echo ""
    read -p "Pokračovat v dry-run bez API klíčů? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Ukončuji. Nastavte API klíče a spusťte znovu."
        exit 1
    fi
    echo "⚠️  Pokračuji bez API klíčů (pouze simulace)"
fi

echo ""
echo "=========================================="
echo "REŽIM SPUŠTĚNÍ"
echo "=========================================="
echo ""
echo "1) DRY-RUN (doporučeno pro start) - simulace obchodů"
echo "2) LIVE - skutečné obchody s reálnými penězi!"
echo ""
read -p "Vyberte režim (1/2): " -n 1 -r
echo
echo ""

if [[ $REPLY =~ ^[2]$ ]]; then
    echo "================================================"
    echo "⚠️⚠️⚠️  VAROVÁNÍ - LIVE TRADING MODE  ⚠️⚠️⚠️"
    echo "================================================"
    echo ""
    echo "Toto bude provádět SKUTEČNÉ obchody!"
    echo "S vaším kapitálem budou prováděny REÁLNÉ transakce!"
    echo ""
    echo "Agresivní nastavení = vysoká frekvence obchodů"
    echo ""
    read -p "Opravdu chcete pokračovat? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Zrušeno. Použijte dry-run pro testování."
        exit 0
    fi
    
    echo ""
    echo "🚀 Spouštím bot v LIVE režimu..."
    echo "📊 Sledujte log v jiném okně: tail -f trading_bot.log"
    echo ""
    sleep 2
    python bot.py --live
else
    echo "🧪 Spouštím bot v DRY-RUN režimu..."
    echo ""
    echo "📊 Sledujte kolik příležitostí bot nachází!"
    echo "💡 Po 15-30 minutách budete vědět, zda je to ziskové"
    echo ""
    sleep 2
    python bot.py
fi

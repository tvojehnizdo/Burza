#!/bin/bash

# BEZPEČNÝ START SCRIPT PRO TRADING BOT
# Kontroluje konfiguraci a spouští s risk managementem

echo "=========================================="
echo "🚀 BURZA TRADING BOT - BEZPEČNÝ START"
echo "=========================================="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  Soubor .env neexistuje!"
    echo ""
    echo "📝 Vytváření z bezpečné šablony..."
    cp .env.safe .env
    echo "✅ Vytvořen .env ze šablony .env.safe"
    echo ""
    echo "🔑 TEĎ MUSÍŠ VYPLNIT API KLÍČE!"
    echo ""
    echo "Otevři soubor .env a vyplň:"
    echo "  - BINANCE_API_KEY"
    echo "  - BINANCE_API_SECRET"
    echo ""
    echo "Pak spusť znovu: ./start_safe.sh"
    exit 1
fi

# Load environment
source .env

# Check if API keys are configured
if [ "$BINANCE_API_KEY" == "your_binance_api_key_here" ] || [ -z "$BINANCE_API_KEY" ]; then
    echo "❌ API klíče nejsou nakonfigurovány!"
    echo ""
    echo "Otevři .env a vyplň své Binance API klíče"
    echo "Pak spusť znovu."
    exit 1
fi

echo "✅ Konfigurace načtena"
echo ""
echo "📊 AKTUÁLNÍ NASTAVENÍ:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Trading Pair:        $TRADING_PAIR"
echo "Scalping Mode:       $SCALPING_MODE"
echo "Profit Target:       $SCALPING_PROFIT_TARGET%"
echo "Max Trade Amount:    \$${MAX_TRADE_AMOUNT}"
echo "Check Interval:      ${CHECK_INTERVAL}s"
echo "Stop Loss:           ${STOP_LOSS_PERCENT}%"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Ask for mode
echo "🎯 VYBER REŽIM:"
echo "1) DRY-RUN (doporučeno) - Simulace bez skutečných obchodů"
echo "2) LIVE TRADING - Skutečné obchody s reálnými penězi"
echo ""
read -p "Zvol možnost (1/2): " mode

if [ "$mode" == "1" ]; then
    echo ""
    echo "🧪 SPOUŠTÍM DRY-RUN MODE"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "✅ Žádné skutečné obchody"
    echo "✅ Simulace založená na reálných cenách"
    echo "✅ Test strategie a risk managementu"
    echo ""
    echo "💡 SLEDUJ OUTPUT:"
    echo "   - 'Simulated WIN' = úspěšný obchod"
    echo "   - 'Simulated LOSS' = neúspěšný obchod"
    echo "   - 'Total P&L' = celkový zisk/ztráta"
    echo ""
    echo "⏹️  Pro zastavení: Ctrl+C"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    sleep 2
    python bot.py

elif [ "$mode" == "2" ]; then
    echo ""
    echo "⚠️  ⚠️  ⚠️  VAROVÁNÍ ⚠️  ⚠️  ⚠️"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Spouštíš LIVE TRADING režim!"
    echo ""
    echo "🛡️  Aktivní ochrana:"
    echo "   ✅ Max denní ztráta: \$$(echo \"$MAX_TRADE_AMOUNT * 5\" | bc)"
    echo "   ✅ Stop po 5 ztrátách v řadě"
    echo "   ✅ Stop-loss: ${STOP_LOSS_PERCENT}%"
    echo ""
    echo "💰 Trading s částkami: \$${MAX_TRADE_AMOUNT} per trade"
    echo ""
    echo "❗ Skutečné peníze budou použity!"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    read -p "Jsi si 100% jistý? (ano/ne): " confirm
    
    if [ "$confirm" == "ano" ]; then
        echo ""
        echo "🚀 SPOUŠTÍM LIVE TRADING..."
        echo ""
        echo "📊 Real-time monitoring aktivní"
        echo "⏹️  Pro zastavení: Ctrl+C"
        echo ""
        sleep 2
        python bot.py --live
    else
        echo ""
        echo "❌ Zrušeno. Spusť znovu když budeš připraven."
        exit 0
    fi
else
    echo "❌ Neplatná volba"
    exit 1
fi

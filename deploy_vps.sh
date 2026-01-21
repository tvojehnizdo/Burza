#!/bin/bash
# Automatizované nasazení Burza Trading Bot na VPS
# Kompletní setup včetně konfigurace, API klíčů a spuštění

set -e  # Exit on error

echo "================================================================="
echo "   BURZA TRADING BOT - AUTOMATICKÉ NASAZENÍ NA VPS"
echo "================================================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${YELLOW}ℹ $1${NC}"
}

# Check if running on Linux
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    print_error "Tento skript je určen pro Linux VPS"
    exit 1
fi

# Get current user and directory
CURRENT_USER=$(whoami)
INSTALL_DIR=$(pwd)

echo "Uživatel: $CURRENT_USER"
echo "Instalační adresář: $INSTALL_DIR"
echo ""

# Step 1: Update system
echo "================================================================="
echo "KROK 1: Aktualizace systému"
echo "================================================================="
sudo apt-get update -qq
print_success "Systém aktualizován"
echo ""

# Step 2: Install dependencies
echo "================================================================="
echo "KROK 2: Instalace závislostí"
echo "================================================================="
sudo apt-get install -y python3 python3-pip python3-venv git screen curl wget -qq
print_success "Závislosti nainstalovány (Python3, pip, git, screen)"
echo ""

# Step 3: Create virtual environment
echo "================================================================="
echo "KROK 3: Vytvoření Python virtual environment"
echo "================================================================="
if [ ! -d "venv" ]; then
    python3 -m venv venv
    print_success "Virtual environment vytvořen"
else
    print_info "Virtual environment již existuje"
fi

# Activate venv
source venv/bin/activate
print_success "Virtual environment aktivován"
echo ""

# Step 4: Install Python packages
echo "================================================================="
echo "KROK 4: Instalace Python balíčků"
echo "================================================================="
pip install --upgrade pip -q
pip install -r requirements.txt -q
print_success "Python balíčky nainstalovány"
echo ""

# Step 5: Configure API keys
echo "================================================================="
echo "KROK 5: Konfigurace API klíčů"
echo "================================================================="

if [ -f .env ]; then
    print_info ".env soubor již existuje"
    read -p "Chcete jej přepsat? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Zachován existující .env soubor"
    else
        rm .env
    fi
fi

if [ ! -f .env ]; then
    echo ""
    echo "Nyní zadejte vaše API klíče:"
    echo ""
    
    # Binance API
    read -p "Binance API Key (Enter pro přeskočení): " BINANCE_KEY
    if [ -n "$BINANCE_KEY" ]; then
        read -p "Binance API Secret: " BINANCE_SECRET
    fi
    
    echo ""
    
    # Kraken API  
    read -p "Kraken API Key (Enter pro přeskočení): " KRAKEN_KEY
    if [ -n "$KRAKEN_KEY" ]; then
        read -p "Kraken API Secret: " KRAKEN_SECRET
    fi
    
    echo ""
    
    # Trading configuration
    read -p "Režim obchodování (standard/aggressive) [aggressive]: " TRADING_MODE
    TRADING_MODE=${TRADING_MODE:-aggressive}
    
    # Create .env file
    if [ "$TRADING_MODE" = "aggressive" ]; then
        cp .env.aggressive .env
        print_success "Použita agresivní konfigurace"
    else
        cp .env.example .env
        print_success "Použita standardní konfigurace"
    fi
    
    # Update API keys in .env
    if [ -n "$BINANCE_KEY" ]; then
        sed -i "s/BINANCE_API_KEY=.*/BINANCE_API_KEY=$BINANCE_KEY/" .env
        sed -i "s/BINANCE_API_SECRET=.*/BINANCE_API_SECRET=$BINANCE_SECRET/" .env
        print_success "Binance API klíče nastaveny"
    fi
    
    if [ -n "$KRAKEN_KEY" ]; then
        sed -i "s/KRAKEN_API_KEY=.*/KRAKEN_API_KEY=$KRAKEN_KEY/" .env
        sed -i "s/KRAKEN_API_SECRET=.*/KRAKEN_API_SECRET=$KRAKEN_SECRET/" .env
        print_success "Kraken API klíče nastaveny"
    fi
fi

echo ""

# Step 6: Test configuration
echo "================================================================="
echo "KROK 6: Test konfigurace"
echo "================================================================="
python3 test_bot.py
if [ $? -eq 0 ]; then
    print_success "Všechny testy prošly!"
else
    print_error "Testy selhaly - zkontrolujte konfiguraci"
    exit 1
fi
echo ""

# Step 7: Setup systemd service
echo "================================================================="
echo "KROK 7: Nastavení systemd služby"
echo "================================================================="

read -p "Chcete nastavit bot jako systemd službu (automatický start)? (y/n): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Create service file
    cat > burza-bot-temp.service <<EOF
[Unit]
Description=Burza Trading Bot
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/bot.py
Restart=on-failure
RestartSec=30
StandardOutput=append:$INSTALL_DIR/trading_bot.log
StandardError=append:$INSTALL_DIR/trading_bot_error.log

[Install]
WantedBy=multi-user.target
EOF

    # Install service
    sudo mv burza-bot-temp.service /etc/systemd/system/burza-bot.service
    sudo systemctl daemon-reload
    sudo systemctl enable burza-bot
    print_success "Systemd služba vytvořena a povolena"
    
    SERVICE_INSTALLED=true
else
    SERVICE_INSTALLED=false
    print_info "Systemd služba nebude nastavena"
fi
echo ""

# Step 8: Initial dry-run test
echo "================================================================="
echo "KROK 8: Iniciální test (DRY-RUN)"
echo "================================================================="

read -p "Spustit rychlý dry-run test? (doporučeno) (y/n): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    print_info "Spouštím 30 sekundový dry-run test..."
    timeout 30 python3 bot.py || true
    echo ""
    print_success "Test dokončen - zkontrolujte výstup výše"
fi
echo ""

# Step 9: Choose run mode
echo "================================================================="
echo "KROK 9: Výběr režimu spuštění"
echo "================================================================="
echo ""
echo "Jak chcete bota spustit?"
echo ""
echo "1) Systemd služba (běží na pozadí, automatický restart)"
echo "2) Screen session (manuální kontrola)"
echo "3) DRY-RUN test (pouze simulace)"
echo "4) Nespouštět teď (manuální start později)"
echo ""

read -p "Vyberte možnost (1-4): " -n 1 -r RUN_MODE
echo
echo ""

case $RUN_MODE in
    1)
        if [ "$SERVICE_INSTALLED" = true ]; then
            echo "⚠️  VAROVÁNÍ: Toto spustí bota v LIVE režimu!"
            read -p "Opravdu chcete pokračovat? (yes/no): " confirm
            if [ "$confirm" = "yes" ]; then
                # Update service to run with --live flag
                sudo sed -i "s|ExecStart=.*|ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/bot.py --live|" /etc/systemd/system/burza-bot.service
                sudo systemctl daemon-reload
                sudo systemctl start burza-bot
                print_success "Bot spuštěn jako systemd služba!"
                echo ""
                echo "Užitečné příkazy:"
                echo "  sudo systemctl status burza-bot    # Zobrazit stav"
                echo "  sudo systemctl stop burza-bot      # Zastavit"
                echo "  sudo journalctl -u burza-bot -f    # Sledovat logy"
            else
                print_info "Spuštění zrušeno"
            fi
        else
            print_error "Systemd služba není nastavena"
        fi
        ;;
    2)
        echo "Spouštím v screen session..."
        screen -dmS burza-bot bash -c "source venv/bin/activate && python3 bot.py --live"
        print_success "Bot běží v screen session 'burza-bot'"
        echo ""
        echo "Připojení k session: screen -r burza-bot"
        echo "Odpojení ze session: Ctrl+A poté D"
        ;;
    3)
        echo "Spouštím DRY-RUN..."
        screen -dmS burza-bot-dryrun bash -c "source venv/bin/activate && python3 bot.py"
        print_success "Dry-run běží v screen session 'burza-bot-dryrun'"
        echo ""
        echo "Připojení k session: screen -r burza-bot-dryrun"
        ;;
    4)
        print_info "Bot nebude spuštěn automaticky"
        ;;
    *)
        print_warning "Neplatná volba"
        ;;
esac

echo ""
echo "================================================================="
echo "   NASAZENÍ DOKONČENO!"
echo "================================================================="
echo ""
print_success "Burza Trading Bot je nasazen a připraven k použití"
echo ""
echo "📍 Instalační adresář: $INSTALL_DIR"
echo "📍 Log soubor: $INSTALL_DIR/trading_bot.log"
echo "📍 Config: $INSTALL_DIR/.env"
echo ""
echo "🚀 RYCHLÉ PŘÍKAZY:"
echo ""
echo "  # Sledovat logy:"
echo "  tail -f $INSTALL_DIR/trading_bot.log"
echo ""
echo "  # Spustit dry-run manuálně:"
echo "  cd $INSTALL_DIR && source venv/bin/activate && python3 bot.py"
echo ""
echo "  # Spustit live trading:"
echo "  cd $INSTALL_DIR && source venv/bin/activate && python3 bot.py --live"
echo ""
echo "  # Predikce zisku:"
echo "  cd $INSTALL_DIR && source venv/bin/activate && python3 profit_prediction.py 50 30"
echo ""
if [ "$SERVICE_INSTALLED" = true ]; then
echo "  # Systemd služba:"
echo "  sudo systemctl status burza-bot"
echo "  sudo systemctl stop burza-bot"
echo "  sudo systemctl restart burza-bot"
echo "  sudo journalctl -u burza-bot -f"
echo ""
fi
echo "📖 Dokumentace:"
echo "  - README.md - hlavní dokumentace"
echo "  - MAX_PROFIT_GUIDE.md - průvodce pro max. výnos"
echo "  - QUICKSTART.md - rychlý start"
echo ""
print_success "Úspěšné nasazení! 🎉"
echo ""

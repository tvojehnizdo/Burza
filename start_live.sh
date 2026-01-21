#!/bin/bash
# Start the trading bot in LIVE mode
# WARNING: This will execute REAL trades!

echo "================================================"
echo "WARNING: Starting in LIVE trading mode!"
echo "This will execute REAL trades with REAL money!"
echo "================================================"
echo ""

# Check if .env file exists
if [ ! -f .env ]; then
    echo "Error: .env file not found!"
    echo "Please copy .env.example to .env and add your API keys."
    echo ""
    echo "Run: cp .env.example .env"
    echo "Then edit .env with your API keys."
    exit 1
fi

# Check if dependencies are installed
if ! python -c "import ccxt" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

echo "Starting bot in LIVE mode..."
python bot.py --live

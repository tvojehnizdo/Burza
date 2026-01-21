#!/bin/bash
# Start the trading bot in dry-run mode
# This script is for testing without real trades

echo "Starting Burza Trading Bot in DRY RUN mode..."
echo "No real trades will be executed."
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

echo "Starting bot..."
python bot.py

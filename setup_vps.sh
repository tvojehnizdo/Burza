#!/bin/bash
# Install and setup Burza trading bot on VPS

echo "Setting up Burza Trading Bot on VPS..."
echo ""

# Update system packages
echo "1. Updating system packages..."
sudo apt-get update

# Install Python and pip if not already installed
echo "2. Installing Python and pip..."
sudo apt-get install -y python3 python3-pip git

# Install screen for persistent sessions
echo "3. Installing screen..."
sudo apt-get install -y screen

# Clone repository if not already cloned
if [ ! -d "Burza" ]; then
    echo "4. Cloning repository..."
    git clone https://github.com/tvojehnizdo/Burza.git
    cd Burza
else
    echo "4. Repository already exists, updating..."
    cd Burza
    git pull
fi

# Install Python dependencies
echo "5. Installing Python dependencies..."
pip3 install -r requirements.txt

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "6. Creating .env file from template..."
    cp .env.example .env
    echo ""
    echo "⚠️  IMPORTANT: Please edit .env file and add your API keys!"
    echo "Run: nano .env"
    echo ""
else
    echo "6. .env file already exists"
fi

# Make scripts executable
chmod +x start_dryrun.sh
chmod +x start_live.sh

echo ""
echo "✓ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit .env file with your API keys: nano .env"
echo "2. Test in dry-run mode: ./start_dryrun.sh"
echo "3. For live trading: ./start_live.sh"
echo ""
echo "To run in background using screen:"
echo "  screen -S trading-bot"
echo "  ./start_dryrun.sh"
echo "  Press Ctrl+A then D to detach"
echo "  screen -r trading-bot  (to reattach)"
echo ""

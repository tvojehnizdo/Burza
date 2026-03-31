# Burza – Crypto Trading Bot 🤖💰

A fully functional, production-ready **24/7 cryptocurrency trading bot** that automatically trades on **Binance** and **Kraken** using two complementary strategies:

| Strategy | When it runs | How it earns |
|----------|-------------|--------------|
| **Arbitrage** | Always (primary) | Buys cheap on one exchange, sells high on the other |
| **Grid Trading** | Fallback (sideways market) | Places buy/sell limit orders at predefined price levels |

Optimised for a small account (~$59): 47 USDC on Binance + 12 USDT on Kraken.

---

## File Structure

```
Burza/
├── .env.example        ← Copy to .env and add your API keys
├── .gitignore
├── requirements.txt
├── config.py           ← Strategy parameters & risk settings
├── exchanges.py        ← Binance & Kraken connection (ccxt)
├── arbitrage.py        ← Arbitrage detection & execution
├── grid_trading.py     ← Grid trading logic
├── risk_manager.py     ← Position sizing, stop-loss, daily limits
├── logger.py           ← Trade logging & metrics dashboard
├── main.py             ← Main 24/7 bot loop
└── README.md
```

---

## Quick Start

### 1. Prerequisites

- Python 3.9 or newer
- Binance account with API key (spot trading enabled)
- Kraken account with API key (spot trading enabled)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure API keys

```bash
cp .env.example .env
```

Edit `.env` and paste your API credentials:

```env
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret
KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_API_SECRET=your_kraken_api_secret
```

> ⚠️ **Never commit `.env` to git.** It is listed in `.gitignore`.

### 4. (Optional) Tune configuration

Edit `config.py` to adjust:

| Setting | Default | Description |
|---------|---------|-------------|
| `TRADING_PAIRS` | BTC/USDT, ETH/USDT, SOL/USDT | Pairs to monitor |
| `ARBITRAGE_MIN_PROFIT_PCT` | 0.3% | Min net profit to trigger arbitrage |
| `MAX_TRADE_SIZE_USD` | $10 | Max position per trade |
| `STOP_LOSS_PCT` | 5% | Stop-loss per position |
| `DAILY_LOSS_LIMIT_USD` | $5 | Halt trading if daily loss exceeds this |
| `GRID_LEVELS` | 4 | Grid levels above/below mid-price |
| `GRID_SPACING_PCT` | 2% | Spacing between grid levels |
| `PRICE_CHECK_INTERVAL_SEC` | 5 | How often to check prices |

### 5. Run the bot

```bash
python main.py
```

The bot runs continuously. Stop it with **Ctrl+C** – it exits cleanly and prints a final metrics summary.

---

## Output & Logs

| File | Content |
|------|---------|
| `logs/bot.log` | Full timestamped log of all events |
| `logs/trades.csv` | Every completed trade (for analysis) |
| `logs/metrics.json` | Running totals: P&L, win rate, uptime |
| `logs/grid_state.json` | Active grid orders (survives restarts) |

A live dashboard is printed to the console every 60 seconds:

```
============================================================
  TRADING BOT DASHBOARD  –  2025-01-01 12:00:00 UTC
============================================================
  Trades       : 7
  Win rate     : 85.7%
  Net P&L      : +0.3412 USD
  Profit       : 0.3600 USD
  Loss         : 0.0188 USD
  Uptime       : 2.3 h
────────────────────────────────────────────────────────────
  Daily loss   : 0.0188 / 5.00 USD
  Open pos.    : 0 / 2
  Trading      : ✅ YES
============================================================
```

---

## Risk Management

- **Max $10 per trade** – protects capital on a small account
- **5% stop-loss** – automatic sell if price drops 5%
- **$5 daily loss limit** – bot halts trading for the day if exceeded
- **Max 2 simultaneous positions** – avoids over-exposure
- **$5 emergency reserve** – always kept untouched

---

## Realistic Expectations

| Metric | Target |
|--------|--------|
| Weekly ROI | 0.5–2% |
| Account size | ~$59 |
| Risk per day | ≤ $5 |

> Arbitrage opportunities on retail accounts are rare and small. Grid trading provides consistent small profits in sideways markets. Past performance does not guarantee future results. **Trade responsibly.**

---

## Running 24/7 on a Laptop

Keep the bot running even when you close the terminal:

```bash
# Linux / macOS – run in background with nohup
nohup python main.py > logs/nohup.log 2>&1 &

# Or use screen
screen -S burza
python main.py
# Detach: Ctrl+A then D
# Reattach: screen -r burza
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `ValueError: BINANCE_API_KEY … must be set` | Check your `.env` file |
| `ccxt.AuthenticationError` | Verify API key permissions (spot trading must be enabled) |
| Bot stops trading | Daily loss limit reached – resumes automatically next day |
| Grid orders not placed | Insufficient balance after emergency reserve |

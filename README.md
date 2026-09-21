# IMPULSE MAX 5K

Jedna aplikace pro rychlý PAPER replay a walk-forward výběr parametrů. Výchozí kapitál 5 000 Kč, spot, bez páky.

## Co dělá
- Bybit public 1m data pro BTC/ETH/SOL
- režimově adaptivní momentum + breakout + mean-reversion
- EMA, log returns, momentum, ATR, z-score, relative volume
- cost-aware gate (fee + slippage)
- volatility-based stop/take-profit
- dynamické position sizing podle risk budgetu
- hard max drawdown
- 70/30 train/test; parametry se nevybírají jen podle train zisku
- ledger každého obchodu, fees, P/L, equity, PF, win rate, DD
- LIVE je záměrně vypnutý, dokud replay/PAPER neprokáže robustnost

## Start Windows
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
$env:START_CAPITAL="5000"
uvicorn app:app --host 127.0.0.1 --port 8765
```
Pak otevřít http://127.0.0.1:8765 a kliknout **Spustit optimalizaci + replay**.

## Další brána
LIVE adapter se přidá až po vyhodnocení out-of-sample výsledků. API klíče nikdy necommitovat; withdrawals vypnout.

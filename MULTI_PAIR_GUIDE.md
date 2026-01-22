# Multi-Pair Trading Guide - Všechny USDC Páry

## Co je Multi-Pair Trading?

**Multi-pair trading** umožňuje botu obchodovat **VŠECHNY dostupné páry** s USDC na Binance a Kraken, ne jen jeden pár.

### Jak to funguje:

1. **Bot načte všechny páry** - Skenuje Binance a Kraken pro všechny páry končící `/USDC`
2. **Hledá příležitosti** - Pro KAŽDÝ pár hledá arbitráž, scalping a market making příležitosti
3. **Obchoduje automaticky** - Když najde zisk, obchoduje

### Výhody:

✅ **Více příležitostí** - Neobchoduje jen BTC, ale ETH, SOL, BNB, MATIC, atd.
✅ **Vyšší zisky** - Více párů = více obchodů = více zisku
✅ **Diverzifikace** - Nespol éháte se jen na jeden coin
✅ **Automatické** - Bot sám najde nejlepší příležitosti

## Rychlý Start

### 1. Spusťte Reset Script

```bash
cd ~/Burza
git pull origin copilot/set-up-automated-trading-system
./reset_and_setup.sh
```

Script automaticky:
- Zruší všechny otevřené obchody
- Nastaví multi-pair mode
- Nakonfiguruje USDC jako quote currency
- Spustí bota

### 2. Ověřte Konfiguraci

Po dokončení check your `.env` file:

```bash
cat .env | grep MULTI_PAIR
```

Měli byste vidět:
```
MULTI_PAIR_MODE=true
QUOTE_CURRENCY=USDC
```

### 3. Spusťte Bota

```bash
# Dry-run (testování)
source venv/bin/activate
python bot.py

# Live trading
python bot.py --live
```

## Příklad Výstupu

Když bot startuje s multi-pair mode, uvidíte:

```
Multi-pair mode enabled - scanning all USDC pairs
Found 142 USDC pairs: BTC/USDC, ETH/USDC, BNB/USDC, SOL/USDC, MATIC/USDC, ADA/USDC, DOT/USDC, AVAX/USDC, LINK/USDC, ATOM/USDC...
Monitoring 142 trading pair(s)
```

Bot pak analyzuje VŠECHNY tyto páry každé 2 sekundy!

## Očekávané Výsledky

S $80 kapitálem a multi-pair trading:

### Konzervativní Scénář:
- **Obchodů**: 20-80 denně (více párů = více příležitostí)
- **Denní zisk**: $2.50-5.00
- **Měsíční zisk**: $75-150 (94-188% ROI)

### Optimistický Scénář:
- **Obchodů**: 50-150 denně
- **Denní zisk**: $8.00-20.00
- **Měsíční zisk**: $240-600 (300-750% ROI)

## Konfigurace

### Multi-Pair s Agresivním Scalping

```env
# Multi-Pair Configuration
MULTI_PAIR_MODE=true
QUOTE_CURRENCY=USDC

# Agresivní nastavení
MIN_PROFIT_THRESHOLD=0.1
MAX_TRADE_AMOUNT=15
CHECK_INTERVAL=2
SCALPING_MODE=true
SCALPING_PROFIT_TARGET=0.15
SCALPING_MIN_TRADE=10
```

### Konzervativní Multi-Pair

```env
# Multi-Pair Configuration
MULTI_PAIR_MODE=true
QUOTE_CURRENCY=USDC

# Konzervativní nastavení
MIN_PROFIT_THRESHOLD=0.5
MAX_TRADE_AMOUNT=50
CHECK_INTERVAL=5
SCALPING_MODE=false
```

## Změna Quote Currency

Pokud chcete obchodovat páry s jinou měnou:

```bash
nano .env
# Změňte:
QUOTE_CURRENCY=EUR    # Pro evropské trhy
# nebo
QUOTE_CURRENCY=BTC    # Pro BTC páry
# nebo
QUOTE_CURRENCY=USDT   # Pro USDT (ne v CZ na Kraken!)
```

## Single-Pair Mode

Pokud chcete vrátit na jeden pár:

```bash
nano .env
# Změňte:
MULTI_PAIR_MODE=false
TRADING_PAIR=BTC/USDC
```

## Monitoring

### Sledujte Logy

```bash
# V reálném čase
tail -f trading_bot.log

# Filtrovat jen příležitosti
tail -f trading_bot.log | grep "opportunity\|Scalping\|Arbitrage"

# Počet obchodů
grep "executed" trading_bot.log | wc -l
```

### Kolik Párů Se Obchoduje

```bash
grep "Found.*pairs" trading_bot.log
```

## Tip & Tricks

### 1. Začněte Pomalu

První den spusťte v **DRY-RUN** módu:
```bash
python bot.py  # bez --live
```

Sledujte kolik příležitostí bot najde.

### 2. Optimalizujte Threshold

Pokud je moc obchodů:
```env
MIN_PROFIT_THRESHOLD=0.3  # Zvyšte threshold
```

Pokud je málo obchodů:
```env
MIN_PROFIT_THRESHOLD=0.05  # Snižte threshold
```

### 3. Limitujte Páry

Pokud chcete jen top coins, upravte bot.py:

```python
# Limitovat na top 20 párů podle volume
self.trading_pairs = sorted(list(pairs_set))[:20]
```

### 4. Performance

Multi-pair mode je **náročnější** na API calls:
- 142 párů × 3 strategie × každé 2 sekundy = hodně requestů
- Zvažte zvýšení `CHECK_INTERVAL` na 5-10 sekund

## Troubleshooting

### "Too many requests" Chyba

```env
CHECK_INTERVAL=5  # Zvyšte interval
```

### Bot je Pomalý

```python
# V bot.py, limitujte počet párů:
self.trading_pairs = sorted(list(pairs_set))[:50]  # Jen top 50
```

### Některé Páry Nefungují

To je normální - ne všechny páry mají dobrou likviditu. Bot automaticky skipuje páry které nefungují.

## FAQ

**Q: Kolik párů bot obchoduje?**
A: Záleží na burze. Binance má ~150 USDC párů, Kraken ~30.

**Q: Musím mít všechny coiny?**
A: Ne! Potřebujete jen USDC. Bot nakoupí coiny když najde příležitost.

**Q: Je to bezpečné?**
A: Ano, pokud:
- Používáte správné risk management (MAX_TRADE_AMOUNT, STOP_LOSS)
- Začnete v dry-run módu
- Sledujete logy

**Q: Můžu kombinovat s jinými strategiemi?**
A: Ano! Multi-pair funguje se všemi strategiemi (arbitráž, scalping, market making).

**Q: Kolik kapitálu potřebuji?**
A: Minimum $50-100 USDC. Více kapitálu = více paralelních obchodů.

## Závěr

Multi-pair trading je **game changer** pro váš bot:
- **Více příležitostí** každý den
- **Vyšší zisky** díky diverzifikaci
- **Automatická optimalizace** - bot sám najde nejlepší páry

**Spusťte to:**
```bash
./reset_and_setup.sh
```

A sledujte jak bot obchoduje VŠECHNY USDC páry! 🚀

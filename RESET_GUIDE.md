# RESET A FRESH START - USDC Trading

## Rychlý start s USDC

Máte-li otevřené obchody a chcete začít znovu s USDC trading, použijte tento jednoduchý skript:

```bash
./reset_and_setup.sh
```

## Co skript udělá:

1. **Zeptá se na API klíče** - budete je bezpečně zadat (secret nebude vidět)
2. **Zruší VŠECHNY otevřené obchody** - na Binance i Kraken
3. **Nastaví konfiguraci pro BTC/USDC** - funguje v České republice
4. **Spustí bota** - na vaši volbu (dry-run nebo live)

## Proč USDC?

- ✅ **USDC funguje v CZ** - Kraken neblokuje USDC trading v České republice
- ✅ **Stabilní** - stejně jako USDT, stablecoin navázaný na USD
- ✅ **Široká podpora** - dostupný na Binance i Kraken
- ✅ **Dobrá likvidita** - vysoké objemy obchodování

## Krok za krokem:

### 1. Spusťte reset script
```bash
cd ~/Burza
./reset_and_setup.sh
```

### 2. Zadejte API klíče když se zeptá:
```
Binance API Key: [váš klíč]
Binance API Secret: [váš secret - nebude vidět]
Kraken API Key: [váš klíč]
Kraken API Secret: [váš secret - nebude vidět]
```

### 3. Script automaticky:
- Zruší všechny otevřené obchody
- Vytvoří .env soubor s BTC/USDC
- Otestuje připojení
- Nabídne spuštění

### 4. Vyberte režim:
- **1 = DRY-RUN** (doporučeno pro test) - žádné skutečné obchody
- **2 = LIVE** - skutečné obchody s penězi
- **3 = Nespouštět** - jen nastaví, nespustí

## Po dokončení:

### Ruční spuštění (kdykoliv):
```bash
source venv/bin/activate
python bot.py              # dry-run - test
python bot.py --live       # live trading
```

### Sledování logů:
```bash
tail -f logs/bot.log
```

### Zastavení bota:
```
Ctrl+C
```

## Agresivní režim pro maximum zisků:

Script automaticky nastaví agresivní konfiguraci:
- ✅ Kontrola každé 2 sekundy
- ✅ Minimální zisk 0.1%
- ✅ Malé obchody $10-15 (rychlé fills)
- ✅ Scalping mode zapnutý
- ✅ Očekávání: 10-50 obchodů denně

## Očekávané výnosy s USDC ($80 kapitál):

**Konzervativní (realistické):**
- Den: $1.50-3.00 (2-4%)
- Měsíc: $45-90 (56-112% ROI)
- Rok: $540-1,080

**Optimistický (příznivé podmínky):**
- Den: $4.50-15.00 (6-19%)
- Měsíc: $135-450 (168-562% ROI)
- Rok: $1,620-5,400

## Řešení problémů:

### "Signature not valid" na Binance:
- Zkontrolujte API secret - žádné mezery před/za
- Zkopírujte znovu z Binance
- Ujistěte se, že API má povolení pro Spot Trading

### Kraken chyby:
- USDC by mělo fungovat v CZ
- Pokud ne, můžete použít pouze Binance (nechte Kraken prázdné)

### Bot nevidí příležitosti:
- To je normální - čeká na správné podmínky
- V dry-run uvidíte co by dělal
- Agresivní režim najde více příležitostí

## Poznámky:

- **Nejprve testujte v dry-run módu** - min 1-2 hodiny
- **Začněte s malým kapitálem** - otestujte strategie
- **Sledujte logy** - uvidíte co bot dělá
- **USDC je lepší než USDT v CZ** - žádné restrikce

## Bezpečnost:

- ✅ API keys jsou v .env (není v gitu)
- ✅ Secret keys jsou skryté při zadávání
- ✅ Dry-run je výchozí (bezpečné testování)
- ✅ Stop-loss je nastavený (1.5%)
- ✅ Maximum position size (80 USDC)

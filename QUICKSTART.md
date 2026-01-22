# Rychlý start - Burza Trading Bot

## Instalace na VPS

### 1. Připojení k VPS
```bash
ssh user@your-vps-ip
```

### 2. Automatická instalace
```bash
wget https://raw.githubusercontent.com/tvojehnizdo/Burza/main/setup_vps.sh
chmod +x setup_vps.sh
./setup_vps.sh
```

### 3. Konfigurace API klíčů
```bash
cd Burza
nano .env
```

Vyplňte vaše API klíče:
```env
BINANCE_API_KEY=váš_binance_klíč
BINANCE_API_SECRET=váš_binance_secret
KRAKEN_API_KEY=váš_kraken_klíč
KRAKEN_API_SECRET=váš_kraken_secret
```

Uložte: `Ctrl + O`, Enter, `Ctrl + X`

### 4. Testování (Dry Run)
```bash
./start_dryrun.sh
```

Bot běží v testovacím režimu - simuluje obchody bez skutečného provádění.
Sledujte log pro zjištěné příležitosti.

Pro zastavení: `Ctrl + C`

### 5. Spuštění na pozadí
```bash
screen -S trading-bot
./start_dryrun.sh
```

Odpojení od session: `Ctrl + A`, poté `D`

Zpět k botu:
```bash
screen -r trading-bot
```

## Live Trading (REÁLNÉ OBCHODY)

⚠️ **VAROVÁNÍ**: Toto provádí skutečné obchody!

```bash
./start_live.sh
```

## Monitorování

### Sledování logů
```bash
tail -f trading_bot.log
```

### Kontrola stavu
```bash
screen -ls  # Zobrazí běžící sessions
```

## Spuštění jako služba (automatický start po restartu VPS)

1. Upravte cestu v `burza-bot.service`
2. Nainstalujte službu:
```bash
sudo cp burza-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable burza-bot
sudo systemctl start burza-bot
```

3. Kontrola stavu:
```bash
sudo systemctl status burza-bot
```

4. Sledování logů:
```bash
sudo journalctl -u burza-bot -f
```

## Nastavení strategie

Upravte `.env` pro změnu parametrů:

### Arbitráž
- `MIN_PROFIT_THRESHOLD=0.5` - Minimální zisk pro provedení obchodu (%)
- `MAX_TRADE_AMOUNT=100` - Maximální částka obchodu (USDT)

### Market Making
- Spread je fixní 0.5% (lze upravit v kódu)

### Risk Management
- `MAX_POSITION_SIZE=1000` - Maximální velikost pozice
- `STOP_LOSS_PERCENT=2.0` - Stop loss procento

### Timing
- `CHECK_INTERVAL=10` - Jak často kontrolovat trh (sekundy)

## Běžné problémy

### Bot se nezapne
```bash
# Zkontrolujte Python
python3 --version

# Reinstalujte závislosti
pip3 install -r requirements.txt
```

### Chyby API
- Zkontrolujte, že API klíče jsou správné
- Ověřte, že máte povolen spot trading
- Zkontrolujte IP whitelist na burze

### Bot často restartuje
- Snižte `CHECK_INTERVAL` (bot může být rate-limited)
- Zkontrolujte stabilitu internetového připojení
- Sledujte `trading_bot.log` pro detaily

## Bezpečnostní doporučení

1. ✅ Nikdy nesdílejte API klíče
2. ✅ Používejte API klíče pouze s spot trading oprávněními
3. ✅ Nastavte IP whitelist na burzách
4. ✅ Začněte s malými částkami
5. ✅ Používejte 2FA na všech účtech
6. ✅ Pravidelně kontrolujte logy
7. ✅ Testujte změny v dry-run režimu

## Kontakt a podpora

Pro problémy a otázky: GitHub Issues

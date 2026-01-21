# Burza - Automated Trading Bot

Automatický obchodní systém pro Binance a Kraken burzy s podporou arbitráže, market makingu a high-frequency scalpingu.

## 🚀 RYCHLÉ NASAZENÍ NA VPS

### Metoda 1: Automatický deployment (DOPORUČENO)
```bash
# Připojte se na VPS
ssh your-user@your-vps-ip

# Spusťte automatický deployment
git clone https://github.com/tvojehnizdo/Burza.git
cd Burza
./deploy_vps.sh
```

Script se postará o vše:
- ✅ Instalace závislostí
- ✅ Nastavení Python prostředí
- ✅ Konfigurace API klíčů (interaktivně)
- ✅ Spuštění bota (systemd nebo screen)

📖 **Detailní návod**: [VPS_DEPLOYMENT.md](VPS_DEPLOYMENT.md)

### Metoda 2: Docker (nejjednodušší)
```bash
# Na VPS s Dockerem
git clone https://github.com/tvojehnizdo/Burza.git
cd Burza
cp .env.aggressive .env
nano .env  # Vyplňte API klíče
docker-compose up -d burza-bot
```

📖 **Docker návod**: [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md)

---

## Funkce

- **Multi-Exchange Support**: Podpora pro Binance a Kraken
- **Arbitrážní Strategie**: Automatické využívání cenových rozdílů mezi burzami
- **Market Making**: Poskytování likvidity a zisk ze spreadu
- **Risk Management**: Ochrana kapitálu s nastavitelnými limity
- **Dry Run Mode**: Testování bez reálných obchodů
- **Logging**: Kompletní zaznamenávání všech operací

## Požadavky

- Python 3.8+
- API klíče pro Binance a/nebo Kraken
- VPS nebo server pro nepřetržitý provoz (doporučeno)

## Instalace

1. Klonujte repozitář:
```bash
git clone https://github.com/tvojehnizdo/Burza.git
cd Burza
```

2. Nainstalujte závislosti:
```bash
pip install -r requirements.txt
```

3. Vytvořte `.env` soubor s vašimi API klíči:
```bash
cp .env.example .env
```

4. Upravte `.env` soubor a vložte své API klíče:
```env
# Binance API klíče
BINANCE_API_KEY=váš_binance_api_klíč
BINANCE_API_SECRET=váš_binance_api_secret

# Kraken API klíče  
KRAKEN_API_KEY=váš_kraken_api_klíč
KRAKEN_API_SECRET=váš_kraken_api_secret

# Nastavení obchodování
TRADING_PAIR=BTC/USDT
MIN_PROFIT_THRESHOLD=0.5
MAX_TRADE_AMOUNT=100
CHECK_INTERVAL=10
```

## Použití

### Predikce Zisku
Před spuštěním bota zjistěte, kolik můžete vydělat s vaším kapitálem:
```bash
python profit_prediction.py [binance_USDT] [kraken_USDT]
```

Příklad pro $50 na Binance a $30 na Kraken:
```bash
python profit_prediction.py 50 30
```

Nástroj zobrazí:
- Odhad denních, měsíčních a ročních zisků
- Konzervativní i optimistické scénáře
- ROI (návratnost investice) v procentech
- Doporučení pro váš kapitál

### Dry Run Mode (Testování bez reálných obchodů)
```bash
python bot.py
```

### Live Mode (Reálné obchody)
```bash
python bot.py --live
```

**⚠️ VAROVÁNÍ**: Live mode provádí skutečné obchody! Používejte opatrně.

## Nastavení

Všechna nastavení jsou v `.env` souboru:

| Parametr | Popis | Výchozí |
|----------|-------|---------|
| `BINANCE_API_KEY` | Binance API klíč | - |
| `BINANCE_API_SECRET` | Binance API secret | - |
| `KRAKEN_API_KEY` | Kraken API klíč | - |
| `KRAKEN_API_SECRET` | Kraken API secret | - |
| `TRADING_PAIR` | Obchodní pár | BTC/USDT |
| `MIN_PROFIT_THRESHOLD` | Minimální zisk pro arbitráž (%) | 0.5 |
| `MAX_TRADE_AMOUNT` | Maximální částka obchodu (USDT) | 100 |
| `CHECK_INTERVAL` | Interval kontroly (sekundy) | 10 |
| `MAX_POSITION_SIZE` | Maximální velikost pozice | 1000 |
| `STOP_LOSS_PERCENT` | Stop loss procento | 2.0 |

## Strategie

### 1. Arbitráž
Automaticky detekuje cenové rozdíly mezi Binance a Kraken:
- Kupuje na burze s nejnižší cenou
- Prodává na burze s nejvyšší cenou
- Provádí se pouze při zisku nad `MIN_PROFIT_THRESHOLD`

### 2. Market Making
Poskytuje likviditu na trhu:
- Umisťuje buy a sell příkazy kolem současné ceny
- Profituje ze spreadu mezi nákupem a prodejem
- Automaticky upravuje ceny podle tržních podmínek

## Bezpečnost

- ✅ Nikdy nesdílejte své API klíče
- ✅ Soubor `.env` je v `.gitignore` a nebude commitnutý
- ✅ Používejte API klíče pouze s oprávněním pro spot trading
- ✅ Nastavte IP whitelisting na burzách
- ✅ Začněte s malými částkami v dry-run módu
- ✅ Používejte 2FA na všech účtech

## Struktura Projektu

```
Burza/
├── bot.py                      # Hlavní soubor bota
├── config.py                   # Konfigurace
├── requirements.txt            # Python závislosti
├── .env.example               # Příklad konfigurace
├── exchanges/                 # Exchange implementace
│   ├── __init__.py
│   ├── base.py               # Základní interface
│   ├── binance_exchange.py   # Binance connector
│   └── kraken_exchange.py    # Kraken connector
└── strategies/                # Trading strategie
    ├── __init__.py
    ├── base.py               # Základní strategie
    ├── arbitrage.py          # Arbitrážní strategie
    └── market_maker.py       # Market making strategie
```

## Provoz na VPS

1. Připojte se na VPS přes SSH
2. Nainstalujte Python a git
3. Naklonujte repozitář a nastavte podle pokynů výše
4. Spusťte bota jako service nebo v screen/tmux:

```bash
# Použití screen
screen -S trading-bot
python bot.py
# Ctrl+A+D pro odpojení

# Zpět ke screen session
screen -r trading-bot
```

## Monitoring

Bot zaznamenává všechny operace do:
- Konzole (stdout)
- Souboru `trading_bot.log`

Sledujte log pro:
- Detekované příležitosti
- Provedené obchody
- Chyby a varování

## Podpora

Pro otázky a problémy vytvořte issue v GitHub repozitáři.

## Disclaimer

Tento software je poskytován "jak je" bez jakýchkoli záruk. Obchodování s kryptoměnami je vysoce rizikové. Používáte tento software na vlastní riziko. Autoři nejsou odpovědní za žádné finanční ztráty.

## Licence

MIT License
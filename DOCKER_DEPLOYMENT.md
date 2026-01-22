# DOCKER DEPLOYMENT - Nejjednodušší způsob nasazení

## 🐳 RYCHLÝ START S DOCKEREM

### Požadavky
- Docker nainstalován na VPS
- Docker Compose nainstalován

### Instalace Docker na VPS
```bash
# Oficiální instalace
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Docker Compose
sudo apt-get install -y docker-compose

# Přidat uživatele do docker skupiny
sudo usermod -aG docker $USER
newgrp docker
```

---

## 🚀 NASAZENÍ (3 příkazy)

### 1. Stáhnout repozitář
```bash
git clone https://github.com/tvojehnizdo/Burza.git
cd Burza
```

### 2. Nastavit .env soubor
```bash
# Pro agresivní režim:
cp .env.aggressive .env

# Upravit API klíče:
nano .env
```

Vyplňte vaše API klíče:
```env
BINANCE_API_KEY=váš_klíč
BINANCE_API_SECRET=váš_secret
KRAKEN_API_KEY=váš_klíč
KRAKEN_API_SECRET=váš_secret
```

### 3. Spustit bota
```bash
# Dry-run test (doporučeno první):
docker-compose run --rm burza-bot python3 bot.py

# Live trading:
docker-compose up -d burza-bot
```

**To je vše! Bot běží v Dockeru. 🎉**

---

## 📊 DOCKER PŘÍKAZY

### Základní operace
```bash
# Zobrazit běžící kontejnery
docker-compose ps

# Sledovat logy
docker-compose logs -f burza-bot

# Zastavit bota
docker-compose stop burza-bot

# Spustit znovu
docker-compose start burza-bot

# Restart
docker-compose restart burza-bot

# Kompletně odstranit
docker-compose down
```

### Aktualizace bota
```bash
# Stáhnout novou verzi
git pull

# Rebuild image
docker-compose build

# Restart s novou verzí
docker-compose up -d burza-bot
```

### Debug
```bash
# Vstoupit do kontejneru
docker-compose exec burza-bot bash

# Spustit testy
docker-compose run --rm burza-bot python3 test_bot.py

# Predikce zisku
docker-compose run --rm burza-bot python3 profit_prediction.py 50 30
```

---

## 📈 S MONITORINGEM

Spusťte s log viewerem (prohlížeč logů v browseru):

```bash
docker-compose --profile monitoring up -d
```

Pak otevřete v browseru:
```
http://your-vps-ip:8080
```

Uvidíte real-time logy bota v pěkném UI.

---

## 🔧 DOCKER VÝHODY

✅ **Izolované prostředí** - žádné konflikty s OS  
✅ **Jednoduché deployment** - 3 příkazy  
✅ **Auto-restart** - bot se automaticky restartuje při pádu  
✅ **Snadné aktualizace** - `git pull && docker-compose up -d`  
✅ **Portable** - funguje všude stejně  
✅ **Bezpečnější** - izolace od host systému  

---

## 📝 PŘÍKLADY POUŽITÍ

### Dry-run test na 1 hodinu
```bash
docker-compose run --rm burza-bot timeout 3600 python3 bot.py
```

### Live s agresivním módem
```bash
# .env už máte s SCALPING_MODE=true
docker-compose up -d burza-bot
```

### Změna konfigurace
```bash
# Upravte .env
nano .env

# Restart bota
docker-compose restart burza-bot
```

### Backup logů
```bash
# Logy jsou uloženy na host systému
cp trading_bot.log trading_bot.log.backup.$(date +%Y%m%d)
```

---

## 🔄 AUTOMATICKÉ STARTY

Docker automaticky startuje bot po restartu VPS díky `restart: unless-stopped` v docker-compose.yml.

Není potřeba žádná další konfigurace!

---

## 💾 PERZISTENCE DAT

Logy jsou uloženy na host systému:
- `./trading_bot.log` - hlavní log
- `./trading_bot_error.log` - error log
- `./.env` - konfigurace (read-only v kontejneru)

I když smažete kontejner, logy zůstanou.

---

## 🆘 TROUBLESHOOTING

### Port 8080 už používán
```bash
# Změňte port v docker-compose.yml:
ports:
  - "9090:8080"  # Místo 8080
```

### Container se neustále restartuje
```bash
# Zkontrolujte logy:
docker-compose logs burza-bot

# Nejčastější příčiny:
# 1. Chybné API klíče v .env
# 2. Síťový problém
# 3. Rate limiting od burzy
```

### Nedostatek místa
```bash
# Vyčistit staré Docker images
docker system prune -a

# Vyčistit staré logy
truncate -s 0 trading_bot.log
```

---

## 🎯 DOPORUČENÉ NASAZENÍ

Pro **produkční** použití doporučujeme:

```bash
# 1. Použít agresivní konfiguraci
cp .env.aggressive .env
nano .env  # Vyplnit API klíče

# 2. Spustit s monitoringem
docker-compose --profile monitoring up -d

# 3. Sledovat výkon první den
docker-compose logs -f burza-bot

# 4. Nastavit auto-updates (optional)
echo "0 3 * * * cd ~/Burza && git pull && docker-compose up -d" | crontab -
```

---

## 📊 POROVNÁNÍ: DOCKER vs NATIVE

| Feature | Docker | Native Python |
|---------|--------|---------------|
| Setup | ⭐⭐⭐⭐⭐ Velmi snadné | ⭐⭐⭐ Střední |
| Izolace | ⭐⭐⭐⭐⭐ Úplná | ⭐⭐ Částečná |
| Výkon | ⭐⭐⭐⭐ Minimální overhead | ⭐⭐⭐⭐⭐ Nativní |
| Updates | ⭐⭐⭐⭐⭐ Git pull + restart | ⭐⭐⭐ Více kroků |
| Monitoring | ⭐⭐⭐⭐⭐ Web UI dostupný | ⭐⭐⭐ Tail logs |
| Portable | ⭐⭐⭐⭐⭐ Funguje všude | ⭐⭐⭐ Závislé na OS |

**Doporučení**: Docker pro jednoduchost, Native pro maximální výkon.

---

## ✅ DOCKER CHECKLIST

- [ ] Docker nainstalován
- [ ] Docker Compose nainstalován
- [ ] Repozitář naklonován
- [ ] .env soubor vytvořen a vyplněn
- [ ] Dry-run test proběhl
- [ ] Live bot spuštěn
- [ ] Logy se sledují
- [ ] Monitoring (optional) běží
- [ ] Auto-restart funguje

---

**Happy Trading v Dockeru! 🐳💰**

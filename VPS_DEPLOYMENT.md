# VPS DEPLOYMENT GUIDE - Kompletní průvodce nasazením

## 🚀 JEDNODUCHÉ NASAZENÍ (1 příkaz)

### SSH připojení k VPS
```bash
ssh root@your-vps-ip
# NEBO
ssh your-username@your-vps-ip
```

### Automatické nasazení (DOPORUČENO)
```bash
# 1. Stáhněte deployment script
wget https://raw.githubusercontent.com/tvojehnizdo/Burza/main/deploy_vps.sh

# 2. Zkontrolujte script (doporučeno)
less deploy_vps.sh

# 3. Spusťte deployment
bash deploy_vps.sh

# NEBO pokud máte git:
git clone https://github.com/tvojehnizdo/Burza.git
cd Burza
./deploy_vps.sh
```

**To je vše!** Script se vás zeptá na:
- API klíče (Binance a Kraken)
- Režim (standard/aggressive)
- Způsob spuštění (systemd/screen/dry-run)

---

## 📋 CO DEPLOYMENT SCRIPT UDĚLÁ

1. ✅ Aktualizuje systém
2. ✅ Nainstaluje Python 3, pip, git, screen
3. ✅ Vytvoří Python virtual environment
4. ✅ Nainstaluje všechny dependencies (ccxt, pandas, atd.)
5. ✅ Nastaví .env soubor s vašimi API klíči
6. ✅ Spustí testy pro ověření konfigurace
7. ✅ (Volitelně) Nastaví systemd službu pro automatický start
8. ✅ (Volitelně) Spustí bota ve zvoleném režimu

---

## 🔧 RUČNÍ NASAZENÍ (krok za krokem)

Pokud preferujete manuální kontrolu:

### 1. Příprava VPS
```bash
# Aktualizace systému
sudo apt-get update
sudo apt-get upgrade -y

# Instalace základních nástrojů
sudo apt-get install -y python3 python3-pip python3-venv git screen curl
```

### 2. Klonování repozitáře
```bash
cd ~
git clone https://github.com/tvojehnizdo/Burza.git
cd Burza
```

### 3. Python virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Instalace dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Konfigurace
```bash
# Pro agresivní režim (doporučeno pro $50+$30):
cp .env.aggressive .env

# NEBO pro standardní režim:
cp .env.example .env

# Upravte API klíče:
nano .env
```

Vyplňte:
```env
BINANCE_API_KEY=váš_klíč_zde
BINANCE_API_SECRET=váš_secret_zde
KRAKEN_API_KEY=váš_klíč_zde
KRAKEN_API_SECRET=váš_secret_zde
```

### 6. Test konfigurace
```bash
python3 test_bot.py
```

### 7. Spuštění

**Dry-run test (doporučeno první):**
```bash
python3 bot.py
```

**Live trading:**
```bash
python3 bot.py --live
```

---

## 🔄 BĚŽÍCÍ BOT NA POZADÍ

### Metoda 1: Screen (jednoduchá)
```bash
# Spustit v screen
screen -S burza-bot
source venv/bin/activate
python3 bot.py --live

# Odpojit se: Ctrl+A poté D

# Připojit se zpět:
screen -r burza-bot

# Ukončit:
# Připojte se a stiskněte Ctrl+C
```

### Metoda 2: Systemd služba (automatický restart)
```bash
# Vytvořte službu
sudo nano /etc/systemd/system/burza-bot.service
```

Vložte:
```ini
[Unit]
Description=Burza Trading Bot
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/Burza
ExecStart=/home/YOUR_USERNAME/Burza/venv/bin/python3 /home/YOUR_USERNAME/Burza/bot.py --live
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/YOUR_USERNAME/Burza/trading_bot.log
StandardError=append:/home/YOUR_USERNAME/Burza/trading_bot_error.log

[Install]
WantedBy=multi-user.target
```

**⚠️ Nezapomeňte nahradit `YOUR_USERNAME` vaším uživatelským jménem!**

Aktivace služby:
```bash
sudo systemctl daemon-reload
sudo systemctl enable burza-bot
sudo systemctl start burza-bot

# Kontrola stavu:
sudo systemctl status burza-bot

# Sledování logů:
sudo journalctl -u burza-bot -f

# Zastavení:
sudo systemctl stop burza-bot

# Restart:
sudo systemctl restart burza-bot
```

---

## 📊 MONITOROVÁNÍ A ÚDRŽBA

### Sledování logů
```bash
# Real-time log
tail -f ~/Burza/trading_bot.log

# Poslední 100 řádků
tail -n 100 ~/Burza/trading_bot.log

# Hledat v logu
grep "Scalping opportunity" ~/Burza/trading_bot.log
```

### Statistiky výkonu
```bash
# Kolik obchodů bylo provedeno
grep "executed" ~/Burza/trading_bot.log | wc -l

# Příležitosti za poslední hodinu
grep "$(date +'%Y-%m-%d %H')" ~/Burza/trading_bot.log | grep "opportunity"
```

### Aktualizace bota
```bash
cd ~/Burza
git pull
source venv/bin/activate
pip install -r requirements.txt --upgrade

# Restart pokud běží jako služba:
sudo systemctl restart burza-bot

# NEBO restart screen session:
screen -X -S burza-bot quit
screen -dmS burza-bot bash -c "cd ~/Burza && source venv/bin/activate && python3 bot.py --live"
```

---

## 🔒 ZABEZPEČENÍ VPS

### 1. Firewall
```bash
# Povolit pouze SSH
sudo ufw allow 22/tcp
sudo ufw enable

# Zkontrolovat stav
sudo ufw status
```

### 2. Fail2Ban (ochrana proti brute-force)
```bash
sudo apt-get install -y fail2ban
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### 3. SSH klíče (místo hesel)
```bash
# Na VAŠEM počítači (ne VPS):
ssh-keygen -t rsa -b 4096

# Zkopírovat klíč na VPS:
ssh-copy-id your-username@your-vps-ip

# Pak zakažte hesla na VPS:
sudo nano /etc/ssh/sshd_config
# Nastavte: PasswordAuthentication no
sudo systemctl restart ssh
```

### 4. Pravidelné zálohy .env
```bash
# Vytvořte cron job pro zálohu
crontab -e

# Přidejte (zálohuje každý den v 2:00):
0 2 * * * cp ~/Burza/.env ~/Burza/.env.backup.$(date +\%Y\%m\%d)
```

---

## 🆘 ŘEŠENÍ PROBLÉMŮ

### Bot se nespustí
```bash
# Zkontrolujte logy
tail -f ~/Burza/trading_bot.log

# Test konfigurace
cd ~/Burza
source venv/bin/activate
python3 test_bot.py

# Zkontrolujte .env soubor
cat ~/Burza/.env
```

### API klíče nefungují
```bash
# Ověřte na burzách:
# 1. Klíče jsou aktivní
# 2. Spot trading je povoleno
# 3. IP whitelist (pokud používáte)

# Test připojení:
cd ~/Burza
source venv/bin/activate
python3 -c "from exchanges import BinanceExchange; print('OK')"
```

### Nedostatek paměti
```bash
# Zkontrolujte paměť
free -h

# Vytvořte swap pokud je potřeba:
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Rate limiting od burz
```bash
# Zvyšte CHECK_INTERVAL v .env
nano ~/Burza/.env
# Změňte: CHECK_INTERVAL=5  (místo 2)

# Restart bota
```

---

## 📈 OPTIMALIZACE VÝKONU

### Pro maximální výnos:
1. **Použijte VPS blízko burz** (např. Londýn, Frankfurt)
2. **Rychlé připojení** - ping < 50ms k burzám
3. **Agresivní režim** - `.env.aggressive`
4. **Monitorujte a adjustujte** CHECK_INTERVAL podle rate limitů

### Doporučené VPS providery:
- **DigitalOcean** - $6/měsíc, Frankfurt datacenter
- **Vultr** - $6/měsíc, Amsterdam datacenter  
- **Hetzner** - €4/měsíc, Německo (NEJRYCHLEJŠÍ do EU burz)
- **AWS Lightsail** - $5/měsíc, různé regiony

### Minimální VPS požadavky:
- **RAM**: 1GB (2GB doporučeno)
- **CPU**: 1 vCPU
- **Storage**: 10GB
- **Bandwidth**: Neomezený
- **OS**: Ubuntu 20.04 nebo 22.04

---

## 🔄 AUTOMATICKÉ SKRIPTY

### Auto-restart při pádu
```bash
# Již zahrnuto v systemd servisu
# Pokud používáte screen, vytvořte watch script:

cat > ~/Burza/watch.sh <<'EOF'
#!/bin/bash
while true; do
    if ! screen -list | grep -q "burza-bot"; then
        echo "Bot crashed, restarting..."
        cd ~/Burza
        screen -dmS burza-bot bash -c "source venv/bin/activate && python3 bot.py --live"
    fi
    sleep 60
done
EOF

chmod +x ~/Burza/watch.sh

# Spustit watch script
screen -dmS burza-watch ~/Burza/watch.sh
```

### Daily profit report
```bash
# Přidejte do crontab
crontab -e

# Denní report v 23:00:
0 23 * * * cd ~/Burza && grep "$(date +'%Y-%m-%d')" trading_bot.log | grep "profit" | mail -s "Daily Trading Report" your@email.com
```

---

## ✅ CHECKLIST PRO DEPLOYMENT

- [ ] VPS připraven (Ubuntu 20.04/22.04)
- [ ] SSH přístup funguje
- [ ] API klíče z Binance/Kraken
- [ ] API klíče mají spot trading oprávnění
- [ ] Deployment script stažen a spuštěn
- [ ] Testy prošly (`test_bot.py`)
- [ ] Dry-run test proběhl (15+ minut)
- [ ] .env soubor zkontrolován
- [ ] Bot spuštěn (screen nebo systemd)
- [ ] Logy se sledují
- [ ] Firewall nastaven
- [ ] Backup .env vytvořen

---

## 🎯 RYCHLÝ START (TL;DR)

```bash
# Na VPS:
ssh your-user@your-vps-ip

# Spusť deployment:
git clone https://github.com/tvojehnizdo/Burza.git
cd Burza
./deploy_vps.sh

# Zadej API klíče když se script zeptá
# Vyber "aggressive" režim
# Vyber způsob spuštění

# Sleduj logy:
tail -f trading_bot.log

# HOTOVO! Bot běží a vydělává.
```

---

## 💬 PODPORA

Problémy nebo otázky:
- Zkontrolujte `trading_bot.log`
- Přečtěte si MAX_PROFIT_GUIDE.md
- GitHub Issues: https://github.com/tvojehnizdo/Burza/issues

**Happy Trading! 🚀💰**

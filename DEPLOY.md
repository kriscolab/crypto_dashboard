## STEP 1 — Create GitHub repo

Go to https://github.com/new
  Name:     crypto-dashboard
  Private:  YES (contains your trading system)
  README:   NO (we have our own)
  Click "Create repository"


## STEP 2 — Push from your local machine (where you downloaded the zip)

# Unzip the downloaded file
unzip crypto_dashboard_backend.zip -d crypto-dashboard
cd crypto-dashboard

# Init git and push
git init
git add .
git commit -m "feat: session 1 - backend, DB schema, ingestion, regime detection, API"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/crypto-dashboard.git
git push -u origin main


## STEP 3 — On your VPS: clone and deploy

ssh user@YOUR_VPS_IP

# Install Docker (one time)
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/crypto-dashboard/main/scripts/vps_setup.sh | bash

# Clone repo
git clone https://github.com/YOUR_USERNAME/crypto-dashboard.git /opt/crypto-dashboard
cd /opt/crypto-dashboard

# Configure environment
cp .env.example .env
nano .env
# Fill in:
#   DB_PASSWORD=strong_password_here
#   BINANCE_API_KEY=  (read-only key, market data only)
#   COINGLASS_API_KEY=
#   COINGECKO_API_KEY=  (optional, helps with rate limits)
#   CRYPTOQUANT_API_KEY=  (optional, needed for on-chain flows)
#   SECRET_KEY=random_string_here

# Start everything
docker compose up -d

# Watch the seed run (takes 5-10 min first boot)
docker compose logs -f backend

# Verify API is live
curl http://localhost:8000/health


## STEP 4 — Future updates (local → VPS workflow)

# On local machine, after making changes:
git add .
git commit -m "your message"
git push

# On VPS, pull and restart:
cd /opt/crypto-dashboard
git pull
docker compose restart backend


## STEP 5 — Useful VPS commands

# Check all containers
docker compose ps

# DB shell
docker exec -it crypto_tsdb psql -U crypto -d crypto_dashboard

# Check how many candles seeded
docker exec -it crypto_tsdb psql -U crypto -d crypto_dashboard \
  -c "SELECT symbol, timeframe, COUNT(*), MIN(time), MAX(time) FROM ohlcv GROUP BY symbol, timeframe ORDER BY symbol, timeframe;"

# Check latest funding rates
docker exec -it crypto_tsdb psql -U crypto -d crypto_dashboard \
  -c "SELECT symbol, funding_rate_pct, time FROM funding_rates ORDER BY time DESC LIMIT 5;"

# Restart just backend (after code changes pulled from git)
docker compose restart backend

# View backend logs (live)
docker compose logs -f backend --tail=100

# Stop everything
docker compose down

# Nuclear reset (WARNING: deletes all data)
docker compose down -v

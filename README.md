# Crypto Trading Dashboard

Multi-layer confluence trading system for BTC/ETH futures. Personal trade journal + real-time signal dashboard + edge discovery analytics.

## Stack
- **Backend:** Python 3.11 + FastAPI + APScheduler
- **DB:** TimescaleDB (PostgreSQL + time-series extension)
- **Data:** Binance Futures WebSocket + CoinGlass + Deribit + CoinGecko + CryptoQuant
- **Infra:** Docker Compose on VPS

## Quick Start (VPS)

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/crypto-dashboard.git
cd crypto-dashboard

# 2. Install Docker (first time)
bash scripts/vps_setup.sh

# 3. Configure
cp .env.example .env
nano .env   # add DB password + API keys

# 4. Start
docker compose up -d

# 5. Watch seed + startup
docker compose logs -f backend
```

API docs at `http://YOUR_VPS_IP:8000/docs`

## See HANDOFF.md for full architecture and context.

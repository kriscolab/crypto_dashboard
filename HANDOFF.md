# CRYPTO DASHBOARD — HANDOFF DIGEST
## Session 1 Complete | Backend + DB + Ingestion Layer

---

## PROJECT OVERVIEW

**What this is:** A personal multi-layer confluence trading dashboard + journal for BTC/ETH futures. Mid-frequency focus (1H/4H/1D). Cloud-hosted on VPS, Python backend, TimescaleDB for time-series, React frontend (next session).

**Philosophy:** 7-layer weighted confluence scoring system. Higher score = larger position size. Trade journal captures every layer at entry so analytics can prove which layers actually generate edge over time.

---

## ARCHITECTURE

```
VPS (Docker Compose)
├── timescaledb:latest-pg15   → time-series DB (OHLCV, funding, OI, etc.)
├── redis:7-alpine             → caching layer (future use)
└── backend (FastAPI)
    ├── WebSocket → Binance Futures (live OHLCV, funding, liquidations)
    ├── APScheduler → polls OI/30s, CoinGlass/5m, CoinGecko/15m, Deribit/15m, F&G/1h, CryptoQuant/30m
    └── REST API → serves dashboard + trade journal
```

---

## FILE STRUCTURE

```
crypto-dashboard/
├── .env.example                   ← copy to .env, fill secrets
├── .gitignore
├── docker-compose.yml             ← full stack orchestration
├── scripts/
│   ├── init.sql                   ← TimescaleDB schema (runs on first boot)
│   └── vps_setup.sh               ← Ubuntu Docker install + firewall + systemd
└── backend/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py                    ← FastAPI app, lifespan, scheduler, router mounts
    ├── config/
    │   └── settings.py            ← all config via env vars, Pydantic Settings
    ├── db/
    │   └── connection.py          ← async SQLAlchemy engine, get_db dependency
    ├── ingestion/
    │   ├── binance.py             ← WebSocket streams + historical REST seed
    │   └── external.py            ← CoinGlass, CoinGecko, Deribit, F&G, CryptoQuant
    ├── regime/
    │   └── detector.py            ← ATR/ADX regime classifier, stored per bar
    └── api/
        ├── market.py              ← /api/market/* endpoints
        ├── trades.py              ← /api/trades/* CRUD + scoring
        └── analytics.py          ← /api/analytics/* edge discovery queries
```

---

## DATABASE SCHEMA (TimescaleDB)

### Hypertables (time-series, partitioned by time)
| Table | Key columns | Chunk interval |
|---|---|---|
| `ohlcv` | time, symbol, timeframe, OHLCV | 7 days |
| `funding_rates` | time, symbol, rate, mark_price | 30 days |
| `open_interest` | time, symbol, source, oi_usd | 30 days |
| `liquidations` | time, symbol, side, price, qty | 7 days |
| `options_skew` | time, underlying, expiry, RR, IV | 30 days |
| `onchain_flows` | time, asset, source, netflow | 30 days |
| `market_sentiment` | time, F&G, BTC dom, ETH/BTC | 90 days |
| `regime_snapshots` | time, symbol, tf, regime, ADX, ATR% | 30 days |

### Standard tables (trade journal)
| Table | Purpose |
|---|---|
| `trades` | Core journal — entry/exit, PnL, R-multiple, context snapshot |
| `trade_confluence` | Layer-by-layer breakdown per trade (FK → trades) |
| `daily_stats` | Nightly rollup for quick stats |

### Key SQL function
```sql
SELECT compute_confluence_score(
    structure, order_flow, funding, options, onchain, macro, liquidation
);
-- Returns weighted score 0-10
```

---

## CONFLUENCE SCORING SYSTEM

| Layer | Weight | Max contribution | Signal type |
|---|---|---|---|
| Structure (TPO/VP) | 2.0 | 2.0 | Trade location |
| Order Flow (delta) | 2.0 | 2.0 | Conviction |
| Funding Rate | 1.5 | 1.5 | Crowd positioning |
| Options Skew | 1.5 | 1.5 | Smart money |
| On-Chain Flows | 1.0 | 1.0 | Macro context |
| Macro/Session | 1.0 | 1.0 | Veto filter |
| Liquidation Map | 1.0 | 1.0 | Tactical entry |
| **Total** | | **10.0** | |

**Sizing thresholds:**
- Score ≥ 7.0 → Full size
- Score 5.0–6.9 → Half size
- Score < 5.0 → No trade

---

## API ENDPOINTS

### Market Data
```
GET /api/market/overview          → all layers, both instruments, one call
GET /api/market/ohlcv/{symbol}    → OHLCV for charting
GET /api/market/funding/{symbol}  → funding rate history
GET /api/market/sentiment         → F&G, BTC dom, stablecoin dom
GET /api/market/regime/{symbol}   → current regime + 30d history
GET /api/market/liquidations/{symbol} → recent liquidation events
```

### Trade Journal
```
POST  /api/trades/                → log new trade + confluence
PATCH /api/trades/{id}/close      → close trade, auto-compute PnL + R
GET   /api/trades/open            → open positions
GET   /api/trades/history         → closed trade history
GET   /api/trades/score-preview   → preview score (UI helper)
```

### Analytics (Edge Discovery)
```
GET /api/analytics/performance/summary          → overall stats + expectancy
GET /api/analytics/performance/by-regime        → win rate + R by regime
GET /api/analytics/performance/by-session       → performance by Asia/London/NY
GET /api/analytics/performance/by-confluence-score → proves score → edge
GET /api/analytics/performance/by-setup         → which setups work
GET /api/analytics/performance/drawdown         → equity curve + drawdown
GET /api/analytics/performance/funding-correlation → extreme funding → better R?
GET /api/analytics/layer-analysis               → which layers contribute most
```

---

## DATA SOURCES

| Layer | Source | Auth | Cost | Interval |
|---|---|---|---|---|
| OHLCV (live) | Binance Futures WS | None | Free | Real-time |
| Funding rate (live) | Binance Futures WS | None | Free | Every 3s |
| Liquidations | Binance Futures WS | None | Free | Real-time |
| Open Interest | Binance REST | None | Free | Poll 30s |
| OI (aggregated) | CoinGlass API | API key | Free tier | Poll 5m |
| On-chain flows | CryptoQuant | API key | Free tier | Poll 30m |
| Options skew | Deribit REST | None | Free | Poll 15m |
| Fear & Greed | Alternative.me | None | Free | Poll 1h |
| Market overview | CoinGecko | Optional | Free tier | Poll 15m |

**Key insight on rate limits:** Binance WebSocket has no rate limit concerns — up to 1024 streams per connection. REST is only used for OI polling (500 req/5min limit, we use ~10/5min) and historical seed (1500 candles/call, 0.1s sleep between calls).

---

## REGIME DETECTION

**Algorithm:** ATR (14) + ADX (14) + EMA(21)/EMA(50)

**Labels:**
- `trending_bull` — ADX > 25, price above both EMAs
- `trending_bear` — ADX > 25, price below both EMAs
- `ranging` — ADX < 20, ATR% < 1.2%
- `volatile` — ATR% > 3.5% (overrides trend/range)

**Computed:** Every 15 minutes on closed bars, stored in `regime_snapshots`. Snapshot at entry captured in `trades.entry_regime` for analytics.

---

## HISTORICAL SEED (first boot)

On first `docker compose up`:
1. Seeds 365 days OHLCV (1H, 4H, 1D) for BTCUSDT + ETHUSDT
2. Seeds 365 days funding rate history
3. Backfills regime snapshots for all seeded bars
4. WebSocket takes over for live updates

Idempotent — safe to restart, uses `ON CONFLICT DO NOTHING/UPDATE`.

---

## WHAT'S NOT DONE YET (next sessions)

### Session 2 — React Frontend Dashboard
- Real-time dashboard consuming `/api/market/overview`
- TradingView Lightweight Charts for OHLCV
- Signal layer cards (funding, regime, options skew, F&G)
- Live liquidation ticker
- Regime badge with confidence

### Session 3 — Trade Journal UI
- Trade entry form with confluence layer checkboxes
- Score preview widget (updates as user fills layers)
- Open positions table
- Closed trades history table

### Session 4 — Analytics Dashboard
- Equity curve chart
- Performance tables (by regime, session, score bucket)
- Layer contribution heatmap
- Running win rate / expectancy over time

### Session 5 — Auto-import + System Trades
- Binance trade history auto-import (REST)
- Webhook receiver for when system eventually takes trades automatically
- Position sizing calculator based on ATR + score

---

## DEPLOY COMMANDS (summary)

```bash
# VPS first-time setup
bash scripts/vps_setup.sh

# Config
cp .env.example .env && nano .env

# Start everything
docker compose up -d

# Monitor
docker compose logs -f backend
docker compose logs -f timescaledb

# DB access
docker exec -it crypto_tsdb psql -U crypto -d crypto_dashboard

# Restart backend only
docker compose restart backend

# Stop all
docker compose down
```

---

## TECH STACK

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.11 | Quant ecosystem, pandas/numpy |
| Web framework | FastAPI | Async, fast, auto-docs |
| DB | TimescaleDB (PG15) | Time-series optimized, SQL familiar |
| ORM | SQLAlchemy async | Type-safe, async support |
| Scheduler | APScheduler | Simple, battle-tested |
| WebSocket | websockets lib | Direct, no abstraction overhead |
| HTTP client | httpx (async) | Async-native |
| Indicators | pandas-ta | ATR, ADX, EMA without TA-Lib pain |
| Container | Docker Compose | Single VPS, simple ops |

---

*Handoff digest generated after Session 1. Upload this file to the repo as `HANDOFF.md` so Hermes has full context on first load.*

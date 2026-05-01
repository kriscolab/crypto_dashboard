"""
FastAPI main application.
Starts WebSocket ingestion + polling scheduler on startup.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db.connection import check_db_connection
from ingestion.binance import run_binance_websocket, run_historical_seed, poll_open_interest
from ingestion.external import (
    poll_coinglass_oi,
    poll_coinglass_funding,
    poll_coingecko_overview,
    poll_deribit_skew,
    poll_fear_greed,
    poll_cryptoquant_flows,
)
from regime.detector import backfill_regimes, compute_and_store_regime
from api import market, trades, analytics
from config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    # ── Startup ───────────────────────────────────────
    logger.info("Starting Crypto Dashboard backend...")

    # Verify DB
    if not await check_db_connection():
        logger.error("Cannot connect to TimescaleDB — aborting")
        raise RuntimeError("Database not ready")

    # Seed historical data (idempotent)
    logger.info("Running historical seed (skips existing data)...")
    await run_historical_seed()
    await backfill_regimes()

    # ── Scheduler — polling jobs ───────────────────────
    # OI from Binance (every 30s per instrument)
    for sym in settings.instruments:
        scheduler.add_job(
            poll_open_interest, "interval", seconds=30,
            args=[sym], id=f"oi_{sym}", replace_existing=True
        )

    # Regime recompute (every 15min, runs after enough new bars close)
    for sym in settings.instruments:
        for tf in settings.timeframes:
            scheduler.add_job(
                compute_and_store_regime, "interval", minutes=15,
                args=[sym, tf], id=f"regime_{sym}_{tf}", replace_existing=True
            )

    # CoinGlass (every 5min)
    scheduler.add_job(poll_coinglass_oi, "interval", minutes=5, id="cg_oi")
    scheduler.add_job(poll_coinglass_funding, "interval", minutes=5, id="cg_funding")

    # CoinGecko overview (every 15min)
    scheduler.add_job(poll_coingecko_overview, "interval", minutes=15, id="coingecko")

    # Deribit skew (every 15min)
    scheduler.add_job(poll_deribit_skew, "interval", minutes=15, args=["BTC"], id="deribit_btc")
    scheduler.add_job(poll_deribit_skew, "interval", minutes=15, args=["ETH"], id="deribit_eth")

    # Fear & Greed (hourly)
    scheduler.add_job(poll_fear_greed, "interval", hours=1, id="fear_greed")

    # CryptoQuant flows (every 30min)
    scheduler.add_job(poll_cryptoquant_flows, "interval", minutes=30, args=["btc"], id="cq_btc")
    scheduler.add_job(poll_cryptoquant_flows, "interval", minutes=30, args=["eth"], id="cq_eth")

    scheduler.start()
    logger.info(f"Scheduler started with {len(scheduler.get_jobs())} jobs")

    # ── WebSocket (runs forever in background) ─────────
    ws_task = asyncio.create_task(run_binance_websocket())
    logger.info("Binance WebSocket task started")

    yield  # app runs here

    # ── Shutdown ──────────────────────────────────────
    scheduler.shutdown(wait=False)
    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutdown complete")


app = FastAPI(
    title="Crypto Trading Dashboard",
    description="Multi-layer confluence trading system — BTC/ETH",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Lock down to your VPS IP in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────
app.include_router(market.router, prefix="/api/market", tags=["Market Data"])
app.include_router(trades.router, prefix="/api/trades", tags=["Trade Journal"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["Analytics"])


@app.get("/health")
async def health():
    db_ok = await check_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "error",
        "scheduler_jobs": len(scheduler.get_jobs()),
        "instruments": settings.instruments,
    }

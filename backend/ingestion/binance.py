"""
Binance Futures data ingestion.

WebSocket streams (real-time, no rate limit concerns):
  - Kline/OHLCV per symbol per timeframe
  - Mark price + funding rate (every 3s)
  - Liquidation orders (real-time)
  - Aggregate trades (for order flow delta)

REST (historical seed + periodic OI poll):
  - Historical klines (1 year seed)
  - Historical funding rates
  - Open interest (poll every 30s - well within limits)
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import websockets
import httpx
from decimal import Decimal

from db.connection import AsyncSessionLocal
from sqlalchemy import text
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Binance Futures base URLs ──────────────────────────────────
WS_BASE = "wss://fstream.binance.com/stream"
REST_BASE = "https://fapi.binance.com"


# ══════════════════════════════════════════════════════════════
# REST — Historical Seeding
# ══════════════════════════════════════════════════════════════

async def fetch_historical_klines(
    symbol: str,
    interval: str,
    days_back: int,
    client: httpx.AsyncClient
) -> List[dict]:
    """
    Fetch historical OHLCV from Binance Futures REST.
    Handles pagination automatically (Binance returns max 1500 candles per call).
    """
    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000)

    all_klines = []
    current_start = start_ts

    while current_start < end_ts:
        try:
            resp = await client.get(
                f"{REST_BASE}/fapi/v1/klines",
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": current_start,
                    "endTime": end_ts,
                    "limit": 1500,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data:
                break

            all_klines.extend(data)

            # Advance start to last candle open time + 1ms
            current_start = data[-1][0] + 1

            # Binance REST: 2400 weight/min limit, klines = 2 weight
            # 1500 candles per call, safe to call without delay for seeding
            await asyncio.sleep(0.1)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("Rate limited, backing off 60s")
                await asyncio.sleep(60)
            else:
                logger.error(f"HTTP error fetching klines {symbol}/{interval}: {e}")
                break

    logger.info(f"Fetched {len(all_klines)} klines for {symbol}/{interval}")
    return all_klines


async def seed_ohlcv(symbol: str, days_back: int = 365):
    """Seed historical OHLCV for all configured timeframes."""
    async with httpx.AsyncClient() as client:
        for tf in settings.timeframes:
            logger.info(f"Seeding {symbol} {tf} ({days_back} days)...")
            klines = await fetch_historical_klines(symbol, tf, days_back, client)

            rows = [
                {
                    "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                    "symbol": symbol,
                    "timeframe": tf,
                    "open": Decimal(str(k[1])),
                    "high": Decimal(str(k[2])),
                    "low": Decimal(str(k[3])),
                    "close": Decimal(str(k[4])),
                    "volume": Decimal(str(k[5])),
                    "quote_vol": Decimal(str(k[7])),
                    "trades": int(k[8]),
                }
                for k in klines
            ]

            if not rows:
                continue

            async with AsyncSessionLocal() as session:
                # Bulk upsert using raw SQL for performance
                await session.execute(
                    text("""
                        INSERT INTO ohlcv
                            (time, symbol, timeframe, open, high, low, close, volume, quote_vol, trades)
                        VALUES
                            (:time, :symbol, :timeframe, :open, :high, :low, :close, :volume, :quote_vol, :trades)
                        ON CONFLICT (time, symbol, timeframe) DO UPDATE SET
                            open      = EXCLUDED.open,
                            high      = EXCLUDED.high,
                            low       = EXCLUDED.low,
                            close     = EXCLUDED.close,
                            volume    = EXCLUDED.volume,
                            quote_vol = EXCLUDED.quote_vol,
                            trades    = EXCLUDED.trades
                    """),
                    rows,
                )
                await session.commit()

            logger.info(f"Seeded {len(rows)} candles for {symbol}/{tf}")


async def seed_funding_rates(symbol: str, days_back: int = 365):
    """Seed historical funding rates. Binance stores every 8h settlement."""
    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000)

    async with httpx.AsyncClient() as client:
        all_rates = []
        current_start = start_ts

        while current_start < end_ts:
            try:
                resp = await client.get(
                    f"{REST_BASE}/fapi/v1/fundingRate",
                    params={
                        "symbol": symbol,
                        "startTime": current_start,
                        "endTime": end_ts,
                        "limit": 1000,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                if not data:
                    break

                all_rates.extend(data)
                current_start = data[-1]["fundingTime"] + 1
                await asyncio.sleep(0.2)

            except Exception as e:
                logger.error(f"Error fetching funding rates: {e}")
                break

        rows = [
            {
                "time": datetime.fromtimestamp(r["fundingTime"] / 1000, tz=timezone.utc),
                "symbol": r["symbol"],
                "funding_rate": Decimal(str(r["fundingRate"])),
                "funding_rate_pct": Decimal(str(r["fundingRate"])) * 100,
                "mark_price": Decimal(str(r.get("markPrice", 0))) if r.get("markPrice") else None,
            }
            for r in all_rates
        ]

        if rows:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO funding_rates
                            (time, symbol, funding_rate, funding_rate_pct, mark_price)
                        VALUES
                            (:time, :symbol, :funding_rate, :funding_rate_pct, :mark_price)
                        ON CONFLICT (time, symbol) DO NOTHING
                    """),
                    rows,
                )
                await session.commit()

        logger.info(f"Seeded {len(rows)} funding rate records for {symbol}")


async def poll_open_interest(symbol: str):
    """
    Poll current OI from Binance REST. Called every 30s via scheduler.
    Binance doesn't stream OI via WebSocket so REST poll is the way.
    Rate limit: 500/5min/IP shared — 30s interval is very safe.
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{REST_BASE}/fapi/v1/openInterest",
                params={"symbol": symbol},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO open_interest
                            (time, symbol, source, oi_contracts, oi_usd)
                        VALUES
                            (:time, :symbol, :source, :oi_contracts, :oi_usd)
                        ON CONFLICT (time, symbol, source) DO NOTHING
                    """),
                    {
                        "time": datetime.now(timezone.utc),
                        "symbol": symbol,
                        "source": "binance",
                        "oi_contracts": Decimal(str(data["openInterest"])),
                        "oi_usd": None,  # Binance returns contracts, not USD
                    },
                )
                await session.commit()

        except Exception as e:
            logger.error(f"OI poll error for {symbol}: {e}")


# ══════════════════════════════════════════════════════════════
# WebSocket Streams — Real-time
# ══════════════════════════════════════════════════════════════

def build_stream_url(streams: List[str]) -> str:
    """Build combined stream URL (up to 1024 streams per connection)."""
    combined = "/".join(streams)
    return f"{WS_BASE}?streams={combined}"


async def handle_kline(data: dict):
    """Process incoming kline/candlestick WebSocket message."""
    k = data["k"]
    if not k["x"]:  # x = is this candle closed?
        return      # Skip unclosed candles for DB writes

    symbol = k["s"]
    tf_map = {"1m": "1m", "1h": "1h", "4h": "4h", "1d": "1d"}
    tf = tf_map.get(k["i"])
    if not tf or tf not in settings.timeframes:
        return

    row = {
        "time": datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc),
        "symbol": symbol,
        "timeframe": tf,
        "open": Decimal(k["o"]),
        "high": Decimal(k["h"]),
        "low": Decimal(k["l"]),
        "close": Decimal(k["c"]),
        "volume": Decimal(k["v"]),
        "quote_vol": Decimal(k["q"]),
        "trades": int(k["n"]),
    }

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO ohlcv
                    (time, symbol, timeframe, open, high, low, close, volume, quote_vol, trades)
                VALUES
                    (:time, :symbol, :timeframe, :open, :high, :low, :close, :volume, :quote_vol, :trades)
                ON CONFLICT (time, symbol, timeframe) DO UPDATE SET
                    open      = EXCLUDED.open,
                    high      = EXCLUDED.high,
                    low       = EXCLUDED.low,
                    close     = EXCLUDED.close,
                    volume    = EXCLUDED.volume,
                    quote_vol = EXCLUDED.quote_vol
            """),
            row,
        )
        await session.commit()


async def handle_mark_price(data: dict):
    """Process mark price + funding rate stream (updates every 3s)."""
    # Only write on funding rate settlement intervals
    next_time = data.get("T")
    if not next_time:
        return

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO funding_rates
                    (time, symbol, funding_rate, funding_rate_pct, mark_price, next_funding_time)
                VALUES
                    (:time, :symbol, :funding_rate, :funding_rate_pct, :mark_price, :next_funding_time)
                ON CONFLICT (time, symbol) DO UPDATE SET
                    funding_rate     = EXCLUDED.funding_rate,
                    funding_rate_pct = EXCLUDED.funding_rate_pct,
                    mark_price       = EXCLUDED.mark_price
            """),
            {
                "time": datetime.now(timezone.utc),
                "symbol": data["s"],
                "funding_rate": Decimal(str(data["r"])),
                "funding_rate_pct": Decimal(str(data["r"])) * 100,
                "mark_price": Decimal(str(data["p"])),
                "next_funding_time": datetime.fromtimestamp(
                    next_time / 1000, tz=timezone.utc
                ),
            },
        )
        await session.commit()


async def handle_liquidation(data: dict):
    """Process liquidation order stream."""
    o = data["o"]
    usd_val = Decimal(str(o["p"])) * Decimal(str(o["q"]))

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO liquidations
                    (time, symbol, side, price, qty, usd_value)
                VALUES
                    (:time, :symbol, :side, :price, :qty, :usd_value)
                ON CONFLICT DO NOTHING
            """),
            {
                "time": datetime.fromtimestamp(o["T"] / 1000, tz=timezone.utc),
                "symbol": o["s"],
                "side": "LONG" if o["S"] == "SELL" else "SHORT",  # liquidation side
                "price": Decimal(str(o["p"])),
                "qty": Decimal(str(o["q"])),
                "usd_value": usd_val,
            },
        )
        await session.commit()


async def run_binance_websocket():
    """
    Main WebSocket loop. Subscribes to all streams for configured instruments.
    Auto-reconnects on disconnect with exponential backoff.
    """
    # Build stream list
    streams = []
    for symbol in settings.instruments:
        sym = symbol.lower()
        for tf in settings.timeframes:
            streams.append(f"{sym}@kline_{tf}")
        streams.append(f"{sym}@markPrice@1s")
        streams.append(f"{sym}@forceOrder")   # liquidations

    url = build_stream_url(streams)
    logger.info(f"Connecting to Binance WebSocket ({len(streams)} streams)")

    backoff = 1
    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=180,    # match Binance 3min ping
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                logger.info("Binance WebSocket connected")
                backoff = 1  # reset on successful connect

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                        stream = msg.get("stream", "")
                        data = msg.get("data", msg)

                        if "@kline_" in stream:
                            await handle_kline(data)
                        elif "@markPrice" in stream:
                            await handle_mark_price(data)
                        elif "@forceOrder" in stream:
                            await handle_liquidation(data)

                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        continue

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}. Reconnecting in {backoff}s...")
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in {backoff}s...")

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)   # cap backoff at 60s


# ══════════════════════════════════════════════════════════════
# Seed runner - called once on first startup
# ══════════════════════════════════════════════════════════════

async def run_historical_seed():
    """Seed all historical data. Idempotent - safe to run multiple times."""
    logger.info("Starting historical data seed...")

    for symbol in settings.instruments:
        logger.info(f"Seeding {symbol}...")
        await seed_ohlcv(symbol, days_back=settings.seed_days_ohlcv)
        await seed_funding_rates(symbol, days_back=settings.seed_days_funding)

    logger.info("Historical seed complete.")

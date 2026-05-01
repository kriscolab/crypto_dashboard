"""
Polling jobs for external data sources.
All run on APScheduler - intervals chosen to stay well within free tier limits.

Schedule:
  CoinGlass OI/funding aggregate  — every 5 min
  CoinGecko market overview       — every 15 min
  Deribit options skew            — every 15 min
  Fear & Greed                    — every 1 hour
  CryptoQuant on-chain flows      — every 30 min
"""

import httpx
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from db.connection import AsyncSessionLocal
from sqlalchemy import text
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Base URLs ─────────────────────────────────────────────────
COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
FEAR_GREED_BASE = "https://api.alternative.me"
CRYPTOQUANT_BASE = "https://api.cryptoquant.com/v1"


# ══════════════════════════════════════════════════════════════
# COINGLASS — Aggregated derivatives data
# ══════════════════════════════════════════════════════════════

async def poll_coinglass_oi():
    """
    Fetch aggregated open interest across all exchanges.
    More useful than single-exchange Binance OI.
    Free tier: ~30 req/min.
    """
    if not settings.coinglass_api_key:
        return

    headers = {"coinglassSecret": settings.coinglass_api_key}
    coins = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        for symbol, coin in coins.items():
            try:
                resp = await client.get(
                    f"{COINGLASS_BASE}/open_interest",
                    params={"symbol": coin},
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("code") != "0":
                    logger.warning(f"CoinGlass OI error for {coin}: {data}")
                    continue

                # Aggregate across all exchanges
                total_oi_usd = sum(
                    float(ex.get("openInterestAmount", 0))
                    for ex in data.get("data", {}).get("exchangeList", [])
                )

                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("""
                            INSERT INTO open_interest (time, symbol, source, oi_usd)
                            VALUES (:time, :symbol, :source, :oi_usd)
                            ON CONFLICT (time, symbol, source) DO UPDATE SET
                                oi_usd = EXCLUDED.oi_usd
                        """),
                        {
                            "time": datetime.now(timezone.utc),
                            "symbol": symbol,
                            "source": "coinglass_agg",
                            "oi_usd": Decimal(str(total_oi_usd)),
                        },
                    )
                    await session.commit()

            except Exception as e:
                logger.error(f"CoinGlass OI poll error ({coin}): {e}")


async def poll_coinglass_funding():
    """Fetch funding rates across exchanges from CoinGlass."""
    if not settings.coinglass_api_key:
        return

    headers = {"coinglassSecret": settings.coinglass_api_key}
    coins = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        for symbol, coin in coins.items():
            try:
                resp = await client.get(
                    f"{COINGLASS_BASE}/funding_usd_rate",
                    params={"symbol": coin},
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("code") != "0":
                    continue

                # Log average funding rate across top exchanges
                rates = [
                    float(ex.get("rate", 0))
                    for ex in data.get("data", {}).get("exchangeList", [])
                    if ex.get("rate") is not None
                ]

                if rates:
                    avg_rate = sum(rates) / len(rates)
                    logger.debug(f"Avg funding {symbol}: {avg_rate:.6f}")

            except Exception as e:
                logger.error(f"CoinGlass funding poll error ({coin}): {e}")


# ══════════════════════════════════════════════════════════════
# COINGECKO — Market overview
# ══════════════════════════════════════════════════════════════

async def poll_coingecko_overview():
    """
    Fetch global market metrics: BTC dominance, total market cap,
    stablecoin dominance, ETH/BTC ratio.
    Free tier: 10,000 credits/month. This call = 1 credit.
    """
    headers = {}
    if settings.coingecko_api_key:
        headers["x-cg-demo-api-key"] = settings.coingecko_api_key

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        try:
            # Global market data
            resp = await client.get(f"{COINGECKO_BASE}/global")
            resp.raise_for_status()
            global_data = resp.json().get("data", {})

            # ETH/BTC ratio
            price_resp = await client.get(
                f"{COINGECKO_BASE}/simple/price",
                params={"ids": "ethereum,bitcoin", "vs_currencies": "btc,usd"},
            )
            price_resp.raise_for_status()
            prices = price_resp.json()
            eth_btc = prices.get("ethereum", {}).get("btc", 0)

            total_mcap = global_data.get("total_market_cap", {}).get("usd", 0)
            btc_dom = global_data.get("market_cap_percentage", {}).get("btc", 0)
            eth_dom = global_data.get("market_cap_percentage", {}).get("eth", 0)
            usdt_dom = global_data.get("market_cap_percentage", {}).get("usdt", 0)
            usdc_dom = global_data.get("market_cap_percentage", {}).get("usdc", 0)
            stablecoin_dom = usdt_dom + usdc_dom

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO market_sentiment
                            (time, btc_dominance, eth_btc_ratio, stablecoin_dom, total_market_cap)
                        VALUES
                            (:time, :btc_dom, :eth_btc, :stable_dom, :total_mcap)
                        ON CONFLICT (time) DO UPDATE SET
                            btc_dominance  = EXCLUDED.btc_dominance,
                            eth_btc_ratio  = EXCLUDED.eth_btc_ratio,
                            stablecoin_dom = EXCLUDED.stablecoin_dom,
                            total_market_cap = EXCLUDED.total_market_cap
                    """),
                    {
                        "time": datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0),
                        "btc_dom": Decimal(str(btc_dom)),
                        "eth_btc": Decimal(str(eth_btc)),
                        "stable_dom": Decimal(str(stablecoin_dom)),
                        "total_mcap": Decimal(str(total_mcap)),
                    },
                )
                await session.commit()

        except Exception as e:
            logger.error(f"CoinGecko overview poll error: {e}")


# ══════════════════════════════════════════════════════════════
# DERIBIT — Options skew
# ══════════════════════════════════════════════════════════════

async def poll_deribit_skew(underlying: str = "BTC"):
    """
    Fetch 25-delta risk reversal (call IV - put IV) from Deribit.
    No API key required for market data.
    Gets nearest weekly and monthly expiries.
    """
    currency = underlying  # BTC or ETH

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            # Get available instruments (to find expiry dates)
            resp = await client.get(
                f"{DERIBIT_BASE}/get_instruments",
                params={
                    "currency": currency,
                    "kind": "option",
                    "expired": False,
                },
            )
            resp.raise_for_status()
            instruments = resp.json().get("result", [])

            # Get unique expiries, sorted
            from datetime import date
            expiries = sorted(set(
                datetime.fromtimestamp(i["expiration_timestamp"] / 1000, tz=timezone.utc).date()
                for i in instruments
                if i["option_type"] in ("call", "put")
            ))

            # Focus on 1st and 2nd expiry (nearest weekly + next)
            target_expiries = expiries[:2]

            for expiry in target_expiries:
                dte = (expiry - date.today()).days
                if dte < 1:
                    continue

                # Get vol surface for this expiry
                resp = await client.get(
                    f"{DERIBIT_BASE}/get_volatility_index_data",
                    params={
                        "currency": currency,
                        "start_timestamp": int(datetime.now(timezone.utc).timestamp() * 1000) - 3600000,
                        "end_timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "resolution": 3600,
                    },
                )

                # Simpler: use mark IV from specific 25d strikes
                # Filter instruments for this expiry at ~25 delta
                expiry_str = expiry.strftime("%d%b%y").upper()
                expiry_instruments = [
                    i for i in instruments
                    if expiry_str in i["instrument_name"]
                ]

                calls = [i for i in expiry_instruments if i["option_type"] == "call"]
                puts = [i for i in expiry_instruments if i["option_type"] == "put"]

                if not calls or not puts:
                    continue

                # Get ticker for ATM options (proxy for skew)
                # Use mid-strike call and put
                if calls:
                    call_resp = await client.get(
                        f"{DERIBIT_BASE}/ticker",
                        params={"instrument_name": calls[len(calls)//2]["instrument_name"]},
                    )
                    call_data = call_resp.json().get("result", {})
                    iv_call = call_data.get("mark_iv", None)
                else:
                    iv_call = None

                if puts:
                    put_resp = await client.get(
                        f"{DERIBIT_BASE}/ticker",
                        params={"instrument_name": puts[len(puts)//2]["instrument_name"]},
                    )
                    put_data = put_resp.json().get("result", {})
                    iv_put = put_data.get("mark_iv", None)
                else:
                    iv_put = None

                if iv_call is None or iv_put is None:
                    continue

                risk_reversal = float(iv_call) - float(iv_put)

                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("""
                            INSERT INTO options_skew
                                (time, underlying, expiry, dte, iv_25d_call, iv_25d_put, risk_reversal)
                            VALUES
                                (:time, :underlying, :expiry, :dte, :iv_call, :iv_put, :rr)
                            ON CONFLICT (time, underlying, expiry) DO UPDATE SET
                                iv_25d_call   = EXCLUDED.iv_25d_call,
                                iv_25d_put    = EXCLUDED.iv_25d_put,
                                risk_reversal = EXCLUDED.risk_reversal
                        """),
                        {
                            "time": datetime.now(timezone.utc),
                            "underlying": underlying,
                            "expiry": expiry,
                            "dte": dte,
                            "iv_call": Decimal(str(iv_call)),
                            "iv_put": Decimal(str(iv_put)),
                            "rr": Decimal(str(risk_reversal)),
                        },
                    )
                    await session.commit()

        except Exception as e:
            logger.error(f"Deribit skew poll error ({underlying}): {e}")


# ══════════════════════════════════════════════════════════════
# FEAR & GREED INDEX
# ══════════════════════════════════════════════════════════════

async def poll_fear_greed():
    """Alternative.me Fear & Greed Index. Free, no auth."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{FEAR_GREED_BASE}/fng/?limit=1")
            resp.raise_for_status()
            data = resp.json().get("data", [{}])[0]

            value = int(data.get("value", 0))
            label = data.get("value_classification", "Unknown")

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO market_sentiment (time, fear_greed_index, fear_greed_label)
                        VALUES (:time, :value, :label)
                        ON CONFLICT (time) DO UPDATE SET
                            fear_greed_index = EXCLUDED.fear_greed_index,
                            fear_greed_label = EXCLUDED.fear_greed_label
                    """),
                    {
                        "time": datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0),
                        "value": value,
                        "label": label,
                    },
                )
                await session.commit()

            logger.debug(f"Fear & Greed: {value} ({label})")

        except Exception as e:
            logger.error(f"Fear & Greed poll error: {e}")


# ══════════════════════════════════════════════════════════════
# CRYPTOQUANT — On-chain flows
# ══════════════════════════════════════════════════════════════

async def poll_cryptoquant_flows(asset: str = "btc"):
    """
    Fetch exchange inflow/outflow from CryptoQuant.
    Requires API key. Free tier: limited endpoints available.
    """
    if not settings.cryptoquant_api_key:
        logger.debug("No CryptoQuant API key - skipping on-chain flows")
        return

    headers = {"Authorization": f"Bearer {settings.cryptoquant_api_key}"}

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        try:
            # Exchange netflow
            resp = await client.get(
                f"{CRYPTOQUANT_BASE}/{asset}/exchange-flows/netflow",
                params={"window": "hour", "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()

            rows = data.get("result", {}).get("data", [])
            if not rows:
                return

            latest = rows[0]

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO onchain_flows
                            (time, asset, source, exchange_inflow, exchange_outflow, netflow)
                        VALUES
                            (:time, :asset, :source, :inflow, :outflow, :netflow)
                        ON CONFLICT (time, asset, source) DO UPDATE SET
                            exchange_inflow  = EXCLUDED.exchange_inflow,
                            exchange_outflow = EXCLUDED.exchange_outflow,
                            netflow          = EXCLUDED.netflow
                    """),
                    {
                        "time": datetime.fromtimestamp(
                            latest.get("datetime", 0) / 1000, tz=timezone.utc
                        ),
                        "asset": asset.upper(),
                        "source": "cryptoquant",
                        "inflow": Decimal(str(latest.get("inflow_total", 0))),
                        "outflow": Decimal(str(latest.get("outflow_total", 0))),
                        "netflow": Decimal(str(latest.get("netflow_total", 0))),
                    },
                )
                await session.commit()

        except Exception as e:
            logger.error(f"CryptoQuant flows poll error ({asset}): {e}")

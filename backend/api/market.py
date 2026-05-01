"""
Market data API endpoints — serves the dashboard frontend.
All queries are fast due to TimescaleDB hypertable indexing.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional
import logging

from db.connection import get_db
from regime.detector import get_current_regime
from config.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


@router.get("/overview")
async def get_overview(db: AsyncSession = Depends(get_db)):
    """
    Full dashboard overview — all layers in one call.
    Returns current state of every signal layer for BTC and ETH.
    """
    result = {}

    for symbol in settings.instruments:
        base = symbol.replace("USDT", "")

        # Latest price + 24h change
        price_q = await db.execute(text("""
            SELECT close, open,
                   ROUND(((close - open) / open * 100)::numeric, 2) AS change_24h
            FROM ohlcv
            WHERE symbol = :sym AND timeframe = '1d'
            ORDER BY time DESC LIMIT 1
        """), {"sym": symbol})
        price_row = price_q.fetchone()

        # Current funding rate
        funding_q = await db.execute(text("""
            SELECT funding_rate_pct, mark_price, next_funding_time
            FROM funding_rates
            WHERE symbol = :sym
            ORDER BY time DESC LIMIT 1
        """), {"sym": symbol})
        funding_row = funding_q.fetchone()

        # Latest OI (aggregated)
        oi_q = await db.execute(text("""
            SELECT oi_usd, time
            FROM open_interest
            WHERE symbol = :sym AND source = 'coinglass_agg'
            ORDER BY time DESC LIMIT 1
        """), {"sym": symbol})
        oi_row = oi_q.fetchone()

        # OI change over 4h
        oi_4h_q = await db.execute(text("""
            SELECT oi_usd
            FROM open_interest
            WHERE symbol = :sym AND source = 'coinglass_agg'
              AND time <= NOW() - INTERVAL '4 hours'
            ORDER BY time DESC LIMIT 1
        """), {"sym": symbol})
        oi_4h_row = oi_4h_q.fetchone()

        # Recent large liquidations (last 4h)
        liq_q = await db.execute(text("""
            SELECT side, SUM(usd_value) AS total_usd
            FROM liquidations
            WHERE symbol = :sym AND time >= NOW() - INTERVAL '4 hours'
            GROUP BY side
        """), {"sym": symbol})
        liq_rows = liq_q.fetchall()
        liqs = {r.side: float(r.total_usd or 0) for r in liq_rows}

        # Options skew (nearest expiry)
        skew_q = await db.execute(text("""
            SELECT risk_reversal, iv_atm, dte
            FROM options_skew
            WHERE underlying = :base
            ORDER BY time DESC, dte ASC LIMIT 1
        """), {"base": base})
        skew_row = skew_q.fetchone()

        # On-chain flows
        flow_q = await db.execute(text("""
            SELECT netflow, exchange_inflow, exchange_outflow
            FROM onchain_flows
            WHERE asset = :asset AND source = 'cryptoquant'
            ORDER BY time DESC LIMIT 1
        """), {"asset": base})
        flow_row = flow_q.fetchone()

        # Current regime (4h timeframe)
        regime = await get_current_regime(symbol, "4h")

        result[symbol] = {
            "price": {
                "current": float(price_row.close) if price_row else None,
                "change_24h_pct": float(price_row.change_24h) if price_row else None,
            },
            "funding": {
                "rate_pct": float(funding_row.funding_rate_pct) if funding_row else None,
                "mark_price": float(funding_row.mark_price) if funding_row else None,
                "next_settlement": funding_row.next_funding_time.isoformat() if funding_row and funding_row.next_funding_time else None,
                "signal": _funding_signal(float(funding_row.funding_rate_pct) if funding_row else 0),
            },
            "open_interest": {
                "usd": float(oi_row.oi_usd) if oi_row and oi_row.oi_usd else None,
                "change_4h_pct": _pct_change(
                    float(oi_row.oi_usd) if oi_row and oi_row.oi_usd else None,
                    float(oi_4h_row.oi_usd) if oi_4h_row and oi_4h_row.oi_usd else None,
                ),
            },
            "liquidations_4h": {
                "longs_usd": liqs.get("LONG", 0),
                "shorts_usd": liqs.get("SHORT", 0),
                "dominant": "longs" if liqs.get("LONG", 0) > liqs.get("SHORT", 0) else "shorts",
            },
            "options_skew": {
                "risk_reversal": float(skew_row.risk_reversal) if skew_row else None,
                "iv_atm": float(skew_row.iv_atm) if skew_row and skew_row.iv_atm else None,
                "dte": skew_row.dte if skew_row else None,
                "bias": _skew_bias(float(skew_row.risk_reversal) if skew_row else 0),
            },
            "onchain": {
                "netflow": float(flow_row.netflow) if flow_row else None,
                "signal": _flow_signal(float(flow_row.netflow) if flow_row else 0),
            },
            "regime": regime,
        }

    return result


@router.get("/ohlcv/{symbol}")
async def get_ohlcv(
    symbol: str,
    timeframe: str = Query(default="1h", regex="^(1h|4h|1d)$"),
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """OHLCV data for charting."""
    result = await db.execute(text("""
        SELECT time, open, high, low, close, volume
        FROM ohlcv
        WHERE symbol = :sym AND timeframe = :tf
        ORDER BY time DESC
        LIMIT :limit
    """), {"sym": symbol.upper(), "tf": timeframe, "limit": limit})

    rows = result.fetchall()
    return [
        {
            "time": r.time.isoformat(),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume),
        }
        for r in reversed(rows)
    ]


@router.get("/funding/{symbol}")
async def get_funding_history(
    symbol: str,
    hours: int = Query(default=168, le=8760),  # default 7 days
    db: AsyncSession = Depends(get_db),
):
    """Funding rate history for a symbol."""
    result = await db.execute(text("""
        SELECT time, funding_rate_pct, mark_price
        FROM funding_rates
        WHERE symbol = :sym AND time >= NOW() - (:hours || ' hours')::interval
        ORDER BY time ASC
    """), {"sym": symbol.upper(), "hours": hours})

    rows = result.fetchall()
    return [
        {
            "time": r.time.isoformat(),
            "rate_pct": float(r.funding_rate_pct),
            "mark_price": float(r.mark_price) if r.mark_price else None,
        }
        for r in rows
    ]


@router.get("/sentiment")
async def get_sentiment(db: AsyncSession = Depends(get_db)):
    """Current market sentiment snapshot."""
    result = await db.execute(text("""
        SELECT time, fear_greed_index, fear_greed_label,
               btc_dominance, eth_btc_ratio, stablecoin_dom,
               total_market_cap, altcoin_season_idx
        FROM market_sentiment
        ORDER BY time DESC LIMIT 1
    """))
    row = result.fetchone()

    if not row:
        return {}

    return {
        "fear_greed": {
            "value": row.fear_greed_index,
            "label": row.fear_greed_label,
        },
        "btc_dominance": float(row.btc_dominance) if row.btc_dominance else None,
        "eth_btc_ratio": float(row.eth_btc_ratio) if row.eth_btc_ratio else None,
        "stablecoin_dom": float(row.stablecoin_dom) if row.stablecoin_dom else None,
        "total_market_cap_usd": float(row.total_market_cap) if row.total_market_cap else None,
        "altcoin_season_idx": row.altcoin_season_idx,
        "as_of": row.time.isoformat(),
    }


@router.get("/regime/{symbol}")
async def get_regime(
    symbol: str,
    timeframe: str = Query(default="4h"),
    db: AsyncSession = Depends(get_db),
):
    """Current market regime + history."""
    current = await get_current_regime(symbol.upper(), timeframe)

    # History (last 30 days)
    history_q = await db.execute(text("""
        SELECT time, regime, adx_14, atr_pct, confidence
        FROM regime_snapshots
        WHERE symbol = :sym AND timeframe = :tf
          AND time >= NOW() - INTERVAL '30 days'
        ORDER BY time ASC
    """), {"sym": symbol.upper(), "tf": timeframe})

    history = [
        {
            "time": r.time.isoformat(),
            "regime": r.regime,
            "adx": float(r.adx_14),
            "atr_pct": float(r.atr_pct),
            "confidence": float(r.confidence),
        }
        for r in history_q.fetchall()
    ]

    return {"current": current, "history": history}


@router.get("/liquidations/{symbol}")
async def get_liquidations(
    symbol: str,
    hours: int = Query(default=24, le=168),
    db: AsyncSession = Depends(get_db),
):
    """Recent liquidation events."""
    result = await db.execute(text("""
        SELECT time, side, price, qty, usd_value
        FROM liquidations
        WHERE symbol = :sym AND time >= NOW() - (:hours || ' hours')::interval
        ORDER BY time DESC
        LIMIT 500
    """), {"sym": symbol.upper(), "hours": hours})

    rows = result.fetchall()
    return [
        {
            "time": r.time.isoformat(),
            "side": r.side,
            "price": float(r.price),
            "qty": float(r.qty),
            "usd_value": float(r.usd_value) if r.usd_value else None,
        }
        for r in rows
    ]


# ── Signal interpretation helpers ─────────────────────────────

def _funding_signal(rate_pct: float) -> str:
    """Interpret funding rate as a trading signal."""
    if rate_pct > 0.15:
        return "extreme_long"    # crowded long → short bias / mean reversion
    elif rate_pct > 0.07:
        return "elevated_long"
    elif rate_pct < -0.05:
        return "extreme_short"   # crowded short → long bias
    elif rate_pct < -0.02:
        return "elevated_short"
    else:
        return "neutral"


def _skew_bias(risk_reversal: float) -> str:
    """Interpret options skew as directional bias."""
    if risk_reversal > 3:
        return "bullish"         # calls priced rich → upside demand
    elif risk_reversal < -3:
        return "bearish"         # puts priced rich → downside hedging
    else:
        return "neutral"


def _flow_signal(netflow: float) -> str:
    """Interpret on-chain netflow."""
    if netflow < -5000:
        return "strong_accumulation"   # coins leaving exchanges
    elif netflow < 0:
        return "mild_accumulation"
    elif netflow > 10000:
        return "strong_distribution"   # coins flowing to exchanges
    elif netflow > 0:
        return "mild_distribution"
    return "neutral"


def _pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 2)

"""
Regime detection - classifies current market state into:
  trending_bull  — strong uptrend (ADX > 25, price above EMA)
  trending_bear  — strong downtrend (ADX > 25, price below EMA)
  ranging        — sideways/consolidation (ADX < 20, low ATR%)
  volatile       — high volatility, no clear direction (ATR% spike)

Computed on closed bars and stored in regime_snapshots.
Strategy rules adapt per regime:
  trending:  momentum entries, trail stops, avoid counter-trend
  ranging:   fade extremes, mean reversion, tight TP
  volatile:  reduce size, widen stops or sit out
"""

import pandas as pd
import numpy as np
import logging
from datetime import datetime, timezone
from typing import Optional

from db.connection import AsyncSessionLocal
from sqlalchemy import text
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Regime thresholds (tune after 30+ trades in journal) ──────
ADX_TREND_THRESHOLD = 25       # above = trending
ADX_WEAK_THRESHOLD = 20        # below = ranging
ATR_PCT_VOLATILE = 3.5         # ATR% of price above = volatile
ATR_PCT_TIGHT = 1.2            # ATR% below = very tight range
EMA_FAST = 21
EMA_SLOW = 50


def classify_regime(
    adx: float,
    atr_pct: float,
    price: float,
    ema_fast: float,
    ema_slow: float,
) -> tuple[str, float]:
    """
    Returns (regime_label, confidence 0-1).
    Confidence reflects how clearly the market fits the regime.
    """

    # Volatile regime takes priority — high ATR% overrides trend/range
    if atr_pct > ATR_PCT_VOLATILE:
        confidence = min(1.0, (atr_pct - ATR_PCT_VOLATILE) / 2.0)
        return "volatile", round(confidence, 3)

    # Trending regimes
    if adx > ADX_TREND_THRESHOLD:
        confidence = min(1.0, (adx - ADX_TREND_THRESHOLD) / 25.0)
        if price > ema_fast and ema_fast > ema_slow:
            return "trending_bull", round(confidence, 3)
        elif price < ema_fast and ema_fast < ema_slow:
            return "trending_bear", round(confidence, 3)
        else:
            # ADX high but EMAs mixed — weak trend
            return "trending_bull" if price > ema_slow else "trending_bear", round(confidence * 0.6, 3)

    # Ranging regime
    if adx < ADX_WEAK_THRESHOLD and atr_pct < ATR_PCT_TIGHT:
        confidence = min(1.0, (ADX_WEAK_THRESHOLD - adx) / 15.0)
        return "ranging", round(confidence, 3)

    # Transitional — between trending and ranging
    confidence = 0.4
    if price > ema_slow:
        return "trending_bull", confidence
    else:
        return "trending_bear", confidence


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute ATR, ADX, EMAs on OHLCV DataFrame.
    Expects columns: time, open, high, low, close, volume
    """
    df = df.copy()
    df = df.sort_values("time").reset_index(drop=True)

    # Cast to float for pandas-ta
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    # ATR (14)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr_14"] = true_range.ewm(alpha=1/14, adjust=False).mean()
    df["atr_pct"] = (df["atr_14"] / df["close"]) * 100

    # ADX (14) - simplified Wilder's ADX
    plus_dm = df["high"].diff()
    minus_dm = -df["low"].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

    atr_smooth = df["atr_14"]
    plus_di = 100 * (plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_smooth)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    df["adx_14"] = dx.ewm(alpha=1/14, adjust=False).mean()

    # EMAs
    df[f"ema_{EMA_FAST}"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df[f"ema_{EMA_SLOW}"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    return df


async def compute_and_store_regime(symbol: str, timeframe: str):
    """
    Fetch recent OHLCV, compute indicators, classify regime,
    store latest snapshot in regime_snapshots.
    Called after each new closed bar.
    """
    # Need enough bars for indicators to warm up
    lookback = max(EMA_SLOW + 20, 100)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT time, open, high, low, close, volume
                FROM ohlcv
                WHERE symbol = :symbol AND timeframe = :timeframe
                ORDER BY time DESC
                LIMIT :limit
            """),
            {"symbol": symbol, "timeframe": timeframe, "limit": lookback},
        )
        rows = result.fetchall()

    if len(rows) < EMA_SLOW + 5:
        logger.debug(f"Not enough data for regime ({symbol}/{timeframe}): {len(rows)} bars")
        return

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df = compute_indicators(df)

    latest = df.iloc[-1]
    if pd.isna(latest["adx_14"]) or pd.isna(latest["atr_pct"]):
        return

    regime, confidence = classify_regime(
        adx=float(latest["adx_14"]),
        atr_pct=float(latest["atr_pct"]),
        price=float(latest["close"]),
        ema_fast=float(latest[f"ema_{EMA_FAST}"]),
        ema_slow=float(latest[f"ema_{EMA_SLOW}"]),
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO regime_snapshots
                    (time, symbol, timeframe, regime, atr_14, adx_14, atr_pct, confidence)
                VALUES
                    (:time, :symbol, :timeframe, :regime, :atr_14, :adx_14, :atr_pct, :confidence)
                ON CONFLICT (time, symbol, timeframe) DO UPDATE SET
                    regime     = EXCLUDED.regime,
                    atr_14     = EXCLUDED.atr_14,
                    adx_14     = EXCLUDED.adx_14,
                    atr_pct    = EXCLUDED.atr_pct,
                    confidence = EXCLUDED.confidence
            """),
            {
                "time": latest["time"],
                "symbol": symbol,
                "timeframe": timeframe,
                "regime": regime,
                "atr_14": float(latest["atr_14"]),
                "adx_14": float(latest["adx_14"]),
                "atr_pct": float(latest["atr_pct"]),
                "confidence": confidence,
            },
        )
        await session.commit()

    logger.debug(f"Regime {symbol}/{timeframe}: {regime} ({confidence:.2f})")


async def get_current_regime(symbol: str, timeframe: str = "4h") -> Optional[dict]:
    """Get the most recent regime snapshot for a symbol."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT regime, adx_14, atr_pct, confidence, time
                FROM regime_snapshots
                WHERE symbol = :symbol AND timeframe = :timeframe
                ORDER BY time DESC
                LIMIT 1
            """),
            {"symbol": symbol, "timeframe": timeframe},
        )
        row = result.fetchone()

    if not row:
        return None

    return {
        "regime": row.regime,
        "adx": float(row.adx_14),
        "atr_pct": float(row.atr_pct),
        "confidence": float(row.confidence),
        "as_of": row.time.isoformat(),
    }


async def backfill_regimes():
    """Backfill regime snapshots for all historical data. Run once after seed."""
    for symbol in settings.instruments:
        for tf in settings.timeframes:
            logger.info(f"Backfilling regimes for {symbol}/{tf}...")
            await compute_and_store_regime(symbol, tf)
    logger.info("Regime backfill complete.")

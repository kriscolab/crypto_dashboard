"""
Analytics endpoints — edge discovery from trade journal.
These queries answer: WHERE is my edge? What conditions produce best R?
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import logging

from db.connection import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/performance/summary")
async def get_performance_summary(db: AsyncSession = Depends(get_db)):
    """Overall performance stats from closed trades."""
    result = await db.execute(text("""
        SELECT
            COUNT(*)                                        AS total_trades,
            COUNT(*) FILTER (WHERE pnl_usd > 0)            AS winners,
            COUNT(*) FILTER (WHERE pnl_usd <= 0)           AS losers,
            ROUND(AVG(r_multiple)::numeric, 3)             AS avg_r,
            ROUND(SUM(pnl_usd)::numeric, 2)                AS total_pnl,
            ROUND(AVG(confluence_score)::numeric, 2)       AS avg_confluence,
            ROUND(MAX(r_multiple)::numeric, 3)             AS best_r,
            ROUND(MIN(r_multiple)::numeric, 3)             AS worst_r,
            ROUND(STDDEV(r_multiple)::numeric, 3)          AS r_stddev,
            -- Win rate
            ROUND(
                (COUNT(*) FILTER (WHERE pnl_usd > 0)::float /
                 NULLIF(COUNT(*), 0) * 100)::numeric, 1
            )                                               AS win_rate_pct,
            -- Expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
            ROUND((
                (COUNT(*) FILTER (WHERE pnl_usd > 0)::float / NULLIF(COUNT(*), 0)) *
                COALESCE(AVG(r_multiple) FILTER (WHERE r_multiple > 0), 0) -
                (COUNT(*) FILTER (WHERE pnl_usd <= 0)::float / NULLIF(COUNT(*), 0)) *
                ABS(COALESCE(AVG(r_multiple) FILTER (WHERE r_multiple <= 0), 0))
            )::numeric, 3)                                  AS expectancy_r
        FROM trades
        WHERE status = 'closed' AND r_multiple IS NOT NULL
    """))
    row = result.fetchone()
    return dict(row._mapping) if row else {}


@router.get("/performance/by-regime")
async def get_performance_by_regime(db: AsyncSession = Depends(get_db)):
    """Break down win rate and avg R by market regime at entry."""
    result = await db.execute(text("""
        SELECT
            COALESCE(entry_regime, 'unknown')               AS regime,
            COUNT(*)                                        AS trades,
            ROUND(AVG(r_multiple)::numeric, 3)             AS avg_r,
            ROUND(SUM(pnl_usd)::numeric, 2)                AS total_pnl,
            ROUND(
                COUNT(*) FILTER (WHERE pnl_usd > 0)::float /
                NULLIF(COUNT(*), 0) * 100, 1
            )                                               AS win_rate_pct,
            ROUND(AVG(confluence_score)::numeric, 2)       AS avg_confluence
        FROM trades
        WHERE status = 'closed' AND r_multiple IS NOT NULL
        GROUP BY entry_regime
        ORDER BY avg_r DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/performance/by-session")
async def get_performance_by_session(db: AsyncSession = Depends(get_db)):
    """Break down performance by trading session (Asia/London/NY)."""
    result = await db.execute(text("""
        SELECT
            COALESCE(entry_session, 'unknown')              AS session,
            COUNT(*)                                        AS trades,
            ROUND(AVG(r_multiple)::numeric, 3)             AS avg_r,
            ROUND(SUM(pnl_usd)::numeric, 2)                AS total_pnl,
            ROUND(
                COUNT(*) FILTER (WHERE pnl_usd > 0)::float /
                NULLIF(COUNT(*), 0) * 100, 1
            )                                               AS win_rate_pct
        FROM trades
        WHERE status = 'closed' AND r_multiple IS NOT NULL
        GROUP BY entry_session
        ORDER BY avg_r DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/performance/by-confluence-score")
async def get_performance_by_score(db: AsyncSession = Depends(get_db)):
    """
    Performance bucketed by confluence score.
    This is the key edge discovery query — proves whether higher score = better R.
    """
    result = await db.execute(text("""
        SELECT
            CASE
                WHEN confluence_score >= 8   THEN '8-10 (premium)'
                WHEN confluence_score >= 7   THEN '7-8 (full size)'
                WHEN confluence_score >= 5   THEN '5-7 (half size)'
                ELSE                              '<5 (no trade zone)'
            END                                             AS score_bucket,
            COUNT(*)                                        AS trades,
            ROUND(AVG(r_multiple)::numeric, 3)             AS avg_r,
            ROUND(SUM(pnl_usd)::numeric, 2)                AS total_pnl,
            ROUND(
                COUNT(*) FILTER (WHERE pnl_usd > 0)::float /
                NULLIF(COUNT(*), 0) * 100, 1
            )                                               AS win_rate_pct,
            ROUND(MIN(confluence_score)::numeric, 1)       AS score_min,
            ROUND(MAX(confluence_score)::numeric, 1)       AS score_max
        FROM trades
        WHERE status = 'closed' AND r_multiple IS NOT NULL
        GROUP BY score_bucket
        ORDER BY score_min DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/performance/by-setup")
async def get_performance_by_setup(db: AsyncSession = Depends(get_db)):
    """Performance by setup type — which setups actually work."""
    result = await db.execute(text("""
        SELECT
            COALESCE(setup_type, 'untagged')                AS setup,
            COUNT(*)                                        AS trades,
            ROUND(AVG(r_multiple)::numeric, 3)             AS avg_r,
            ROUND(SUM(pnl_usd)::numeric, 2)                AS total_pnl,
            ROUND(
                COUNT(*) FILTER (WHERE pnl_usd > 0)::float /
                NULLIF(COUNT(*), 0) * 100, 1
            )                                               AS win_rate_pct
        FROM trades
        WHERE status = 'closed' AND r_multiple IS NOT NULL
        GROUP BY setup_type
        HAVING COUNT(*) >= 3         -- only show setups with enough sample
        ORDER BY avg_r DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/performance/drawdown")
async def get_drawdown_curve(db: AsyncSession = Depends(get_db)):
    """Equity curve and drawdown over time."""
    result = await db.execute(text("""
        SELECT
            exit_time                                   AS time,
            r_multiple,
            pnl_usd,
            SUM(pnl_usd) OVER (ORDER BY exit_time)    AS cumulative_pnl,
            -- Running drawdown
            SUM(pnl_usd) OVER (ORDER BY exit_time) -
            MAX(SUM(pnl_usd) OVER (ORDER BY exit_time))
                OVER (ORDER BY exit_time)              AS drawdown_usd
        FROM trades
        WHERE status = 'closed' AND exit_time IS NOT NULL
        ORDER BY exit_time ASC
    """))
    rows = result.fetchall()
    return [
        {
            "time": r.time.isoformat(),
            "r_multiple": float(r.r_multiple) if r.r_multiple else None,
            "pnl_usd": float(r.pnl_usd) if r.pnl_usd else None,
            "cumulative_pnl": float(r.cumulative_pnl) if r.cumulative_pnl else None,
            "drawdown_usd": float(r.drawdown_usd) if r.drawdown_usd else None,
        }
        for r in rows
    ]


@router.get("/performance/funding-correlation")
async def get_funding_correlation(db: AsyncSession = Depends(get_db)):
    """
    Correlate entry funding rate with trade outcome.
    Key insight: do extreme funding rates actually produce better mean-reversion R?
    """
    result = await db.execute(text("""
        SELECT
            CASE
                WHEN entry_funding_rate > 0.15  THEN 'extreme_long (>0.15%)'
                WHEN entry_funding_rate > 0.07  THEN 'elevated_long (0.07-0.15%)'
                WHEN entry_funding_rate < -0.05 THEN 'extreme_short (<-0.05%)'
                WHEN entry_funding_rate < -0.02 THEN 'elevated_short (-0.02 to -0.05%)'
                ELSE                                 'neutral'
            END                                             AS funding_bucket,
            COUNT(*)                                        AS trades,
            ROUND(AVG(r_multiple)::numeric, 3)             AS avg_r,
            ROUND(
                COUNT(*) FILTER (WHERE pnl_usd > 0)::float /
                NULLIF(COUNT(*), 0) * 100, 1
            )                                               AS win_rate_pct
        FROM trades
        WHERE status = 'closed'
          AND r_multiple IS NOT NULL
          AND entry_funding_rate IS NOT NULL
        GROUP BY funding_bucket
        ORDER BY avg_r DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/layer-analysis")
async def get_layer_analysis(db: AsyncSession = Depends(get_db)):
    """
    Which individual confluence layers contribute most to winning trades?
    Compares avg R when each layer is active vs inactive.
    """
    layers = [
        ("structure", "structure_active"),
        ("order_flow", "order_flow_active"),
        ("funding", "funding_active"),
        ("options", "options_active"),
        ("onchain", "onchain_active"),
        ("macro", "macro_active"),
        ("liquidation", "liquidation_active"),
    ]

    analysis = []
    for layer_name, col in layers:
        result = await db.execute(text(f"""
            SELECT
                tc.{col}                                    AS active,
                COUNT(*)                                    AS trades,
                ROUND(AVG(t.r_multiple)::numeric, 3)       AS avg_r,
                ROUND(
                    COUNT(*) FILTER (WHERE t.pnl_usd > 0)::float /
                    NULLIF(COUNT(*), 0) * 100, 1
                )                                           AS win_rate_pct
            FROM trades t
            JOIN trade_confluence tc ON tc.trade_id = t.id
            WHERE t.status = 'closed' AND t.r_multiple IS NOT NULL
            GROUP BY tc.{col}
        """))
        rows = result.fetchall()
        layer_data = {str(r.active): {"trades": r.trades, "avg_r": float(r.avg_r or 0), "win_rate": float(r.win_rate_pct or 0)} for r in rows}
        analysis.append({
            "layer": layer_name,
            "when_active": layer_data.get("True", {}),
            "when_inactive": layer_data.get("False", {}),
            "r_delta": round(
                layer_data.get("True", {}).get("avg_r", 0) -
                layer_data.get("False", {}).get("avg_r", 0), 3
            ),
        })

    return sorted(analysis, key=lambda x: x["r_delta"], reverse=True)

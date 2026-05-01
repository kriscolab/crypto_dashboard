"""
Trade journal API endpoints.
Handles manual trade entry, confluence scoring, and trade management.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel, Field
from typing import Optional, List
from decimal import Decimal
from datetime import datetime
import uuid
import logging

from db.connection import get_db
from config.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


# ── Pydantic models ───────────────────────────────────────────

class ConfluenceInput(BaseModel):
    # Structure (weight 2.0)
    structure_active: bool = False
    structure_detail: Optional[str] = None
    structure_score: float = Field(default=0, ge=0, le=1)

    # Order flow (weight 2.0)
    order_flow_active: bool = False
    order_flow_detail: Optional[str] = None
    order_flow_score: float = Field(default=0, ge=0, le=1)

    # Funding (weight 1.5)
    funding_active: bool = False
    funding_detail: Optional[str] = None
    funding_value: Optional[float] = None
    funding_score: float = Field(default=0, ge=0, le=1)

    # Options (weight 1.5)
    options_active: bool = False
    options_detail: Optional[str] = None
    options_rr: Optional[float] = None
    options_score: float = Field(default=0, ge=0, le=1)

    # On-chain (weight 1.0)
    onchain_active: bool = False
    onchain_detail: Optional[str] = None
    onchain_score: float = Field(default=0, ge=0, le=1)

    # Macro/session (weight 1.0)
    macro_active: bool = False
    macro_detail: Optional[str] = None
    macro_score: float = Field(default=0, ge=0, le=1)

    # Liquidation map (weight 1.0)
    liquidation_active: bool = False
    liquidation_detail: Optional[str] = None
    liquidation_score: float = Field(default=0, ge=0, le=1)

    def compute_weighted_score(self) -> float:
        return round(
            self.structure_score   * settings.weight_structure    +
            self.order_flow_score  * settings.weight_order_flow   +
            self.funding_score     * settings.weight_funding       +
            self.options_score     * settings.weight_options       +
            self.onchain_score     * settings.weight_onchain       +
            self.macro_score       * settings.weight_macro         +
            self.liquidation_score * settings.weight_liquidation,
            2
        )

    def size_recommendation(self) -> str:
        score = self.compute_weighted_score()
        if score >= settings.score_full_size:
            return "full"
        elif score >= settings.score_half_size:
            return "half"
        else:
            return "no_trade"


class TradeInput(BaseModel):
    symbol: str
    side: str = Field(..., pattern="^(LONG|SHORT)$")
    entry_price: float
    entry_size_usd: float
    leverage: float = 1.0
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    risk_usd: Optional[float] = None
    setup_type: Optional[str] = None
    entry_session: Optional[str] = None
    notes: Optional[str] = None
    confluence: ConfluenceInput


class TradeCloseInput(BaseModel):
    exit_price: float
    exit_reason: str = Field(..., pattern="^(tp|sl|manual|liquidation)$")
    fees_usd: Optional[float] = None
    notes: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_trade(payload: TradeInput, db: AsyncSession = Depends(get_db)):
    """
    Log a new trade with full confluence breakdown.
    Automatically computes weighted score and size recommendation.
    """
    trade_id = str(uuid.uuid4())
    confluence_score = payload.confluence.compute_weighted_score()
    size_rec = payload.confluence.size_recommendation()

    # Snapshot current regime and sentiment from DB
    regime_q = await db.execute(text("""
        SELECT regime FROM regime_snapshots
        WHERE symbol = :sym AND timeframe = '4h'
        ORDER BY time DESC LIMIT 1
    """), {"sym": payload.symbol.upper()})
    regime_row = regime_q.fetchone()

    funding_q = await db.execute(text("""
        SELECT funding_rate_pct FROM funding_rates
        WHERE symbol = :sym ORDER BY time DESC LIMIT 1
    """), {"sym": payload.symbol.upper()})
    funding_row = funding_q.fetchone()

    fg_q = await db.execute(text("""
        SELECT fear_greed_index FROM market_sentiment ORDER BY time DESC LIMIT 1
    """))
    fg_row = fg_q.fetchone()

    # Insert trade
    await db.execute(text("""
        INSERT INTO trades (
            id, symbol, side, entry_price, entry_size_usd, leverage,
            entry_time, stop_loss, take_profit_1, take_profit_2, take_profit_3,
            risk_usd, setup_type, entry_session, notes,
            confluence_score, entry_regime, entry_funding_rate, entry_fear_greed,
            status
        ) VALUES (
            :id, :symbol, :side, :entry_price, :entry_size_usd, :leverage,
            NOW(), :sl, :tp1, :tp2, :tp3,
            :risk_usd, :setup_type, :entry_session, :notes,
            :confluence_score, :regime, :funding_rate, :fear_greed,
            'open'
        )
    """), {
        "id": trade_id,
        "symbol": payload.symbol.upper(),
        "side": payload.side,
        "entry_price": payload.entry_price,
        "entry_size_usd": payload.entry_size_usd,
        "leverage": payload.leverage,
        "sl": payload.stop_loss,
        "tp1": payload.take_profit_1,
        "tp2": payload.take_profit_2,
        "tp3": payload.take_profit_3,
        "risk_usd": payload.risk_usd,
        "setup_type": payload.setup_type,
        "entry_session": payload.entry_session,
        "notes": payload.notes,
        "confluence_score": confluence_score,
        "regime": regime_row.regime if regime_row else None,
        "funding_rate": float(funding_row.funding_rate_pct) if funding_row else None,
        "fear_greed": fg_row.fear_greed_index if fg_row else None,
    })

    # Insert confluence breakdown
    c = payload.confluence
    await db.execute(text("""
        INSERT INTO trade_confluence (
            trade_id,
            structure_active, structure_detail, structure_score,
            order_flow_active, order_flow_detail, order_flow_score,
            funding_active, funding_detail, funding_value, funding_score,
            options_active, options_detail, options_rr, options_score,
            onchain_active, onchain_detail, onchain_score,
            macro_active, macro_detail, macro_score,
            liquidation_active, liquidation_detail, liquidation_score,
            total_weighted_score
        ) VALUES (
            :trade_id,
            :str_a, :str_d, :str_s,
            :of_a, :of_d, :of_s,
            :fn_a, :fn_d, :fn_v, :fn_s,
            :op_a, :op_d, :op_rr, :op_s,
            :oc_a, :oc_d, :oc_s,
            :mc_a, :mc_d, :mc_s,
            :lq_a, :lq_d, :lq_s,
            :total
        )
    """), {
        "trade_id": trade_id,
        "str_a": c.structure_active, "str_d": c.structure_detail, "str_s": c.structure_score,
        "of_a": c.order_flow_active, "of_d": c.order_flow_detail, "of_s": c.order_flow_score,
        "fn_a": c.funding_active, "fn_d": c.funding_detail, "fn_v": c.funding_value, "fn_s": c.funding_score,
        "op_a": c.options_active, "op_d": c.options_detail, "op_rr": c.options_rr, "op_s": c.options_score,
        "oc_a": c.onchain_active, "oc_d": c.onchain_detail, "oc_s": c.onchain_score,
        "mc_a": c.macro_active, "mc_d": c.macro_detail, "mc_s": c.macro_score,
        "lq_a": c.liquidation_active, "lq_d": c.liquidation_detail, "lq_s": c.liquidation_score,
        "total": confluence_score,
    })

    await db.commit()

    return {
        "trade_id": trade_id,
        "confluence_score": confluence_score,
        "size_recommendation": size_rec,
        "message": f"Trade logged. Score: {confluence_score}/10 → {size_rec} size",
    }


@router.patch("/{trade_id}/close")
async def close_trade(
    trade_id: str,
    payload: TradeCloseInput,
    db: AsyncSession = Depends(get_db),
):
    """Close a trade and compute final P&L and R-multiple."""
    trade_q = await db.execute(text("""
        SELECT * FROM trades WHERE id = :id AND status = 'open'
    """), {"id": trade_id})
    trade = trade_q.fetchone()

    if not trade:
        raise HTTPException(status_code=404, detail="Open trade not found")

    # Compute PnL
    direction = 1 if trade.side == "LONG" else -1
    price_change_pct = (payload.exit_price - float(trade.entry_price)) / float(trade.entry_price)
    pnl_pct = direction * price_change_pct * float(trade.leverage) * 100
    pnl_usd = float(trade.entry_size_usd) * (pnl_pct / 100)
    net_pnl = pnl_usd - (payload.fees_usd or 0)

    # R-multiple
    r_multiple = None
    if trade.risk_usd and float(trade.risk_usd) > 0:
        r_multiple = round(net_pnl / float(trade.risk_usd), 3)

    await db.execute(text("""
        UPDATE trades SET
            exit_time    = NOW(),
            exit_price   = :exit_price,
            exit_reason  = :exit_reason,
            pnl_usd      = :pnl_usd,
            pnl_pct      = :pnl_pct,
            r_multiple   = :r_multiple,
            fees_usd     = :fees_usd,
            status       = 'closed',
            notes        = COALESCE(notes || E'\n' || :notes, notes, :notes)
        WHERE id = :id
    """), {
        "id": trade_id,
        "exit_price": payload.exit_price,
        "exit_reason": payload.exit_reason,
        "pnl_usd": round(net_pnl, 2),
        "pnl_pct": round(pnl_pct, 4),
        "r_multiple": r_multiple,
        "fees_usd": payload.fees_usd,
        "notes": payload.notes or "",
    })
    await db.commit()

    return {
        "trade_id": trade_id,
        "pnl_usd": round(net_pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "r_multiple": r_multiple,
        "status": "closed",
    }


@router.get("/open")
async def get_open_trades(db: AsyncSession = Depends(get_db)):
    """All currently open trades."""
    result = await db.execute(text("""
        SELECT t.*, tc.total_weighted_score
        FROM trades t
        LEFT JOIN trade_confluence tc ON tc.trade_id = t.id
        WHERE t.status = 'open'
        ORDER BY t.entry_time DESC
    """))
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/history")
async def get_trade_history(
    limit: int = 50,
    symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Closed trade history with confluence details."""
    filters = "WHERE t.status = 'closed'"
    params = {"limit": limit}
    if symbol:
        filters += " AND t.symbol = :sym"
        params["sym"] = symbol.upper()

    result = await db.execute(text(f"""
        SELECT t.id, t.symbol, t.side, t.entry_time, t.exit_time,
               t.entry_price, t.exit_price, t.pnl_usd, t.pnl_pct,
               t.r_multiple, t.confluence_score, t.entry_regime,
               t.setup_type, t.entry_session, t.exit_reason
        FROM trades t
        {filters}
        ORDER BY t.exit_time DESC
        LIMIT :limit
    """), params)

    rows = result.fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/score-preview")
async def preview_score(confluence: ConfluenceInput):
    """
    Preview confluence score before committing a trade.
    Use this in the UI to show score as user fills in layers.
    """
    score = confluence.compute_weighted_score()
    return {
        "score": score,
        "max_score": 10,
        "size_recommendation": confluence.size_recommendation(),
        "active_layers": sum([
            confluence.structure_active,
            confluence.order_flow_active,
            confluence.funding_active,
            confluence.options_active,
            confluence.onchain_active,
            confluence.macro_active,
            confluence.liquidation_active,
        ]),
    }

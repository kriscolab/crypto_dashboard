-- ═══════════════════════════════════════════════════════════════
-- Crypto Dashboard - TimescaleDB Schema
-- ═══════════════════════════════════════════════════════════════

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── INSTRUMENTS ───────────────────────────────────────────────
-- Master list of tracked instruments
CREATE TABLE IF NOT EXISTS instruments (
    id          SERIAL PRIMARY KEY,
    symbol      VARCHAR(20) NOT NULL UNIQUE,   -- e.g. BTCUSDT
    base        VARCHAR(10) NOT NULL,           -- BTC
    quote       VARCHAR(10) NOT NULL,           -- USDT
    market      VARCHAR(20) NOT NULL,           -- futures | spot
    exchange    VARCHAR(20) NOT NULL DEFAULT 'binance',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO instruments (symbol, base, quote, market) VALUES
    ('BTCUSDT', 'BTC', 'USDT', 'futures'),
    ('ETHUSDT', 'ETH', 'USDT', 'futures')
ON CONFLICT DO NOTHING;

-- ── OHLCV ─────────────────────────────────────────────────────
-- Price data - TimescaleDB hypertable partitioned by time
CREATE TABLE IF NOT EXISTS ohlcv (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(20) NOT NULL,
    timeframe   VARCHAR(5)  NOT NULL,   -- 1h, 4h, 1d
    open        NUMERIC(20, 8) NOT NULL,
    high        NUMERIC(20, 8) NOT NULL,
    low         NUMERIC(20, 8) NOT NULL,
    close       NUMERIC(20, 8) NOT NULL,
    volume      NUMERIC(30, 8) NOT NULL,
    quote_vol   NUMERIC(30, 8),         -- volume in USDT
    trades      INTEGER,
    PRIMARY KEY (time, symbol, timeframe)
);

SELECT create_hypertable(
    'ohlcv', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_tf
    ON ohlcv (symbol, timeframe, time DESC);

-- ── FUNDING RATES ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS funding_rates (
    time                TIMESTAMPTZ NOT NULL,
    symbol              VARCHAR(20) NOT NULL,
    funding_rate        NUMERIC(20, 10) NOT NULL,
    funding_rate_pct    NUMERIC(10, 6),          -- rate * 100
    mark_price          NUMERIC(20, 8),
    index_price         NUMERIC(20, 8),
    next_funding_time   TIMESTAMPTZ,
    PRIMARY KEY (time, symbol)
);

SELECT create_hypertable(
    'funding_rates', 'time',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_funding_symbol
    ON funding_rates (symbol, time DESC);

-- ── OPEN INTEREST ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS open_interest (
    time            TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    source          VARCHAR(20) NOT NULL DEFAULT 'binance',   -- binance | coinglass_agg
    oi_contracts    NUMERIC(30, 8),      -- in contracts
    oi_usd          NUMERIC(30, 2),      -- in USD
    PRIMARY KEY (time, symbol, source)
);

SELECT create_hypertable(
    'open_interest', 'time',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- ── LIQUIDATIONS ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS liquidations (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(20) NOT NULL,
    side        VARCHAR(5) NOT NULL,    -- LONG | SHORT
    price       NUMERIC(20, 8) NOT NULL,
    qty         NUMERIC(20, 8) NOT NULL,
    usd_value   NUMERIC(20, 2),
    PRIMARY KEY (time, symbol, side, price)
);

SELECT create_hypertable(
    'liquidations', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_liq_symbol
    ON liquidations (symbol, time DESC);

-- ── OPTIONS SKEW ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS options_skew (
    time            TIMESTAMPTZ NOT NULL,
    underlying      VARCHAR(10) NOT NULL,   -- BTC | ETH
    expiry          DATE NOT NULL,
    dte             INTEGER,                -- days to expiry
    iv_25d_call     NUMERIC(10, 4),         -- 25-delta call IV %
    iv_25d_put      NUMERIC(10, 4),         -- 25-delta put IV %
    risk_reversal   NUMERIC(10, 4),         -- call IV - put IV (positive = bullish skew)
    iv_atm          NUMERIC(10, 4),         -- at-the-money IV %
    put_call_oi_ratio NUMERIC(10, 4),
    PRIMARY KEY (time, underlying, expiry)
);

SELECT create_hypertable(
    'options_skew', 'time',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- ── ON-CHAIN FLOWS ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS onchain_flows (
    time            TIMESTAMPTZ NOT NULL,
    asset           VARCHAR(10) NOT NULL,   -- BTC | ETH
    source          VARCHAR(30) NOT NULL,   -- cryptoquant | glassnode
    exchange_inflow  NUMERIC(20, 8),        -- coins flowing TO exchanges
    exchange_outflow NUMERIC(20, 8),        -- coins flowing FROM exchanges
    netflow         NUMERIC(20, 8),         -- outflow - inflow (negative = selling pressure)
    whale_inflow    NUMERIC(20, 8),         -- large tx inflow
    PRIMARY KEY (time, asset, source)
);

SELECT create_hypertable(
    'onchain_flows', 'time',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- ── FEAR & GREED + MACRO ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_sentiment (
    time                TIMESTAMPTZ NOT NULL PRIMARY KEY,
    fear_greed_index    INTEGER,            -- 0-100
    fear_greed_label    VARCHAR(20),        -- Extreme Fear → Extreme Greed
    btc_dominance       NUMERIC(6, 3),      -- BTC % of total market cap
    eth_btc_ratio       NUMERIC(10, 8),     -- ETH/BTC price ratio
    stablecoin_dom      NUMERIC(6, 3),      -- stablecoin % of total cap
    total_market_cap    NUMERIC(30, 2),     -- USD
    altcoin_season_idx  INTEGER             -- 0-100
);

SELECT create_hypertable(
    'market_sentiment', 'time',
    chunk_time_interval => INTERVAL '90 days',
    if_not_exists => TRUE
);

-- ── REGIME SNAPSHOTS ──────────────────────────────────────────
-- Computed market regime per bar - stored so we can query trade history by regime
CREATE TABLE IF NOT EXISTS regime_snapshots (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(20) NOT NULL,
    timeframe   VARCHAR(5)  NOT NULL,
    regime      VARCHAR(20) NOT NULL,   -- trending_bull | trending_bear | ranging | volatile
    atr_14      NUMERIC(20, 8),
    adx_14      NUMERIC(10, 4),
    atr_pct     NUMERIC(10, 4),         -- ATR as % of price
    confidence  NUMERIC(5, 3),          -- 0-1
    PRIMARY KEY (time, symbol, timeframe)
);

SELECT create_hypertable(
    'regime_snapshots', 'time',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- ═══════════════════════════════════════════════════════════════
-- TRADE JOURNAL TABLES (non-time-series, standard postgres)
-- ═══════════════════════════════════════════════════════════════

-- ── TRADES ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Instrument
    symbol              VARCHAR(20) NOT NULL,
    side                VARCHAR(5) NOT NULL,    -- LONG | SHORT
    market              VARCHAR(20) NOT NULL DEFAULT 'futures',

    -- Entry
    entry_time          TIMESTAMPTZ NOT NULL,
    entry_price         NUMERIC(20, 8) NOT NULL,
    entry_size_usd      NUMERIC(20, 2) NOT NULL,
    leverage            NUMERIC(5, 2) NOT NULL DEFAULT 1,

    -- Exit
    exit_time           TIMESTAMPTZ,
    exit_price          NUMERIC(20, 8),
    exit_reason         VARCHAR(30),            -- tp | sl | manual | liquidation

    -- Risk
    stop_loss           NUMERIC(20, 8),
    take_profit_1       NUMERIC(20, 8),
    take_profit_2       NUMERIC(20, 8),
    take_profit_3       NUMERIC(20, 8),
    risk_usd            NUMERIC(20, 2),         -- max $ risk on trade

    -- Results
    pnl_usd             NUMERIC(20, 2),
    pnl_pct             NUMERIC(10, 4),
    r_multiple          NUMERIC(10, 4),         -- pnl / risk (e.g. 2.5R)
    fees_usd            NUMERIC(20, 2),

    -- Context at entry (snapshot)
    entry_regime        VARCHAR(20),
    entry_session       VARCHAR(20),            -- asia | london | ny | weekend
    entry_funding_rate  NUMERIC(10, 6),
    entry_fear_greed    INTEGER,

    -- Scoring
    confluence_score    NUMERIC(5, 2),          -- weighted score 0-10
    setup_type          VARCHAR(50),            -- e.g. "vah_rejection_long"

    -- State
    status              VARCHAR(20) NOT NULL DEFAULT 'open',   -- open | closed | cancelled

    -- Notes
    notes               TEXT,
    screenshots         TEXT[]                  -- array of S3/URL paths
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades (entry_regime);

-- ── TRADE CONFLUENCE LAYERS ───────────────────────────────────
-- Which specific layers were active for each trade
CREATE TABLE IF NOT EXISTS trade_confluence (
    id                      SERIAL PRIMARY KEY,
    trade_id                UUID NOT NULL REFERENCES trades(id) ON DELETE CASCADE,

    -- Structure layer (weight 2)
    structure_active        BOOLEAN DEFAULT FALSE,
    structure_detail        VARCHAR(100),       -- e.g. "price at VAH, POC below"
    structure_score         NUMERIC(4, 2),

    -- Order flow layer (weight 2)
    order_flow_active       BOOLEAN DEFAULT FALSE,
    order_flow_detail       VARCHAR(100),       -- e.g. "delta exhaustion at level"
    order_flow_score        NUMERIC(4, 2),

    -- Funding rate layer (weight 1.5)
    funding_active          BOOLEAN DEFAULT FALSE,
    funding_detail          VARCHAR(100),       -- e.g. "funding 0.12% - extreme long"
    funding_value           NUMERIC(10, 6),
    funding_score           NUMERIC(4, 2),

    -- Options skew layer (weight 1.5)
    options_active          BOOLEAN DEFAULT FALSE,
    options_detail          VARCHAR(100),       -- e.g. "RR -3.5%, put skew bearish"
    options_rr              NUMERIC(10, 4),
    options_score           NUMERIC(4, 2),

    -- On-chain layer (weight 1)
    onchain_active          BOOLEAN DEFAULT FALSE,
    onchain_detail          VARCHAR(100),       -- e.g. "exchange outflow 12k BTC"
    onchain_score           NUMERIC(4, 2),

    -- Macro/session layer (weight 1)
    macro_active            BOOLEAN DEFAULT FALSE,
    macro_detail            VARCHAR(100),       -- e.g. "NY open, risk-on, BTC dom falling"
    macro_score             NUMERIC(4, 2),

    -- Liquidation map layer (weight 1)
    liquidation_active      BOOLEAN DEFAULT FALSE,
    liquidation_detail      VARCHAR(100),       -- e.g. "large short cluster at 69k"
    liquidation_score       NUMERIC(4, 2),

    total_weighted_score    NUMERIC(5, 2)       -- computed total
);

CREATE INDEX IF NOT EXISTS idx_confluence_trade ON trade_confluence (trade_id);

-- ── DAILY STATS ───────────────────────────────────────────────
-- Rolled-up daily performance - rebuilt nightly
CREATE TABLE IF NOT EXISTS daily_stats (
    date                DATE PRIMARY KEY,
    trades_taken        INTEGER DEFAULT 0,
    trades_won          INTEGER DEFAULT 0,
    trades_lost         INTEGER DEFAULT 0,
    win_rate            NUMERIC(5, 2),
    gross_pnl           NUMERIC(20, 2),
    net_pnl             NUMERIC(20, 2),
    fees_paid           NUMERIC(20, 2),
    avg_r_multiple      NUMERIC(10, 4),
    best_trade_r        NUMERIC(10, 4),
    worst_trade_r       NUMERIC(10, 4),
    max_drawdown_pct    NUMERIC(10, 4),
    avg_confluence      NUMERIC(5, 2),
    running_balance     NUMERIC(20, 2)
);

-- ── CONTINUOUS AGGREGATES (TimescaleDB feature) ───────────────
-- Pre-computed hourly OHLCV rollups for fast dashboard queries

CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_4h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('4 hours', time) AS bucket,
    symbol,
    first(open, time)   AS open,
    max(high)           AS high,
    min(low)            AS low,
    last(close, time)   AS close,
    sum(volume)         AS volume,
    sum(quote_vol)      AS quote_vol
FROM ohlcv
WHERE timeframe = '1h'
GROUP BY bucket, symbol
WITH NO DATA;

-- ── HELPER FUNCTIONS ──────────────────────────────────────────

-- Auto-update updated_at on trades
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Compute weighted confluence score
CREATE OR REPLACE FUNCTION compute_confluence_score(
    p_structure     NUMERIC,
    p_order_flow    NUMERIC,
    p_funding       NUMERIC,
    p_options       NUMERIC,
    p_onchain       NUMERIC,
    p_macro         NUMERIC,
    p_liquidation   NUMERIC
) RETURNS NUMERIC AS $$
BEGIN
    RETURN ROUND(
        (COALESCE(p_structure, 0)   * 2.0  +
         COALESCE(p_order_flow, 0)  * 2.0  +
         COALESCE(p_funding, 0)     * 1.5  +
         COALESCE(p_options, 0)     * 1.5  +
         COALESCE(p_onchain, 0)     * 1.0  +
         COALESCE(p_macro, 0)       * 1.0  +
         COALESCE(p_liquidation, 0) * 1.0), 2
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON TABLE trades IS 'Core trade journal - manual entry with auto-import planned';
COMMENT ON TABLE trade_confluence IS 'Layer-by-layer confluence breakdown per trade';
COMMENT ON TABLE ohlcv IS 'TimescaleDB hypertable - OHLCV for BTC/ETH, 1h/4h/1d';
COMMENT ON TABLE regime_snapshots IS 'Computed market regime stored per bar for analytics';

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- One row per scan run
CREATE TABLE IF NOT EXISTS scan_summary (
    scanned_at      TIMESTAMPTZ NOT NULL,
    trigger_type    TEXT        NOT NULL DEFAULT 'manual',  -- 'manual' | 'auto'
    total_launches  INT,
    sent_to_llm     INT,
    stale_count     INT,
    interesting_count INT,
    watch_count     INT,
    skip_count      INT,
    elapsed_ms      INT
);

SELECT create_hypertable('scan_summary', 'scanned_at', if_not_exists => TRUE);

-- One row per WATCH or INTERESTING token per scan
CREATE TABLE IF NOT EXISTS token_signals (
    scanned_at      TIMESTAMPTZ NOT NULL,
    address         TEXT        NOT NULL,
    pair_address    TEXT,
    symbol          TEXT,
    chain           TEXT,
    dex             TEXT,
    rating          TEXT        NOT NULL,  -- 'WATCH' | 'INTERESTING'
    age_minutes     INT,
    -- Price & liquidity at scan time
    price_usd       NUMERIC,
    liquidity_usd   NUMERIC,
    volume_1h       NUMERIC,
    volume_5m       NUMERIC,
    -- Price changes
    price_ch_5m     NUMERIC,
    price_ch_1h     NUMERIC,
    price_ch_6h     NUMERIC,
    -- Momentum signals
    buy_pct_5m      NUMERIC,
    buy_pct_1h      NUMERIC,
    vl_ratio        NUMERIC,
    vol_trend       TEXT,
    micro_trend     TEXT,
    -- Safety
    flags           TEXT,
    -- LLM output
    reasoning       TEXT,
    -- INTERESTING only — null for WATCH
    entry_price     NUMERIC,
    target_price    NUMERIC,
    stop_price      NUMERIC,
    -- Birdeye enrichment (Solana only; null for Base)
    unique_traders_1h INT,
    net_inflow_usd  NUMERIC,
    -- Outcome backfill (populated by outcome tracker workflow)
    price_at_5m     NUMERIC,
    price_at_15m    NUMERIC,
    price_at_30m    NUMERIC,
    price_peak_30m  NUMERIC,
    target_hit      BOOLEAN,
    stop_hit        BOOLEAN
);

SELECT create_hypertable('token_signals', 'scanned_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_ts_address   ON token_signals (address, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_ts_rating    ON token_signals (rating,  scanned_at DESC);
-- Partial index to quickly find rows needing outcome backfill
CREATE INDEX IF NOT EXISTS idx_ts_pending   ON token_signals (scanned_at)
    WHERE price_at_5m IS NULL;

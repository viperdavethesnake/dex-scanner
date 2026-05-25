-- DEX Trader DB — idempotent schema
-- Executed by TimescaleDB docker-entrypoint-initdb.d on first start.
-- Also executed by db.migrate() on every trader startup (all statements safe to re-run).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── trades ────────────────────────────────────────────────────────────────────
-- One row per trade candidate. Created at 'intent'; updated through lifecycle.

CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL        NOT NULL,
    created_at          TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    -- Identity
    chain               TEXT             NOT NULL,
    token_address       TEXT             NOT NULL,
    pair_address        TEXT,
    symbol              TEXT,

    -- Signal at decision time (from raw_signals)
    signal_ts           TIMESTAMPTZ      NOT NULL,
    signal_price_usd    NUMERIC(30,12),
    conviction_score    REAL             NOT NULL,
    conviction_band     TEXT,                        -- 'shadow_only'(0.65-0.70) | 'live_eligible'(>=0.70)
    model_version       TEXT,
    signal_features     JSONB,                       -- enriched feature dict (nullable; populated opportunistically)
    collector_signal_id BIGINT,

    -- Security check
    security_checked    BOOLEAN          NOT NULL DEFAULT FALSE,
    security_passed     BOOLEAN,
    security_source     TEXT,                        -- 'goplus+honeypot' | 'cache' | 'skipped'
    security_flags      TEXT,

    -- Quote (entry)
    quote_ts            TIMESTAMPTZ,
    quote_source        TEXT,                        -- '0x' | 'aerodrome' | 'uniswap_v3' | 'none'
    quote_price_usd     NUMERIC(30,12),
    quote_slippage_bps  INT,
    quote_gas_usd       NUMERIC(10,4),
    quote_latency_ms    INT,

    -- Simulated fill (entry)
    fill_ts             TIMESTAMPTZ,
    fill_size_usd       NUMERIC(10,4),
    fill_price_usd      NUMERIC(30,12),
    entry_cost_pct      REAL,                        -- (fill_price - signal_price) / signal_price * 100

    -- Exit (fill_ts + POSITION_HOLD_SECONDS = expected_exit_ts)
    exit_ts             TIMESTAMPTZ,
    exit_trigger        TEXT,                        -- 'timer' | 'kill_switch'
    exit_price_usd      NUMERIC(30,12),              -- DexScreener price at exit (parallel truth)
    exit_quote_usd      NUMERIC(30,12),              -- aggregator quote at exit (primary P&L basis)
    exit_quote_source   TEXT,
    exit_quote_latency_ms INT,

    -- P&L (computed using exit_quote_usd as exit price)
    gross_pct           REAL,
    cost_pct            REAL,
    net_pct             REAL,
    pnl_usd             NUMERIC(10,4),

    -- Backtest comparison
    backtest_cost_pct   REAL             NOT NULL DEFAULT 1.5,
    backtest_net_pct    REAL,
    cost_delta_pct      REAL,                        -- real cost_pct - 1.5

    -- State machine
    status              TEXT             NOT NULL
        CHECK (status IN ('intent','quoted','simulated','managed','exited','failed','skipped')),
    failure_reason      TEXT,

    PRIMARY KEY (id, created_at)
);

SELECT create_hypertable('trades', 'created_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_trades_token  ON trades (token_address, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_open   ON trades (created_at DESC)
    WHERE status IN ('intent','quoted','simulated','managed');


-- ── trader_state ──────────────────────────────────────────────────────────────
-- Key-value config. Kill switch lives here.
-- Toggle: UPDATE trader_state SET value='true' WHERE key='kill_switch';

CREATE TABLE IF NOT EXISTS trader_state (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO trader_state (key, value) VALUES
    ('kill_switch', 'false'),
    ('shadow_mode',  'true')
ON CONFLICT DO NOTHING;


-- ── signal_watermark ──────────────────────────────────────────────────────────
-- Tracks how far we've read into raw_signals (collector DB).
-- Updated after each ingest cycle.

CREATE TABLE IF NOT EXISTS signal_watermark (
    id         INT         PRIMARY KEY DEFAULT 1,
    last_id    BIGINT      NOT NULL DEFAULT 0,
    last_ts    TIMESTAMPTZ
);

INSERT INTO signal_watermark (id, last_id) VALUES (1, 0) ON CONFLICT DO NOTHING;

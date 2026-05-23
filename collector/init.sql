CREATE TABLE IF NOT EXISTS raw_signals (
    id              BIGSERIAL NOT NULL,
    scanned_at      TIMESTAMPTZ NOT NULL,
    token_address   TEXT NOT NULL,
    pair_address    TEXT,
    symbol          TEXT,
    name            TEXT,
    chain           TEXT NOT NULL,
    dex             TEXT,
    pair_created_at TIMESTAMPTZ,
    age_minutes     REAL,

    price_usd       NUMERIC(30,12),
    liquidity_usd   NUMERIC(20,4),
    market_cap      NUMERIC(20,4),
    volume_5m       NUMERIC(20,4),
    volume_1h       NUMERIC(20,4),
    volume_6h       NUMERIC(20,4),
    price_ch_5m     REAL,
    price_ch_1h     REAL,
    price_ch_6h     REAL,
    buys_1h         INT,
    sells_1h        INT,
    buys_5m         INT,
    sells_5m        INT,

    vl_ratio        REAL,
    vol_trend       TEXT,
    vol_trend_pct   REAL,
    micro_trend     TEXT,
    buy_pct_5m      REAL,
    buy_pct_1h      REAL,

    price_at_5m     NUMERIC(30,12),
    outcome_pct     REAL,

    PRIMARY KEY (id, scanned_at)
);

SELECT create_hypertable('raw_signals', 'scanned_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_rs_token_time  ON raw_signals (token_address, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_rs_pending     ON raw_signals (scanned_at DESC) WHERE price_at_5m IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_rs_dedup ON raw_signals (token_address, pair_address, scanned_at);

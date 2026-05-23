import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

import psycopg2
import psycopg2.extras

from signals import Token

log = logging.getLogger(__name__)

INSERT_SQL = """
INSERT INTO raw_signals (
    scanned_at, token_address, pair_address, symbol, name, chain, dex,
    pair_created_at, age_minutes,
    price_usd, liquidity_usd, market_cap,
    volume_5m, volume_1h, volume_6h,
    price_ch_5m, price_ch_1h, price_ch_6h,
    buys_1h, sells_1h, buys_5m, sells_5m,
    vl_ratio, vol_trend, vol_trend_pct, micro_trend, buy_pct_5m, buy_pct_1h
) VALUES %s
ON CONFLICT (token_address, pair_address, scanned_at) DO NOTHING
"""

PENDING_SQL = """
SELECT id, scanned_at, chain, pair_address, price_usd
FROM raw_signals
WHERE price_at_5m IS NULL
  AND scanned_at < NOW() - INTERVAL '5 minutes'
ORDER BY scanned_at ASC
LIMIT 100
"""

UPDATE_OUTCOME_SQL = """
UPDATE raw_signals
SET price_at_5m = %s,
    outcome_pct = CASE WHEN %s > 0 THEN (%s - price_usd) / price_usd * 100 ELSE NULL END
WHERE id = %s AND scanned_at = %s
"""


def connect(retries=10, delay=5):
    dsn = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", 5432)),
        "dbname": os.environ.get("DB_NAME", "collector_signals"),
        "user": os.environ.get("DB_USER", "collector"),
        "password": os.environ.get("DB_PASS", "collector"),
    }
    for attempt in range(retries):
        try:
            conn = psycopg2.connect(**dsn)
            conn.autocommit = False
            log.info("connected to DB at %s:%s/%s", dsn["host"], dsn["port"], dsn["dbname"])
            return conn
        except psycopg2.OperationalError as e:
            if attempt == retries - 1:
                raise
            log.warning("DB not ready (%s), retrying in %ds…", e, delay)
            time.sleep(delay)


def _epoch_ms_to_dt(ms) -> Optional[datetime]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def bulk_insert(conn, tokens: List[Token], scanned_at: datetime) -> int:
    if not tokens:
        return 0
    rows = []
    for t in tokens:
        rows.append((
            scanned_at,
            t.token_address, t.pair_address, t.symbol, t.name, t.chain, t.dex,
            _epoch_ms_to_dt(t.pair_created_at), t.age_minutes,
            t.price_usd, t.liquidity_usd, t.market_cap,
            t.volume_5m, t.volume_1h, t.volume_6h,
            t.price_ch_5m, t.price_ch_1h, t.price_ch_6h,
            t.buys_1h, t.sells_1h, t.buys_5m, t.sells_5m,
            t.vl_ratio, t.vol_trend, t.vol_trend_pct, t.micro_trend,
            t.buy_pct_5m, t.buy_pct_1h,
        ))
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, INSERT_SQL, rows)
    conn.commit()
    return len(rows)


def fetch_pending_outcomes(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(PENDING_SQL)
        return cur.fetchall()


def update_outcome(conn, row_id: int, scanned_at, price_at_5m: float):
    with conn.cursor() as cur:
        cur.execute(UPDATE_OUTCOME_SQL, (
            price_at_5m, price_at_5m, price_at_5m,
            row_id, scanned_at,
        ))
    conn.commit()

"""Database connections and idempotent schema migration.

Two DB connections:
  trader_conn   — dex-trader-db (writes, state)
  collector_conn — dex-collector-db (read-only: raw_signals)

db.migrate() executes init.sql on the trader DB at every startup.
All SQL in init.sql is idempotent (IF NOT EXISTS, ON CONFLICT DO NOTHING).
"""
import logging
import os
import time

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

_INIT_SQL_PATH = os.path.join(os.path.dirname(__file__), "init.sql")

TRADER_DSN = dict(
    host     = os.environ.get("TRADER_DB_HOST",  "dex-trader-db"),
    port     = int(os.environ.get("TRADER_DB_PORT", "5432")),
    dbname   = os.environ.get("TRADER_DB_NAME",  "trader"),
    user     = os.environ.get("TRADER_DB_USER",  "trader"),
    password = os.environ.get("TRADER_DB_PASS",  "trader"),
)

COLLECTOR_DSN = dict(
    host     = os.environ.get("COLLECTOR_DB_HOST", "dex-collector-db"),
    port     = int(os.environ.get("COLLECTOR_DB_PORT", "5432")),
    dbname   = os.environ.get("COLLECTOR_DB_NAME", "collector_signals"),
    user     = os.environ.get("COLLECTOR_DB_USER", "collector"),
    password = os.environ.get("COLLECTOR_DB_PASS", "collector"),
)


def _connect(dsn: dict, retries: int = 12, delay: int = 5):
    for attempt in range(retries):
        try:
            conn = psycopg2.connect(**dsn)
            conn.autocommit = False
            log.info("db: connected to %s:%s/%s", dsn["host"], dsn["port"], dsn["dbname"])
            return conn
        except psycopg2.OperationalError as exc:
            if attempt == retries - 1:
                raise
            log.warning("db: %s not ready (%s), retry %d/%d in %ds…",
                        dsn["dbname"], exc, attempt + 1, retries, delay)
            time.sleep(delay)


def connect_trader():
    return _connect(TRADER_DSN)


def connect_collector():
    return _connect(COLLECTOR_DSN)


def migrate(conn) -> None:
    """Execute init.sql on the trader DB. Safe to call on every startup."""
    with open(_INIT_SQL_PATH) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    log.info("db: migration complete")

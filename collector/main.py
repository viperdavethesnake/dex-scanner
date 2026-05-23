"""
dex-collector — continuous DexScreener poller and outcome tracker.

Collects every Base/Solana token DexScreener surfaces, computes the same
signals as the scanner, records the filter decision (pass or fail + reason),
and backfills 5-minute price outcomes for all tokens.

No GPU required. Runs independently of n8n and llamacpp.
"""
import logging
import os
import time
from datetime import datetime, timezone

import api
import db
import signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))


def poll(conn):
    scanned_at = datetime.now(tz=timezone.utc)

    profiles = api.fetch_profiles()
    if not profiles:
        log.warning("no profiles returned from DexScreener")
        return

    tokens = []
    seen = set()

    for profile in profiles:
        chain_id = profile.get("chainId")
        token_address = profile.get("tokenAddress", "")
        if not token_address:
            continue

        pair = api.fetch_pair(token_address, chain_id)
        if not pair:
            continue

        key = (token_address, pair.get("pairAddress", ""))
        if key in seen:
            continue
        seen.add(key)

        t = signals.from_pair(pair, chain_id)
        signals.compute_signals(t)
        tokens.append(t)

    inserted = db.bulk_insert(conn, tokens, scanned_at)
    log.info("poll: %d tokens | inserted: %d", len(tokens), inserted)


def backfill_outcomes(conn):
    pending = db.fetch_pending_outcomes(conn)
    if not pending:
        return 0

    filled = 0
    for row in pending:
        price = api.fetch_current_price(row["chain"], row["pair_address"])
        if price is None:
            continue
        db.update_outcome(conn, row["id"], row["scanned_at"], price)
        filled += 1

    if filled:
        log.info("outcomes: filled %d / %d pending", filled, len(pending))
    return filled


def main():
    log.info("dex-collector starting (poll interval: %ds)", POLL_INTERVAL)
    conn = db.connect()

    while True:
        start = time.monotonic()
        try:
            poll(conn)
        except Exception as e:
            log.error("poll error: %s", e, exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass

        try:
            backfill_outcomes(conn)
        except Exception as e:
            log.error("backfill error: %s", e, exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass

        elapsed = time.monotonic() - start
        sleep_for = max(0, POLL_INTERVAL - elapsed)
        log.debug("cycle done in %.1fs, sleeping %.1fs", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()

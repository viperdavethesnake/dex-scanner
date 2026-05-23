"""
dex-collector — continuous DexScreener poller and outcome tracker.

Collects every Base/Solana token DexScreener surfaces, computes the same
signals as the scanner, records the filter decision (pass or fail + reason),
and backfills 5-minute price outcomes for all tokens.

No GPU required. Runs independently of n8n and llamacpp.
"""
import hashlib
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

# Birdeye enrichment — Base-only, feature-flagged off by default
BIRDEYE_API_KEY       = os.environ.get("BIRDEYE_API_KEY", "")
BIRDEYE_ENRICHMENT    = os.environ.get("COLLECTOR_BIRDEYE_ENRICHMENT", "false").lower() == "true"
BIRDEYE_SAMPLE_RATE   = float(os.environ.get("COLLECTOR_BIRDEYE_SAMPLE_RATE", "0.02"))
BIRDEYE_MAX_PER_CYCLE = int(os.environ.get("COLLECTOR_BIRDEYE_MAX_PER_CYCLE", "20"))


def _should_sample(token_address: str, scanned_at: datetime, rate: float) -> bool:
    """
    Deterministic per-token sampling within a 5-minute window.
    Same token always lands on the same side of the boundary within a cycle,
    preventing double-enrichment with the n8n scanner enricher.
    """
    window = scanned_at.replace(second=0, microsecond=0)
    window = window.replace(minute=(window.minute // 5) * 5)
    key = f"{token_address}:{window.isoformat()}"
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return (h % 10000) < int(rate * 10000)


def birdeye_enrich(t, conn, cycle_state: dict, scanned_at: datetime) -> None:
    """
    Enrich a Token with Birdeye data in-place. Never raises.

    cycle_state keys mutated: count, cap_hit, success, fail,
                              skipped_solana, skipped_sample
    """
    if not BIRDEYE_ENRICHMENT:
        return

    if t.chain != "base":
        cycle_state["skipped_solana"] += 1
        return

    if not _should_sample(t.token_address, scanned_at, BIRDEYE_SAMPLE_RATE):
        cycle_state["skipped_sample"] += 1
        return

    if cycle_state["count"] >= BIRDEYE_MAX_PER_CYCLE:
        cycle_state["cap_hit"] += 1
        return

    cycle_state["count"] += 1
    result = api.fetch_birdeye_overview(t.token_address, BIRDEYE_API_KEY)

    if result["http_status"] == 200 and result["error_message"] is None:
        t.unique_traders_1h = result["unique_traders_1h"]
        t.net_inflow_usd    = result["net_inflow_usd"]
        cycle_state["success"] += 1
    else:
        cycle_state["fail"] += 1
        log.debug(
            "birdeye: %s http=%s err=%s",
            t.token_address[:8], result["http_status"], result["error_message"],
        )

    t.birdeye_enriched = True

    db.log_birdeye_call(
        conn,
        called_at     = scanned_at,
        chain         = "base",
        address       = t.token_address,
        http_status   = result["http_status"],
        cu_consumed   = result["cu_consumed"],
        response_ms   = result["response_ms"],
        error_message = result["error_message"],
    )


def poll(conn):
    scanned_at = datetime.now(tz=timezone.utc)
    cycle_state = {
        "count":           0,   # Birdeye calls attempted
        "cap_hit":         0,   # skipped: per-cycle cap reached
        "success":         0,   # HTTP 200 + parsed OK
        "fail":            0,   # non-200 or parse error
        "skipped_solana":  0,   # Solana tokens bypassed
        "skipped_sample":  0,   # not sampled this cycle
    }

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
        birdeye_enrich(t, conn, cycle_state, scanned_at)
        tokens.append(t)

    inserted = db.bulk_insert(conn, tokens, scanned_at)
    log.info("poll: %d tokens | inserted: %d", len(tokens), inserted)

    if BIRDEYE_ENRICHMENT:
        log.info(
            "birdeye: cycle done | sampled=%d skipped_solana=%d skipped_sample=%d "
            "cap_hit=%d success=%d fail=%d",
            cycle_state["count"],
            cycle_state["skipped_solana"],
            cycle_state["skipped_sample"],
            cycle_state["cap_hit"],
            cycle_state["success"],
            cycle_state["fail"],
        )


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
    if BIRDEYE_ENRICHMENT:
        log.info(
            "birdeye enrichment: ENABLED | sample_rate=%.3f max_per_cycle=%d",
            BIRDEYE_SAMPLE_RATE, BIRDEYE_MAX_PER_CYCLE,
        )
    else:
        log.info("birdeye enrichment: disabled (COLLECTOR_BIRDEYE_ENRICHMENT=false)")
    conn = db.connect()
    db.migrate(conn)

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

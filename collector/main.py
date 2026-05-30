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
from pathlib import Path

import api
import db
import goplus
import signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))

# GoPlus enrichment — both chains, 100% sample (free, no key required)
GOPLUS_API_KEY       = os.environ.get("GOPLUS_API_KEY", "")
GOPLUS_ENRICHMENT    = os.environ.get("COLLECTOR_GOPLUS_ENRICHMENT", "false").lower() == "true"
GOPLUS_SAMPLE_RATE   = float(os.environ.get("COLLECTOR_GOPLUS_SAMPLE_RATE", "1.0"))
GOPLUS_MAX_PER_CYCLE = int(os.environ.get("COLLECTOR_GOPLUS_MAX_PER_CYCLE", "30"))

# Birdeye enrichment — per-chain sample rates
# Solana defaults to 0: /defi/token_overview returns CU-limit-exceeded on the
# current plan tier for Solana. Set COLLECTOR_BIRDEYE_SAMPLE_RATE_SOLANA > 0
# only after verifying Solana coverage on the active Birdeye plan.
BIRDEYE_API_KEY              = os.environ.get("BIRDEYE_API_KEY", "")
BIRDEYE_ENRICHMENT           = os.environ.get("COLLECTOR_BIRDEYE_ENRICHMENT", "false").lower() == "true"
BIRDEYE_SAMPLE_RATE_BASE     = float(os.environ.get("COLLECTOR_BIRDEYE_SAMPLE_RATE", "1.0"))
BIRDEYE_SAMPLE_RATE_SOLANA   = float(os.environ.get("COLLECTOR_BIRDEYE_SAMPLE_RATE_SOLANA", "0.0"))
BIRDEYE_MAX_PER_CYCLE        = int(os.environ.get("COLLECTOR_BIRDEYE_MAX_PER_CYCLE", "30"))


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


def goplus_enrich(t, conn, cycle_state: dict, scanned_at: datetime) -> None:
    """Enrich a Token with GoPlus holder/security data in-place. Never raises."""
    if not GOPLUS_ENRICHMENT:
        return

    if not _should_sample(t.token_address, scanned_at, GOPLUS_SAMPLE_RATE):
        cycle_state["gp_skipped_sample"] += 1
        return

    if cycle_state["gp_count"] >= GOPLUS_MAX_PER_CYCLE:
        cycle_state["gp_cap_hit"] += 1
        return

    cycle_state["gp_count"] += 1
    result = goplus.fetch_goplus_security(t.token_address, t.chain, GOPLUS_API_KEY)

    if result["http_status"] == 200 and result["error_message"] is None:
        t.goplus_found_in_db         = result["found_in_db"]
        t.top1_pct                   = result["top1_pct"]
        t.top5_pct                   = result["top5_pct"]
        t.top10_pct                  = result["top10_pct"]
        t.holder_count_gp            = result["holder_count"]
        t.creator_pct                = result["creator_pct"]
        t.creator_balance            = result["creator_balance"]
        t.lp_holder_count            = result["lp_holder_count"]
        t.lp_locked_pct              = result["lp_locked_pct"]
        t.buy_tax                    = result["buy_tax"]
        t.sell_tax                   = result["sell_tax"]
        t.is_honeypot_gp             = result["is_honeypot"]
        t.is_blacklisted             = result["is_blacklisted"]
        t.is_mintable                = result["is_mintable"]
        t.hidden_owner               = result["hidden_owner"]
        t.can_take_back_ownership    = result["can_take_back_ownership"]
        t.owner_change_balance       = result["owner_change_balance"]
        t.honeypot_with_same_creator = result["honeypot_with_same_creator"]
        t.is_proxy                   = result["is_proxy"]
        t.is_open_source             = result["is_open_source"]
        t.transfer_pausable          = result["transfer_pausable"]
        t.trading_cooldown           = result["trading_cooldown"]
        t.anti_whale_modifiable      = result["anti_whale_modifiable"]
        t.slippage_modifiable        = result["slippage_modifiable"]
        cycle_state["gp_success"] += 1
    else:
        cycle_state["gp_fail"] += 1
        log.debug(
            "goplus: %s http=%s err=%s",
            t.token_address[:8], result["http_status"], result["error_message"],
        )

    t.goplus_enriched = True

    db.log_goplus_call(
        conn,
        called_at     = scanned_at,
        chain         = t.chain,
        address       = t.token_address,
        http_status   = result["http_status"],
        found_in_db   = result["found_in_db"],
        response_ms   = result["response_ms"],
        error_message = result["error_message"],
    )


def birdeye_enrich(t, conn, cycle_state: dict, scanned_at: datetime) -> None:
    """Enrich a Token with Birdeye data in-place. Never raises."""
    if not BIRDEYE_ENRICHMENT:
        return

    rate = BIRDEYE_SAMPLE_RATE_BASE if t.chain == "base" else BIRDEYE_SAMPLE_RATE_SOLANA
    if not _should_sample(t.token_address, scanned_at, rate):
        cycle_state["skipped_sample"] += 1
        return

    if cycle_state["count"] >= BIRDEYE_MAX_PER_CYCLE:
        cycle_state["cap_hit"] += 1
        return

    cycle_state["count"] += 1
    result = api.fetch_birdeye_overview(t.token_address, t.chain, BIRDEYE_API_KEY)

    if result["http_status"] == 200 and result["error_message"] is None:
        t.unique_traders_1h     = result["unique_traders_1h"]
        t.unique_traders_30m    = result["unique_traders_30m"]
        t.unique_traders_24h    = result["unique_traders_24h"]
        t.buy_volume_1h_usd     = result["buy_volume_1h_usd"]
        t.sell_volume_1h_usd    = result["sell_volume_1h_usd"]
        t.net_inflow_usd        = result["net_inflow_usd"]
        t.volume_24h_usd        = result["volume_24h_usd"]
        t.buy_volume_24h_usd    = result["buy_volume_24h_usd"]
        t.sell_volume_24h_usd   = result["sell_volume_24h_usd"]
        t.trade_count_1h        = result["trade_count_1h"]
        t.trade_count_24h       = result["trade_count_24h"]
        t.holder_count_birdeye  = result["holder_count_birdeye"]
        t.market_count          = result["market_count"]
        t.last_trade_unix_ts    = result["last_trade_unix_ts"]
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
        chain         = t.chain,
        address       = t.token_address,
        http_status   = result["http_status"],
        cu_consumed   = result["cu_consumed"],
        response_ms   = result["response_ms"],
        error_message = result["error_message"],
    )


def poll(conn):
    scanned_at = datetime.now(tz=timezone.utc)
    cycle_state = {
        "count":            0,   # Birdeye calls attempted
        "cap_hit":          0,   # skipped: per-cycle cap reached
        "success":          0,   # HTTP 200 + parsed OK
        "fail":             0,   # non-200 or parse error
        "skipped_sample":   0,   # not sampled this cycle
        "gp_count":         0,
        "gp_skipped_sample": 0,
        "gp_cap_hit":       0,
        "gp_success":       0,
        "gp_fail":          0,
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
        goplus_enrich(t, conn, cycle_state, scanned_at)
        tokens.append(t)

    inserted = db.bulk_insert(conn, tokens, scanned_at)
    log.info("poll: %d tokens | inserted: %d", len(tokens), inserted)

    if BIRDEYE_ENRICHMENT:
        log.info(
            "birdeye: cycle done | sampled=%d skipped_sample=%d "
            "cap_hit=%d success=%d fail=%d",
            cycle_state["count"],
            cycle_state["skipped_sample"],
            cycle_state["cap_hit"],
            cycle_state["success"],
            cycle_state["fail"],
        )
    if GOPLUS_ENRICHMENT:
        log.info(
            "goplus: cycle done | sampled=%d skipped=%d cap_hit=%d success=%d fail=%d",
            cycle_state["gp_count"],
            cycle_state["gp_skipped_sample"],
            cycle_state["gp_cap_hit"],
            cycle_state["gp_success"],
            cycle_state["gp_fail"],
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
            "birdeye enrichment: ENABLED | rate_base=%.3f rate_solana=%.3f max_per_cycle=%d",
            BIRDEYE_SAMPLE_RATE_BASE, BIRDEYE_SAMPLE_RATE_SOLANA, BIRDEYE_MAX_PER_CYCLE,
        )
    else:
        log.info("birdeye enrichment: disabled (COLLECTOR_BIRDEYE_ENRICHMENT=false)")
    if GOPLUS_ENRICHMENT:
        log.info(
            "goplus enrichment: ENABLED | sample_rate=%.3f max_per_cycle=%d",
            GOPLUS_SAMPLE_RATE, GOPLUS_MAX_PER_CYCLE,
        )
    else:
        log.info("goplus enrichment: disabled (COLLECTOR_GOPLUS_ENRICHMENT=false)")
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

        Path("/tmp/heartbeat").touch()
        elapsed = time.monotonic() - start
        sleep_for = max(0, POLL_INTERVAL - elapsed)
        log.debug("cycle done in %.1fs, sleeping %.1fs", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()

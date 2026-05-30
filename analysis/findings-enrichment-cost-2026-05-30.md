# Findings: Full Per-Launch Enrichment Cost Estimate
**Date:** 2026-05-30  
**Status:** Decision-ready  
**Depends on:** `data-gap-diagnostic-2026-05-29.md`

---

## Context

The data-gap diagnostic (2026-05-29) identified that `top5_pct`, `creator_pct`,
`lp_locked`, `lp_burned`, `holder_count`, `buy_tax`, `sell_tax` are absent from the
collector schema and not available anywhere in the pipeline. This document estimates
what it would cost to capture those fields for every new token launch from day one.

---

## Volume Baseline

The collector surfaces the same tokens on every 5-minute cycle. Unique token volume
(the actual unit of API cost) is much smaller than signal volume:

| Chain  | Signals/day | **Unique tokens/day** |
|--------|-------------|----------------------|
| Base   | 639         | **24**               |
| Solana | 2,894       | **124**              |
| Total  | 3,533       | **148**              |

All per-token enrichment costs are calculated on 148 unique tokens/day, not 3,533
signals/day. With a token-level cache (first-seen within a 2-hour window), most cycles
incur zero new API calls.

---

## What Each API Delivers

### GoPlus — one call covers all Base security + holder fields

Confirmed from live response probe against Base chain (8453):

| Field | GoPlus key | Type |
|-------|-----------|------|
| `buy_tax` | `buy_tax` | string % |
| `sell_tax` | `sell_tax` | string % |
| `creator_pct` | `creator_percent` | string % |
| `holder_count` | `holder_count` | string int |
| `top1_pct` | `holders[0].percent` | computed |
| `top5_pct` | `sum(holders[0:5].percent)` | computed |
| `lp_locked` | `lp_holder[n].is_locked == 1` | computed bool |
| `is_honeypot` | `is_honeypot` | "0"/"1" |

**The trader's `security.py` already calls this endpoint per trade and discards 90%
of the response.** Only four boolean flags survive into `security_flags`. All numeric
holder and tax values are available now — they are parsed and thrown away.

GoPlus also covers Solana (chain `solana` endpoint) though coverage of new meme coins
is less complete than for Base EVM tokens.

### RugCheck — Solana LP and risk score

Summary endpoint (`/v1/tokens/{address}/report/summary`) returns:

| Field | Key | Type |
|-------|-----|------|
| `lp_locked_pct` | `lpLockedPct` | float % |
| `risk_score` | `score_normalised` | float 0–1 |
| `risks` | `risks[]` | array of {name, score} |

One call per unique Solana token.

### Honeypot.is — Base EVM cross-check

Returns `is_honeypot`, `buy_tax`, `sell_tax` for Base tokens. Redundant with GoPlus
but higher confidence for EVM. Useful as a cross-check; not strictly required if
GoPlus covers the token.

### Birdeye — already coded, already paid

Current collector already calls `/defi/token_overview` for sampled Base tokens.
Returns `uniqueWallet1h` and `net_inflow_usd`. Additional available fields not yet
parsed: `uniqueWallet5m`, `uniqueWallet30m`, buy/sell volume at multiple timeframes.
Birdeye Standard plan (300 req/min) is already active on the `BIRDEYE_API_KEY` in `.env`.

---

## Cost Table

| Data points | Provider | Calls/day | Rate limit | Incremental $/mo |
|-------------|----------|-----------|------------|------------------|
| buy_tax, sell_tax, creator_pct, holder_count, top1_pct, top5_pct, lp_locked, lp_burned | GoPlus (Base) | 24 | ~120/min free | **$0** |
| Same fields for Solana | GoPlus (Solana) | 124 | same | **$0** |
| lp_locked_pct, risk_score | RugCheck | 124 | 15/min free | **$0** |
| is_honeypot cross-check | Honeypot.is | 24 | 50/min free | **$0** |
| unique_traders_1h, net_inflow_usd | Birdeye | 148 | 300/min (paid, active) | **$0** |
| **Total** | | **444/day** | | **$0** |

444 calls/day = 0.3 calls/min combined across all providers.

---

## Rate Limit Headroom

At full enrichment plus peak scanner load (~120 calls/hour per provider):

| Provider | Limit | Enrichment | Scanner | Combined | Headroom |
|----------|-------|------------|---------|----------|----------|
| GoPlus | ~120/min | 0.10/min | 2/min | 2.1/min | 98% |
| RugCheck | 15/min | 0.09/min | 0/min | 0.09/min | 99% |
| Honeypot.is | 50/min | 0.02/min | 2/min | 2.02/min | 96% |
| Birdeye | 300/min | 0.10/min | 0/min | 0.10/min | 99% |

No rate-limit risk at any realistic volume.

---

## Latency Impact

Per-token API response times (measured):

| Provider | Avg response |
|----------|-------------|
| GoPlus | ~250ms |
| RugCheck | ~470ms |
| Honeypot.is | ~280ms |
| Birdeye | ~840ms (bottleneck) |

With a first-seen cache, most cycle iterations hit zero new tokens. For a cycle where
all 10 tokens are new (worst case), parallelising API calls per token reduces total
added latency to the slowest single call (~840ms Birdeye) — well within the 300s poll
interval.

---

## Implementation Scope

**Schema:** 8 new columns on `raw_signals`:
```sql
buy_tax          REAL,
sell_tax         REAL,
creator_pct      REAL,
holder_count     INTEGER,
top1_pct         REAL,
top5_pct         REAL,
lp_locked        BOOLEAN,
lp_locked_pct    REAL       -- Solana: from RugCheck; Base: from GoPlus lp_holder
```

**Collector changes:**
- Add GoPlus call per Base token (first-seen, 2h cache)
- Parse `holders` array to compute `top1_pct`, `top5_pct`
- Parse `lp_holder` array for `lp_locked`
- Add RugCheck call per Solana token (first-seen, 2h cache)
- Optionally add Honeypot.is as Base cross-check
- Raise Birdeye `COLLECTOR_BIRDEYE_SAMPLE_RATE` to `1.0` — capacity allows it

**Estimated dev time:** 1 day.

**Retroactive coverage:** None. These APIs return live data; the 47k existing
collector rows cannot be backfilled.

---

## Decision-Ready Summary

| Dimension | Value |
|-----------|-------|
| Financial cost (incremental) | **$0** |
| API calls/day at full coverage | **444** |
| Rate limit risk | **None** (<2% of any provider) |
| Dev effort | **~1 day** |
| Retroactive backfill | **Not possible** |
| Shortest path | Extend existing GoPlus call (already in `security.py`), store numeric fields |
| Data available from day 1 | buy_tax, sell_tax, creator_pct, top1/5_pct, lp_locked, holder_count, lp_locked_pct (Solana) |

The binding constraint is not money or API capacity — it is the one dev day needed
to extend the collector's GoPlus/RugCheck calls and add the schema columns.

# Decision: Collector Enrichment Architecture
**Date:** 2026-05-30  
**Status:** Implemented  
**Commits:** `42e0517` (Birdeye Phase 1), `eafdaac` (GoPlus Phase 2)

---

## Context

The data-gap diagnostic (2026-05-29) found that the collector's `raw_signals` table
lacked all holder concentration, security, and richer market data fields needed for
rug fingerprinting and filter calibration. The specific motivation: PIRATES (-89%)
and SPECULATE (-57%) were the two worst trader losses. Post-hoc analysis showed
neither token was distinguishable from winners using any feature currently in the
schema at entry time. Holder concentration (creator_pct, top5_pct, lp_locked) is the
canonical predictor for this rug class — but that data was never collected.

---

## Decisions Made

### 1. Birdeye: capture full token_overview, both chains, 100% Base sample

**What changed:** `fetch_birdeye_overview` now accepts a `chain` parameter and parses
12 additional fields from the same API call that was already being made:

| New field | Birdeye key |
|-----------|-------------|
| `unique_traders_30m` | `uniqueWallet30m` |
| `unique_traders_24h` | `uniqueWallet24h` |
| `buy_volume_1h_usd` | `vBuy1hUSD` |
| `sell_volume_1h_usd` | `vSell1hUSD` |
| `volume_24h_usd` | `v24hUSD` |
| `buy_volume_24h_usd` | `vBuy24hUSD` |
| `sell_volume_24h_usd` | `vSell24hUSD` |
| `trade_count_1h` | `trade1h` |
| `trade_count_24h` | `trade24h` |
| `holder_count_birdeye` | `holder` |
| `market_count` | `numberMarkets` |
| `last_trade_unix_ts` | `lastTradeUnixTime` |

Sample rate raised from 2% to 100% for Base. Storage and API cost both negligible.

### 2. Birdeye Solana: disabled (plan tier finding)

**Finding:** `/defi/token_overview` returns HTTP 400 `"Compute units usage limit exceeded"`
for every Solana token, including at 10% sample rate (1 call per cycle). The Base call
in the same cycle succeeds. This is not a req/min rate limit issue — the `x-ratelimit-limit`
header shows 300 req/min with 299 remaining. It is a separate CU (compute unit) budget
constraint that appears to be plan-tier-gated for Solana.

**Decision:** `COLLECTOR_BIRDEYE_SAMPLE_RATE_SOLANA` defaults to `0.0`. Solana Birdeye
enrichment is disabled until the plan tier is confirmed to cover Solana on this endpoint.
The env var is in `.env.example` for easy re-enablement.

**To re-enable:** Set `COLLECTOR_BIRDEYE_SAMPLE_RATE_SOLANA=0.1` (or higher) after
verifying with Birdeye support that the active plan includes Solana `/defi/token_overview`.
Confirmed working for Base at 100%.

### 3. GoPlus: new collector-side enrichment, both chains, 100% sample, feature-flagged

**What:** New `collector/goplus.py` module. Single call per token to
`/api/v1/token_security/{chain_id}` captures 25 structured fields:

**Holder concentration:**
- `top1_pct`, `top5_pct`, `top10_pct` — computed from `holders[]` array
- `holder_count_gp`
- `creator_pct`, `creator_balance`
- `lp_holder_count`, `lp_locked_pct` — computed from `lp_holders[]` array

**Tax rates:**
- `buy_tax`, `sell_tax`

**Security flags (0/1/NULL):**
- `is_honeypot_gp`, `is_blacklisted`, `is_mintable`, `hidden_owner`
- `can_take_back_ownership`, `owner_change_balance`, `honeypot_with_same_creator`
- `is_proxy`, `is_open_source`, `transfer_pausable`, `trading_cooldown`
- `anti_whale_modifiable`, `slippage_modifiable`

**Why GoPlus and not Honeypot.is for this data:** GoPlus returns holder concentration,
creator data, LP info, and 13 security flags in a single call. Honeypot.is only covers
`is_honeypot`, `buy_tax`, `sell_tax` for Base. RugCheck (Solana) covers LP and risk
scoring but not holder concentration. GoPlus is the most complete single source for
what we need on both chains.

**API cost:** Free tier, no key required. `~300 calls/day` vs `~1,800/min` free-tier
limit. `goplus_calls` hypertable audits every call.

**Feature flag:** `COLLECTOR_GOPLUS_ENRICHMENT=false` by default in `.env.example`.
Flip to `true` when ready. Verified in production: first cycle after enabling showed
`sampled=13 success=13 fail=0` on both chains.

### 4. GoPlus `found_in_db=False` is expected for brand-new tokens

GoPlus indexes tokens after they appear on-chain and accumulate some activity. Very
new launches (age < 30 min) may not yet be in the GoPlus DB. `found_in_db=False` is
not an API error — it is stored as `goplus_found_in_db=false` and all numeric fields
stay NULL. This is the correct representation.

---

## What This Enables

Once 2–4 weeks of enriched data accumulates:

1. **Rug fingerprinting:** correlate `top5_pct`, `creator_pct`, `lp_locked_pct` with
   `outcome_pct < -30%` to find distinguishing pre-entry features for rug-class tokens.

2. **Pre-entry filters:** if e.g. `creator_pct > 20%` or `lp_locked_pct = 0` predicts
   rugs with high precision, add as a hard filter in `signals.py`.

3. **Model retraining:** these fields become candidate features for the next scorer
   version — the first one trained with holder data rather than pure price/volume signals.

4. **Backfill is impossible:** APIs return live data. The 47k existing rows will never
   have GoPlus or expanded Birdeye fields. Historical analysis is limited to
   DexScreener-sourced fields.

---

## Schema State After This Decision

`raw_signals` now has 71 columns (was 34). The 37 new columns are all nullable and
have zero impact on existing queries.

Key new column groups:
- Birdeye Phase 1: 12 columns (multi-window wallet counts, volume, trade counts)
- GoPlus Phase 2: 25 columns + `goplus_enriched` + `goplus_found_in_db`

New audit tables: `birdeye_calls`, `goplus_calls` (both hypertables).

---

## Env Vars Added

| Variable | Default | Purpose |
|----------|---------|---------|
| `COLLECTOR_BIRDEYE_SAMPLE_RATE` | `1.0` | Base Birdeye sample rate |
| `COLLECTOR_BIRDEYE_SAMPLE_RATE_SOLANA` | `0.0` | Solana Birdeye (disabled, CU limit) |
| `COLLECTOR_BIRDEYE_MAX_PER_CYCLE` | `30` | Birdeye per-cycle cap |
| `COLLECTOR_GOPLUS_ENRICHMENT` | `false` | GoPlus feature flag |
| `COLLECTOR_GOPLUS_SAMPLE_RATE` | `1.0` | GoPlus sample rate |
| `COLLECTOR_GOPLUS_MAX_PER_CYCLE` | `30` | GoPlus per-cycle cap |
| `GOPLUS_API_KEY` | _(empty)_ | Optional; raises free-tier limit |

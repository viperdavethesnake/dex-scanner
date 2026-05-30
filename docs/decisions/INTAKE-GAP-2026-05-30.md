# Decision: Intake Feed Expansion (Intake Gap Diagnostic)
**Date:** 2026-05-30  
**Status:** Decided  
**Evidence:** `analysis/intake-gap-diagnostic-2026-05-30.md`

---

## Context

The data collector polls DexScreener `/token-profiles/latest/v1` every 5 minutes
to capture new Base and Solana token launches. The intake-gap diagnostic (2026-05-30)
measured the actual capture rate against on-chain ground truth and found:

- **Base: ~5–12% coverage.** On-chain Base factories create ~40 new pairs/hour;
  the collector captures ~2/hour.
- **Solana: ~4–11% coverage.** Birdeye `/defi/v2/tokens/new_listing` surfaces
  ~324 Solana tokens/hour with DEX liquidity; the collector captures ~14/hour.
- **Survivorship bias is real.** DexScreener profiles require explicit submission.
  Tokens that rug in under ~30 minutes never appear. The ML training corpus is
  definitionally biased toward tokens that survived long enough to get attention.

---

## Decisions Made

### 1. Add `token_profiles_updates` to the collector poller (Phase 1)

**Endpoint:** `https://api.dexscreener.com/token-profiles/recent-updates/v1`

**Evidence:**
- 60-min sampler: 172 unique tokens (160 Base, 12 Solana) vs 37 from `token_profiles`
  (1 Base, 36 Solana). Base improvement: **160x**.
- Continuous growth: +83 new Base tokens in the second 30 minutes — not a one-time
  backlog flush. Rate ≈ 160 unique Base tokens/hour sustained.
- Token freshness: `updatedAt` timestamps within 30–60 seconds of query time.

**Implementation:** Union `token_profiles_updates/recent-updates/v1` with
`token_profiles/latest/v1` in `collector/api.py`. Dedup on `(chainId, tokenAddress)`.
No new API key, no rate-limit risk (well within 60 rpm per endpoint).

**Cost:** $0.

**Expected impact:** Base collector rate: 2/hr → ~160/hr. Coverage of on-chain
Uniswap V2 launches: ~12% → ~65%+.

---

### 2. Add Birdeye `/defi/v2/tokens/new_listing` as a second ingest feed (Phase 2)

**Endpoint:** `https://public-api.birdeye.so/defi/v2/tokens/new_listing`

**Evidence:**
- 5-min test: Base ~144/hr with DEX liquidity, Solana ~324/hr with DEX liquidity.
- Tokens appear seconds after `liquidityAddedAt` — much earlier than DexScreener profiles.
- Base tokens frequently have `liquidity: 0` (pre-trading); Solana tokens arrive with
  real liquidity (meteora, raydium sources).

**Implementation notes:**
- `limit` max is 20 per call. Poll both `base` and `solana` chains each cycle.
- Filter: skip pair enrichment for tokens with `liquidity: 0` until a DEX pair exists.
- Dedup against DexScreener results on token address.
- No new API key needed (uses existing `BIRDEYE_API_KEY`).

**Cost:** Within existing Birdeye Standard plan (300 req/min; 4 calls/5min = 48 calls/hr).

**Expected impact:** Reduces survivorship bias — captures tokens before DexScreener
profiles exist, including tokens that die in under 30 minutes.

---

### 3. Endpoint classification (what NOT to add)

| Endpoint | Decision | Reason |
|----------|----------|--------|
| `search?q=base` | ❌ Do not add | Name search (returns tokens called "BASE"), not chain filter |
| `search?q=solana` | ❌ Do not add | Name search (returns tokens called "SOL"), not chain filter |
| `token_boosts/latest`, `token_boosts/top` | ❌ Do not add | Paid promotional tokens — established tokens, not new launches |
| `community_takeovers` | ❌ Do not add | Historical relaunches, not fresh token launches |
| `metas/trending` | ❌ Do not add | Category metadata — returns 0 Base/Solana token addresses |
| `latest/dex/pairs/base`, `/pairs/solana` | ❌ Do not add | 404 — these endpoints don't exist |

---

### 4. Direct on-chain monitoring (Phase 3 — deferred)

On-chain monitoring (Alchemy WebSocket for Base, Helius webhooks for Solana)
would provide 100% coverage with zero survivorship bias. This is the gold
standard but requires:
- Alchemy paid tier (free tier limits `eth_getLogs` to 10 blocks).
- Helius API key (not currently in use).
- Estimated dev work: 2–3 weeks.
- Estimated cost: $200–400/month.

**Decision:** Defer until Phase 1+2 are deployed and the residual gap is measured.
If DexScreener + Birdeye combined covers >80% of on-chain launches, Phase 3 is
not justified. If a material gap remains (particularly for Base memecoins that
never get a Birdeye listing), revisit.

**Public Base RPC note:** `https://mainnet.base.org` supports `eth_getLogs` over
large block ranges at no cost and was used for all on-chain measurements in this
diagnostic. It is a viable fallback for one-off analysis but not reliable enough
for production polling.

---

## Why This Matters for ML

The current model was trained on tokens that appeared in DexScreener profiles.
That corpus is biased toward "tokens that survived long enough to get attention."
Sub-30-minute rugs — the pattern we most want to detect for risk management — are
absent from training data.

Phase 1+2 will expose the collector to a broader risk distribution: tokens with
shorter survival times, lower initial liquidity, and less community backing. The
model trained on this expanded corpus should have better precision against immediate
rugs.

**Expected training corpus growth:** From ~170 unique tokens/day to ~1,000–2,000/day
after Phase 1+2. Retraining frequency and minimum corpus age thresholds should be
revisited once the new feeds are live for ≥7 days.

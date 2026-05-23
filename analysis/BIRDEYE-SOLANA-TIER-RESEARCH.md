# Birdeye Solana Tier Research
**Date:** 2026-05-23  
**Status:** Read-only research — no purchases, no code changes  
**Purpose:** Determine whether/when to pay for Solana Birdeye access; provide data for the tier decision.

---

## Section 1 — Endpoint Eligibility by Tier

### `/defi/token_overview` — confirmed behaviour (2026-05-23)

| Chain | Standard (Free) | Lite ($39) | Starter ($99) | Premium ($199) | Business ($499+) |
|---|---|---|---|---|---|
| Base | ✅ HTTP 200 | ✅ | ✅ | ✅ | ✅ |
| Solana | ❌ HTTP 400 `"Compute units usage limit exceeded"` | ✅ (most APIs) | ✅ | ✅ | ✅ |

**Interpretation of the Solana 400:** The error message says "Compute units usage limit exceeded," not "endpoint not accessible" or "unauthorized." This is consistent with two possible causes:

1. **Monthly CU budget exhausted** — Standard has 30,000 CU/month. At 25 CU/call, that's only 1,200 calls. The n8n enricher has been running for weeks making Base calls; it's possible the monthly budget is already spent.

2. **Solana data priced at a higher tier** — Birdeye's access matrix notes "Standard: Limited" access, and Solana multi-chain data is typically gated. Base is Coinbase-native and may receive preferential Standard access.

**We cannot definitively distinguish (1) from (2) without either:** resetting the month's CU counter or upgrading to Lite and testing. The conservative assumption is (2) — Solana token_overview requires a paid tier — because Base continues to return 200 even as Solana consistently returns 400 on the same key.

**Floor:** Lite ($39/month) appears to be the minimum tier required for Solana `/defi/token_overview`.

---

## Section 2 — CU Cost Model

### `/defi/token_overview` cost

| Endpoint | CU per call |
|---|---|
| `/defi/token_overview` | **25 CU** |
| `/defi/price` (single token) | 3 CU |
| `/defi/token_overview` (if alternative exists) | — |

**Alternative endpoints for the same two fields (`uniqueWallet1h` + net flow):**

No cheaper alternative provides both `uniqueWallet1h` and volume buy/sell breakdown in a single call. Closest options:
- `/defi/price` (3 CU) — price only, no trader count or volume split
- `/defi/token_security` — security flags, not trading stats
- `/defi/txs/token` (dynamic CU, expensive) — raw transactions, requires client-side aggregation

**Conclusion:** `token_overview` at 25 CU/call is the correct and only practical endpoint for these two features.

### Base vs Solana CU comparison

Both chains use the same endpoint and the same 25 CU cost per call. The difference is tier access, not per-call cost.

---

## Section 3 — Volume Projection

### Observed collector volume (7-day, 2026-05-16 to 2026-05-23)

| Chain | Unique tokens | Total rows | Rows/day | Rows/5-min cycle |
|---|---|---|---|---|
| Base | 268 | 5,452 | 778.9 | 2.70 |
| Solana | 774 | 14,800 | 2,114.3 | 7.34 |

### Projected CU consumption at Task A defaults (`SAMPLE_RATE=0.2`, `MAX_PER_CYCLE=20`)

| Scenario | Calls/cycle | Calls/day | Calls/month | CU/month |
|---|---|---|---|---|
| Base only, rate=0.2 | 0.54 | 156 | 4,680 | **117,000** |
| Solana only, rate=0.2 | 1.47 | 422 | 12,660 | **316,500** |
| Base+Solana, rate=0.2 | 2.01 | 578 | 17,340 | **433,500** |

### Projected CU consumption at conservative start (`SAMPLE_RATE=0.05`)

| Scenario | Calls/cycle | Calls/day | Calls/month | CU/month |
|---|---|---|---|---|
| Base only, rate=0.05 | 0.14 | 39 | 1,170 | **29,250** |
| Base+Solana, rate=0.05 | 0.50 | 145 | 4,350 | **108,750** |

### n8n Birdeye Enricher (existing, not changing)

The n8n enricher runs on INTERESTING/WATCH tokens from manual scanner sessions only — estimated 5–30 calls/day depending on scan frequency. At 25 CU/call, ~750 CU/day worst case. This is already included in the Standard free tier usage and does not materially affect projections.

---

## Section 4 — Recommendation Matrix

Assumptions: conservative sampling (`SAMPLE_RATE=0.05`), Base-only first, Solana after tier upgrade.

| Tier | Monthly cost | Included CUs | Base-only burn (rate=0.05) | Base+Solana burn (rate=0.05) | Base+Solana burn (rate=0.20) | Notes |
|---|---|---|---|---|---|---|
| Standard | $0 | 30,000 | **29,250** ✅ (barely) | 108,750 ❌ | 433,500 ❌ | Base-only at 0.05 fits; no Solana; razor-thin margin |
| Lite | $39 | 1,500,000 | 29,250 ✅ | 108,750 ✅ | 433,500 ✅ | Comfortable at any realistic sample rate |
| Starter | $99 | 5,000,000 | ✅ | ✅ | ✅ | Overkill for current volume |
| Premium+ | $199+ | 15M+ | ✅ | ✅ | ✅ | No case for this at current scale |

### Key thresholds

- **Standard + rate=0.05 (Base only):** Just fits. 29,250 CU projected vs 30,000 included. One bad month (more scanner sessions, higher DexScreener activity) overruns. No margin.
- **Lite + rate=0.2 (Base+Solana):** 433,500 CU vs 1,500,000 included. 3.5× headroom. Safe for a year of growth.
- **Upgrade trigger for Lite:** When auto-trading is live on Base and generating revenue, $39/month is trivial. Until then it's a pure research cost.

---

## Section 5 — Decision

**Recommended path: stay on Standard free tier, Base-only enrichment at `SAMPLE_RATE=0.05`, defer Solana.**

Rationale:
1. Standard fits Base-only at the conservative sample rate — just barely, but it fits. This gives real enrichment data for ML training without any cost.
2. Solana enrichment at Standard is blocked (likely tier-gated). Paying $39/month purely to enrich a dataset that isn't yet being traded on is premature. The ML model for Solana already underperforms Base (0.579 vs 0.661 AUC) and the auto-trader will launch on Base first.
3. The right trigger for upgrading to Lite is: **Base auto-trading is live and profitable.** At that point the research cost of $39/month is covered by trading returns and Solana enrichment becomes useful for model training ahead of a Solana trader.
4. The `COLLECTOR_BIRDEYE_ENRICHMENT=false` feature flag means enrichment can be turned on immediately after tier upgrade without a code deploy.

**Contingency:** If the Standard tier turns out to be the CU exhaustion issue (not an access gate), run a quick test at the start of next month by setting `ENRICHMENT=true`, `SAMPLE_RATE=0.05`, and watching `birdeye_calls` for a single day. If Solana returns 200, we get it for free.

**Do not upgrade until:** Base auto-trading is generating profit OR the ML model reaches the 0.62 AUC gate in ROADMAP.md Phase 1 and Solana training data becomes the next bottleneck.

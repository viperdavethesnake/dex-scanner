# Decision: Filter Audit Findings — Deferred Actions
**Date:** 2026-05-29  
**Status:** Deferred — corpus enrichment required first  
**Data:** 47,627 collector rows with 5-min outcomes (Base n=10,362, Solana n=37,265)

---

## Findings Summary

Five findings from the filter threshold sweep across collector data. Two were acted
on immediately; three are deferred pending a richer corpus.

---

### Finding 1 — sell_pressure_5m cliff at 0.70 (ACTED ON: 2026-05-29)

**Data:** Base outcomes by sells_5m / (buys_5m + sells_5m):

| bucket | n | avg_outcome | pct_positive |
|--------|---|-------------|-------------|
| 0.0–0.5 | ~7,900 | +3–6% | 53–55% |
| 0.5–0.7 | ~2,300 | +2% | 51% |
| **0.7–0.8** | **517** | **-0.89%** | **41.6%** |
| **0.8+** | **819** | **-0.21%** | **33.5%** |

The cliff is real. Outcomes turn negative above 0.70 and pct_positive falls to
33–42%. A single evidence point (BASED rug, sell_pressure=0.77) motivated the
initial filter; the 10k-row audit confirmed it.

**Action taken:** `SELL_PRESSURE_MAX_5M=0.70` added to `dex-trader/signals.py`
hard_filter (commit `19e7984`). Filter active in production.

**Solana note:** No equivalent cliff on Solana — the 0.8+ bucket is slightly better
than 0.7–0.8. Solana sell_pressure doesn't carry the same signal.

---

### Finding 2 — buy_pct_1h sweet spot 55–60% on Base (DEFERRED)

**Data:**

| bucket | n | avg_outcome | pct_positive |
|--------|---|-------------|-------------|
| 0–40 | 3,030 | +0.28% | 38.1% |
| 40–50 | 1,078 | +1.27% | 48.0% |
| 50–55 | 1,406 | +3.68% | 44.2% |
| **55–60** | **2,379** | **+6.17%** | **51.5%** |
| 60–65 | 1,003 | +3.73% | 48.9% |
| 65–70 | 419 | +0.75% | 43.0% |
| 70+ | 597 | +0.78% | 37.9% |

Non-monotonic: peak at 55–60%, then degrades on both sides. 70%+ is as bad as 0–40%.
This is a sharper signal than buy_pct_5m at the same granularity.

**Why deferred:**
- `buy_pct_1h` and `buy_pct_5m` are correlated. Adding a 1h filter without
  understanding the interaction risks removing a signal the scorer already captures.
- The bucket with best outcomes (55–60%) has n=2,379 which is large enough to be
  real, but the tails (0–40% and 70%+) overlap in absolute outcome — the degradation
  is modest (+0.28% vs +6.17%, not a cliff).
- Needs validation against the GoPlus-enriched population: does the 55–60% signal
  hold when conditioned on lp_locked=True? Is the degradation at 70%+ caused
  disproportionately by rug-class tokens (which GoPlus can now flag)?

**Action when ready:** If the 55–60% peak persists after conditioning on
lp_locked/creator_pct, add a buy_pct_1h range filter: reject < 40% or > 70%.

---

### Finding 3 — market_cap sweet spot 50k–500k on Base (DEFERRED)

**Data:**

| bucket | n | avg_outcome | pct_positive |
|--------|---|-------------|-------------|
| <10k | 308 | +2.11% | 28.2% |
| 10–50k | 2,816 | +1.80% | 26.6% |
| **50–100k** | **1,684** | **+6.50%** | **51.8%** |
| **100–500k** | **4,640** | **+2.38%** | **50.8%** |
| 500k–1M | 493 | -2.60% | 41.0% |
| 1M–5M | 254 | +0.96% | 44.5% |
| 5M+ | 165 | -0.40% | 23.6% |

Strong finding: 50–500k market cap is the sweet spot. Below 10k and 10–50k: avg
looks plausible (+1.8–2.1%) but pct_positive is only 27% — these are lottery tickets
with high upside variance, not reliable momentum signals. Above 500k: outcomes
deteriorate sharply.

**Why deferred:**
- The low pct_positive in the <50k brackets (26–28%) may be partly explained by
  rug-class tokens that the current schema can't identify. Once GoPlus enrichment
  accumulates, we can test: is the low win rate in <50k explained by high creator_pct
  or lp_locked=False? If yes, GoPlus filtering handles it more precisely than a
  blanket market_cap floor.
- Adding a market_cap floor (e.g., reject <50k) before that test risks discarding
  legitimate micro-cap momentum that GoPlus would pass.
- Also: market_cap is a DexScreener-reported field and can be manipulated at launch.
  A token with spoofed liquidity can show any market_cap. The signal may be partially
  a proxy for liquidity quality rather than market_cap itself.

**Action when ready:** After 4 weeks of GoPlus data, segment the <50k bucket by
lp_locked_pct and creator_pct. If rugs cluster there and non-rugs don't, keep the
<50k tokens with GoPlus passes rather than blanket-filtering on market_cap.

---

### Finding 4 — liq_mcap_ratio ≥ 0.5 best on Base (DEFERRED)

**Data:**

| bucket | n | avg_outcome | pct_positive |
|--------|---|-------------|-------------|
| <0.01 | 55 | 0.00% | 0.0% |
| 0.01–0.05 | 237 | +1.13% | 26.2% |
| 0.05–0.25 | 592 | -0.36% | 40.4% |
| 0.25–0.5 | 2,314 | +0.39% | 45.1% |
| **0.5+** | **7,162** | **+3.55%** | **42.8%** |

High liq/mcap ratio = deeper order books relative to market size = harder to rug,
lower slippage, more reliable price discovery. The 0.5+ bucket has the best avg
outcome (+3.55%) by a clear margin.

**Solana note:** Inverted on Solana — 0.5+ has the worst pct_positive (34.3%).
Chain-specific; any filter must be Base-only.

**Why deferred:** Same reasoning as market_cap — liq_mcap is correlated with whether
a token is a rug. Rather than filtering on the derived ratio, wait for GoPlus data
to test whether high liq/mcap tokens with good GoPlus scores have clean outcomes, or
whether the ratio is just a proxy for lp_locked_pct.

---

### Finding 5 — sell_pressure on Solana (NO ACTION, NOTED)

Solana shows no sell_pressure cliff. The 0.8+ bucket performs slightly better than
0.7–0.8. The sell_pressure filter at 0.70 was implemented for Base only (correctly).
No Solana-side action warranted from this data.

---

## Decision Summary

| Finding | Action | Condition to act |
|---------|--------|-----------------|
| sell_pressure cliff at 0.70 (Base) | **Implemented** | — |
| buy_pct_1h 55–60% sweet spot | Deferred | Validate against GoPlus-enriched data |
| market_cap 50k–500k sweet spot | Deferred | Test if <50k rugs cluster by GoPlus flags |
| liq_mcap_ratio ≥0.5 best | Deferred | Test if signal is proxy for lp_locked |
| sell_pressure on Solana | No action | No cliff found |

**Common deferral condition:** All three deferred findings require 4+ weeks of
GoPlus-enriched collector data (began collecting 2026-05-30) to distinguish "these
tokens are bad because of structural rug-class features" from "these tokens are bad
for unrelated reasons." Premature filtering on market_cap or liq_mcap could discard
legitimate tokens that GoPlus would pass.

**Revisit date:** ~2026-07-01, once GoPlus has covered ≥2,000 Base tokens.

---

## Data Gaps Noted

- `unique_traders_1h` was 0.17% covered at audit time — not usable
- `top5_pct`, `creator_pct`, `lp_locked` were absent from schema — now being
  collected via GoPlus (2026-05-30 onwards)
- Sections 4.6–4.8 of the original audit (holder concentration, LP state) could
  not be run; will be re-run once 2,000+ GoPlus-enriched rows are available

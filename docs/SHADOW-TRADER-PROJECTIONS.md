# Shadow Trader: Signal Flow & Trade Accumulation Projections

*Created: 2026-05-25 — based on collector DB data through 2026-05-24*

---

## Signal Flow (from collector DB)

### Filter-pass Base signals per day

| Date | Filter-pass signals | Signals/5m cycle |
|------|--------------------:|----------------:|
| 2026-05-22 | 437 | 1.52 |
| 2026-05-23 | 360 | 1.25 |
| 2026-05-24 | 219 | 0.76 |

**Working estimate: 0.76–1.5 filter-pass signals per 5-minute cycle.**

### Micro-trend breakdown (last 3 days, Base)

| Trend | Count | Notes |
|-------|------:|-------|
| `up`    | 443 | Primary entry target |
| `flat`  | 362 | Excluded on Solana; Base ≤4x V/L ceiling |
| `fading`| 269 | Passes filter (Base); excluded: `down`, `recovering` only |

Base filter passes `up`, `flat`, and `fading` micro-trends (with volume ceiling), so the relevant pool is `up + flat + fading` = ~1074 signals over 3 days.

---

## Model Hit Rate

- **Val-set AUC:** 0.620
- **Threshold:** 0.65 (shadow), 0.70 (live)
- **Val-set scoring rate at 0.65:** ~28% (232 predicted-positive out of 828 eligible)

Applied to signal flow: **0.2–0.4 model hits per 5-min cycle → 60–120 scored signals/day.**

---

## Gate Pass-Through Estimate

After model scoring ≥ 0.65, three gates remain:

| Gate | Estimated pass-through | Notes |
|------|----------------------:|-------|
| GoPlus security check | ~85% | Occasional blacklist/honeypot hits |
| 0x quote (liquidityAvailable=true) | ~50–70% | **Unknown — dominant uncertainty.** Thin meme coin pools often return false. First-hit calibrates this. |
| Quote drift ≤ 3% | ~85% | Price moves between signal and quote time |

**Combined gate pass-through: ~40–60%**

---

## Projected Fills per Day

```
0.2–0.4 model hits/cycle × 288 cycles/day = 58–115 scored signals/day
× 40–60% gate pass-through
= 25–70 fills/day
```

Working central estimate: **~40 fills/day** in steady state.

---

## Trade Accumulation Milestones

| Milestone | Low estimate | Central | High estimate |
|-----------|-------------|---------|---------------|
| First hit | 1–4 hours | ~2 hours | within minutes |
| 10 trades | 4–24 hours | ~6 hours | ~2 hours |
| 20 trades | 1–2 days | ~12 hours | ~4 hours |
| 50 trades | 2–5 days | ~1.5 days | ~18 hours |
| 100 trades | 4–10 days | ~3 days | ~1.5 days |

The wide range is almost entirely driven by the **0x `liquidityAvailable` unknown**. First hit calibrates everything else.

---

## Opening Batch Observation

Trader's first ingest batch (signal IDs 28908–28959, 10 Base signals) had **0 pass hard_filter**. This is expected:

- Trader starts at the collector's current watermark — it gets whatever DexScreener returned at that moment
- Early-morning / thin-market batches can have no qualifying momentum
- Normal startup artifact, not a filter bug

---

## Key Uncertainties

1. **0x `liquidityAvailable` rate** — dominant unknown. Many new meme coin pools are too thin for 0x to route. First 10–20 quote attempts will show the true rejection rate.

2. **Quote drift rejection** — tracked as `cost_delta_pct`. If entry fill price has drifted >3% from signal, trade is skipped. Higher in fast markets.

3. **Daily signal volume** — the 219–437 range is volatile. Weekend vs. weekday, market sentiment, and new chain launches all affect throughput.

4. **Model calibration** — val-set hit rate of 28% is training-set calibrated. Real-world precision may differ as the model encounters distribution shift.

---

## Calibration Plan

Once first 10 shadow fills accumulate:

```sql
-- Rejection breakdown by gate
SELECT
  COUNT(*) FILTER (WHERE status='skipped' AND failure_reason='no_quote')                AS quote_rejected,
  COUNT(*) FILTER (WHERE status='skipped' AND failure_reason LIKE 'quote_drift%')       AS drift_rejected,
  COUNT(*) FILTER (WHERE status='skipped' AND failure_reason LIKE 'slippage_too_high%') AS slippage_rejected,
  COUNT(*) FILTER (WHERE status='skipped' AND failure_reason LIKE 'security_fail%')     AS security_rejected,
  COUNT(*) FILTER (WHERE status IN ('simulated','exited'))                              AS filled,
  COUNT(*)                                                                              AS total_post_score
FROM trades
WHERE created_at >= CURRENT_DATE;
```

Use this to tighten or widen the projections. A 30%+ quote rejection rate would shift estimates toward the low end; <10% toward the high end.

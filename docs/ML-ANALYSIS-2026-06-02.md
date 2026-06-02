# ML Analysis — 2026-06-02

Analysis run against collector DB with 64,624 rows (28,881 in the 15–90 minute
scanner window). Base chain only, since the shadow trader is Base-only.

---

## 1. Dataset

| Metric | Value |
|--------|-------|
| Total rows (with 5m outcome) | 28,881 (age 15–90m window) |
| Unique tokens | 2,273 |
| Date range | 2026-05-17 → 2026-06-02 |
| Days of data | 15 |

By chain (age 15–90m window):

| Chain | n | Win rate | Avg outcome |
|-------|---|----------|-------------|
| base | 4,713 | 48.1% | +4.68% |
| solana | 24,168 | 40.7% | -1.08% |

---

## 2. Base win rate is stable — no regime shift

| Week | n | Win rate | Avg outcome |
|------|---|----------|-------------|
| May 11–17 | 49 | 51.0% | +3.81% |
| May 18–24 | 3,012 | 47.6% | +4.99% |
| May 25–31 | 1,312 | 48.0% | +3.59% |
| Jun 1–7 | 340 | 52.1% | +6.30% |

The market did not shift between training and shadow trading periods.
The shadow trader's 32.8% win rate is a model/execution problem, not a market problem.

---

## 3. Outcome distribution (Base)

```
Mean:     +4.68%
Median:   0.00%
Std dev:  28.17%   (extremely fat-tailed)
Win rate: 48.1%

Percentiles (raw, unclipped):
  P1:  -56.3%    P25: -7.3%    P75: +12.6%    P99: +110.5%
  P5:  -32.1%    P50:  0.0%    P90: +35.2%
  P10: -21.9%                  P95: +54.7%

Winners (>0):    n=2,266  mean=+22.7%  median=+13.3%  max=+278.5%
Losers (≤0):     n=2,447  mean=-12.0%  median=-6.9%   min=-99.3%

Big wins (>20%):    17.6% of all tokens
Big losses (<-20%): 11.3% of all tokens
Rugs (<-80%):        0.4% of all tokens
Catastrophic (<-50%): 1.3% of all tokens
```

The distribution is the dominant challenge. Losses are bounded at -100%; wins are
unbounded. The mean is positive (+4.68%) but median is 0% — the mean is driven by
a small number of large winners. Most tokens do nothing or lose small.

---

## 4. Feature correlations with outcome (Base)

Point-biserial correlation with binary win/loss target:

| Feature | Correlation |
|---------|-------------|
| log_volume_5m | +0.250 |
| log_liquidity_usd | +0.146 |
| vol1h_6h_ratio | +0.129 |
| log_market_cap | +0.127 |
| log_volume_1h | +0.125 |
| age_minutes | -0.114 |
| txn_accel | +0.108 |
| sell_pressure_5m | +0.105 |
| liq_mcap_ratio | -0.097 |
| vol5m_1h_ratio | +0.096 |

**Key observations:**

- `log_volume_5m` (r=0.25) is the strongest single predictor. Higher 5m volume
  correlates with winning. This makes sense: tokens with active buying tend to
  continue short-term.
- `age_minutes` (r=-0.11): younger tokens win more. Older tokens in the window
  have often already completed their move.
- All correlations are individually weak (<0.25). No single feature discriminates
  cleanly. LightGBM's value is in combining weak signals.
- These features are all visible on DexScreener and manipulable by deployers.

---

## 5. Walk-forward AUC — the model has real signal

Three train/test splits, Base chain only, token-level deduplication between sets:

| Train window | Val window | Val base rate | AUC | ≥0.55 | ≥0.60 | ≥0.65 | ≥0.70 |
|---|---|---|---|---|---|---|---|
| May 17–21 | May 21–25 | 45.9% | **0.6618** | 57.6% | 58.9% | 59.8% | 62.9% |
| May 17–25 | May 25–Jun 2 | 48.5% | **0.6258** | 56.3% | 58.5% | 60.4% | 66.9% |
| May 21–28 | May 28–Jun 2 | 48.9% | **0.6311** | 58.5% | 58.5% | 62.8% | 63.9% |

Win rate percentages are for the DexScreener 5-minute price metric.

**The model has real predictive signal.** AUC 0.63–0.66 across all windows. At
≥0.70 threshold, precision is 63–67% on held-out data (base rate 46–49%).
That is approximately +15pp lift over random at the highest conviction tier.

---

## 6. Why the shadow trader underperformed despite real model signal

The shadow trader achieved 30.8% win rate while the walk-forward shows 63–67%.
This is a 30+ point gap. The causes, in order of impact:

### 6a. Hard-filter + model: double-momentum selection

`signals.py:hard_filter` removes tokens where:
- micro_trend is "down" or "recovering"
- V/L ratio > 8.0
- sell_pressure_5m > 70%

This pre-selects for tokens that are actively pumping with buy pressure. The model
then also scores those same momentum signals highly. The combined selection
disproportionately picks tokens at or near their momentum peak — the end of the pump
rather than the beginning. The walk-forward has no such pre-filter; it trains and
tests on the full distribution, so the model's lift is measured on the full range
including recovering and down tokens.

**Fix:** Remove micro_trend and sell_pressure exclusions from hard_filter before
model scoring. Let the model decide. Apply hard exclusions only to non-ML safety
gates (age, extreme V/L, liquidity floor).

### 6b. Cost structure: 8.3% real vs 1.5% assumed

The backtest assumed 1.5% round-trip cost (gas + swap fee + slippage). Actual
measured cost for $10 positions on Base AMMs:

| Component | Assumed | Actual |
|-----------|---------|--------|
| Backtest round-trip | 1.5% | — |
| Real total friction | — | ~8.3% |
| Delta | — | +6.8pp |

At $10 position size, gas alone consumes several percent. The AMMs for new tokens
have thin liquidity; even a $10 swap can move price significantly.

Even with a 65% win rate and the measured outcome distribution (+22.7% avg win,
-12.0% avg loss), expected gross per trade ≈ +0.65×22.7 + 0.35×(-12.0) = +10.6%.
After 8.3% cost: +2.3% net. Barely positive and fragile to execution variation.
At 1.5% cost: +9.1% net — a very different business.

**Fix:** Position size of $50–$100 minimum. At $100, gas drops from ~5% to ~0.5%
of trade size. The edge becomes real.

### 6c. Stop-loss blind spot on rug pulls

`_fetch_dexscreener_price` returns None when DexScreener delists a rugged pair.
`drawdown_pct` stays None; stop-loss check is skipped. Position waits 5 minutes
and exits via aggregator at the crashed pool price. Example: OL token, -78% via
timer exit.

The 0.4% rug rate (per collector data) produces extreme left-tail losses that
dominate the mean.

**Fix:** Treat a None DexScreener fetch as "assume stopped — exit immediately via
fallback price." Or use on-chain pool reserves as a backup price source.

---

## 7. Conclusions and direction

### What is true

1. **The model has real signal.** AUC 0.63–0.66, +15pp lift at ≥0.70 threshold.
   This is not noise. It is weak but real.

2. **The features are the right category.** Volume, momentum, transaction counts —
   these correlate with 5-minute price direction. They are manipulable but still
   predictive.

3. **The market is stable.** No regime shift. May and June have the same base rate.
   The model trained on May data should generalize to June data.

4. **Execution is what breaks it.** The gap between backtest (DexScreener prices,
   1.5% cost) and live (aggregator prices, 8.3% real cost, double-momentum filter)
   is the entire problem.

### What to do with more data

The model will improve with more data, but the improvement will be incremental. AUC
0.63 with 4,700 rows is a reasonable ceiling for these features. On-chain features
(deployer history, LP lock status, holder concentration at launch) would be more
informative and harder to fake. DexScreener features will always have a ceiling
because deployers know exactly what to do to look good on DexScreener.

### The scanner use case

The LLM scanner does something the ML model cannot: qualitative pattern matching
on narrative context (token name, socials, behavior patterns). It is complementary
to, not redundant with, the ML layer. For manual scanning, the current tool is
correct and should be used as-is.

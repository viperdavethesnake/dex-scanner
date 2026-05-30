# Decision: Conviction Score Calibration Finding
**Date:** 2026-05-29  
**Status:** Recorded — no immediate action  
**Script:** `analysis/calibration_check.py`  
**Data:** 36 clean exits (1 dexscreener_fallback excluded), 20 post-v2

---

## Finding

The model's `conviction_score` does **not** rank trades within the passing set.

```
All clean  (n=36): rho = +0.221, p = 0.196   — not significant
Post-v2    (n=20): rho = -0.177, p = 0.454   — not significant, directionally negative
```

Neither result approaches significance. The p-values confirm the sample is too small
to draw hard conclusions, but the direction in the post-v2 cohort — the only clean
data — is mildly anti-calibrated.

**Win rate and avg net by conviction band (all clean exits):**

| Band | n | avg_net | win_rate |
|------|---|---------|----------|
| 0.65–0.70 | 12 | -12.68% | 33.3% |
| 0.70–0.75 | 12 | -13.77% | 16.7% |
| 0.75–0.80 | 4  | +2.58%  | 50.0% |
| **0.80+** | 8  | **-9.49%** | **25.0%** |

The 0.80+ band — the model's most confident signals — has **lower win rate and worse
avg net than the 0.65–0.70 floor band.** This is the clearest diagnostic: if the
score were a useful ranking signal, the 0.80+ band should be the best.

**Backtest vs realized delta:**

| Band | avg_backtest | avg_realized | delta |
|------|-------------|-------------|-------|
| 0.65–0.70 | -3.22% | -12.68% | -9.46pp |
| 0.70–0.75 | -17.46% | -13.77% | +3.70pp |
| 0.75–0.80 | -8.24% | +2.58%  | +10.82pp |
| 0.80+ | +6.18% | -9.49% | **-15.67pp** |

The 0.80+ band was backtest-positive (+6.18%) but realized deeply negative (-9.49%).
The -15.67pp delta is the largest in the set. High-conviction signals may be
over-exposed to slippage and fast price extension — by the time a fill is confirmed,
the move is largely complete and mean-reversion dominates.

**Kelly criterion:**

```
win_rate:          28.6%
avg_win:          +19.33%
avg_loss:         -23.05%
b (win/loss ratio): 0.84
Kelly fraction:   -56.6%  ← negative
```

Kelly is negative. The strategy does not have positive expected value at current
parameters regardless of position size.

---

## Interpretation

The model was trained on 44k+ collector rows with a binary 5-minute outcome label
(price up vs. down). It learned to separate "plausible momentum setup" from "bad
signal" well enough to act as a useful pre-filter. What it did **not** learn — and
cannot be expected to learn from the current feature set — is ranking within the
passing set.

Three reasons high-conviction is not performing better:

1. **Feature set is price/volume only.** The model has no holder data, no tax rate,
   no LP lock info. These features (now being collected via GoPlus) are the primary
   differentiators between rugs and legitimate launches. A model that doesn't see
   them can't price risk correctly.

2. **Training label mismatch.** The 5-minute outcome used for training measures
   short-term price momentum. The strategy holds for 5 minutes from fill, not 5
   minutes from scan. Drift between scan and fill (0–15% upward per the entry_cost_pct
   distribution) means the training label and the strategy's actual profit window are
   misaligned.

3. **High conviction = already extended.** The positive-drift drift-band finding
   (Q4 analysis) showed that "discount fills" (price dipped between signal and quote)
   had 100% win rate. Higher conviction signals tend to arrive with more positive
   drift — meaning the model is most confident about tokens that have already moved
   the most, leaving less remaining upside and more reversal risk.

---

## Decisions

**Do NOT raise the conviction threshold** to filter for "higher quality" signals.
The ordering doesn't work. Raising from 0.65 to e.g. 0.75 would simply reduce
volume without improving outcomes.

**Do NOT retrain immediately.** With 36 exits and no holder features in the training
corpus, retraining would produce the same model with less data. Wait for:
- ≥200 exits (current: 36)
- ≥4 weeks of GoPlus-enriched collector data
- Post-stop-loss data to correct the loss-distribution bias in current exits

**Do use the model as a binary gate.** Score ≥ threshold = trade. Score < threshold =
skip. This is what it does well. The threshold itself (0.65 shadow, 0.70 live) is
appropriate given the win rate.

**Schedule a re-calibration** after stop-loss has accumulated 50+ exits. The current
distribution is dominated by timer-only exits with no catastrophic-loss truncation.
Stop-loss exits will shift the loss distribution and may reveal different calibration
behavior.

---

## What Would Change the Conclusion

- A post-stop-loss dataset of ≥100 exits with holder features in signal_features
- A retrained model that uses GoPlus fields (top5_pct, creator_pct, lp_locked_pct)
- If rho post-retrain is > +0.4 with p < 0.10, proceed to using score as a ranker
  (e.g., position sizing proportional to conviction)

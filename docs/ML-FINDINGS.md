# ML & Data Findings

*Last updated: 2026-05-30. Based on 19,700+ collector signals (May 17–23) and 2,228 scanner signals (May 3–17). Note: intake-gap diagnostic (2026-05-30) revised the survivorship-bias assessment — see below.*

---

## Dataset

### Collector (unbiased — all tokens seen)

| Stat | Value |
|---|---|
| Total rows | 19,700+ |
| With 5m outcome | 19,685+ |
| Unique tokens | ~830 |
| Date range | 2026-05-17 → present |
| Chains | Base (27%), Solana (73%) |
| Scanner window rows (15–90m) | 10,070 |

The collector records every token DexScreener returns, regardless of whether it passes filters. This is unbiased **within DexScreener's view** — but DexScreener itself is curated.

**⚠ Survivorship bias (revised 2026-05-30):** DexScreener `/token-profiles/latest/v1` only surfaces tokens that had a profile explicitly submitted — meaning tokens that survived long enough for someone to file one. Tokens that rug in under ~30 minutes never appear. Intake-gap diagnostic (2026-05-30) found we capture only **~6% of Base on-chain launches** and ~4% of Solana tradeable new tokens. The rug class we most want to detect is systematically underrepresented. See `docs/decisions/INTAKE-GAP-2026-05-30.md` for the full analysis and remediation plan.

### Scanner DB (scanner survivors only)

| Stat | Value |
|---|---|
| Total signals | ~2,228 |
| Date range | 2026-05-03 → 2026-05-17 |
| With 5m outcome | ~587 |
| With 15m outcome | ~434 |

Scanner data is biased — only tokens that survived all hard filters and LLM scoring. Useful for LLM calibration, not for unbiased ML training.

---

## Key Data Findings

### 1. Base consistently outperforms Solana

| Chain | n | avg 5m | win rate |
|---|---|---|---|
| Base | 5,295 | +3.53% | 44.1% |
| Solana | 14,344 | −1.00% | 40.5% |

Within the scanner window (15–90m):

| Chain | n | avg 5m | win rate |
|---|---|---|---|
| Base | 2,435 | +5.41% | 47.6% |
| Solana | 7,624 | −1.26% | 41.1% |

Chain is the single strongest predictor. Base returns a positive expected value after costs with no model. Solana is marginally negative without filtering.

### 2. Micro-trend is the most reliable filter signal

| Chain | micro_trend | avg 5m | win rate |
|---|---|---|---|
| Base | fading | +6.08% | 52.6% |
| Base | up | +5.40% | 55.3% |
| Base | recovering | +2.31% | 53.0% |
| Base | down | +2.19% | 46.2% |
| Base | flat | +0.55% | 25.8% |
| Solana | up | +0.54% | 46.0% |
| Solana | flat | −0.64% | 32.5% |
| Solana | fading | −1.07% | 45.2% |
| Solana | down | −1.47% | 42.0% |
| Solana | recovering | −2.57% | 40.3% |

**Current filter excludes:** `recovering` and `down` (both chains), `flat` (Solana only — added Phase 9).  
**Note:** Base `flat` has a 25.8% win rate — poor, but positive avg. Not currently excluded for Base.

### 3. V/L ratio — Phase 9 correction

Unbiased collector data (19,700 rows) overturned the May-17 "non-linear" V/L finding from scanner data:

| Solana V/L bucket | collector avg 5m | collector win rate |
|---|---|---|
| 0–2x | −0.07% | 36.3% |
| 2–4x | −0.56% | 43.4% |
| 4–6x | −0.75% | 44.4% |
| 6–8x | −1.42% | 43.9% |
| 8–12x | −1.43% | 41.6% |
| 12x+ | −2.12% | 40.2% |

The scanner data had shown 6–8x at 62.7% win rate. That was survivorship bias — only scanner-surviving tokens. Collector data (all tokens) shows 6–8x is the second-worst bucket. **Filter reverted to flat ≤4x ceiling for Solana (Phase 9, 2026-05-23).**

Within filter-eligible Solana tokens:

| V/L zone | avg 5m | win rate |
|---|---|---|
| ≤4x (current pass) | +0.40% | 38.4% |
| 4–6x (blocked) | +0.31% | 45.2% |
| 6–8x (was re-admitted, now blocked) | −0.56% | 44.2% |

The 4–6x zone has better performance than 6–8x — the correction made things worse. Flat ≤4x is the correct Solana ceiling with current data.

### 4. Age — Base 15–20m is the best window

| Chain | age bucket | avg 5m | win rate |
|---|---|---|---|
| Base | 0–5m | +21.23% | 60.0% |
| Base | 5–10m | +15.01% | 56.5% |
| Base | **15–20m** | **+18.82%** | **64.9%** |
| Base | 20–30m | +8.53% | 56.7% |
| Base | 30–60m | +5.23–5.86% | 49% |
| Solana | 15–20m | −3.40% | 43.7% (worst) |
| Solana | 20–30m | +0.60% | 45.0% (best) |

**Do not raise the age floor to 20m on Base** — 15–20m is the highest-performing Base bucket. Solana 15–20m is the worst Solana bucket; a Solana-specific 20m floor could help, but sample is small.

### 5. Volume trend on Base is the strongest entry signal

| Base vol_trend | avg 5m | win rate |
|---|---|---|
| rising | +7.22% | 52.7% |
| flat | +5.48% | 54.6% |
| falling | +1.55% | 47.7% |

Rising volume on Base = real buying building. Solana rising = +0.08% — chains behave oppositely on this signal.

### 6. High buy pressure is a topping signal on Solana

| Solana buy_pct_5m | avg 5m | win rate |
|---|---|---|
| ≤75% | −0.05% | 42.3% |
| 75–85% | +2.31% | 43.5% |
| >85% | **−3.47%** | **36.7%** |

Above 85% buy pressure on Solana: everyone has already bought. You're the exit liquidity. (Sample small at n=61 — not yet added as a filter.)

### 7. Liquidity correlates positively; buy volume correlates negatively

Spearman correlations with 5-minute outcome:

| Feature | r | Direction |
|---|---|---|
| buys_1h | −0.074 | More 1h buys → worse |
| vl_ratio | −0.072 | Higher V/L → worse |
| price_ch_6h | +0.070 | Positive 6h trend → better |
| liquidity_usd | +0.065 | Higher liquidity → better |
| net_txn_1h | −0.064 | Net buy imbalance 1h → worse |

Counter-intuitive: raw buy *volume* predicts worse outcomes (crowded = already bid up). Higher *liquidity* predicts better outcomes (deeper book = cleaner execution, real depth behind the move).

---

## ML Model

### Architecture

- **Algorithm:** LightGBM binary classifier
- **Target:** `outcome_pct > 0` (5-minute win/loss)
- **Training:** 5,865 rows (May 17–20), token-grouped 5-fold CV
- **Validation:** 4,165 rows (May 21–23), no tokens shared with train
- **Regularization:** `num_leaves=20`, `min_child_samples=50`, `reg_lambda=5.0`
- **CV AUC:** 0.610 ± 0.010

### Validation performance

| Threshold | Precision | Recall | Lift | N flagged |
|---|---|---|---|---|
| 0.55 | 50.0% | 42.5% | 1.19x | 1,491 |
| 0.60 | 52.2% | 30.6% | 1.24x | 1,028 |
| 0.65 | 54.5% | 19.5% | 1.29x | 628 |
| 0.70 | 57.7% | 11.1% | 1.37x | 336 |
| 0.75 | 60.0% | 4.6% | 1.42x | 135 |

### Chain-specific models

| Chain | CV AUC | Val AUC | At threshold 0.70 |
|---|---|---|---|
| **Base** | **0.661** | **0.673** | 64.3% precision, 1.43x lift |
| Solana | 0.579 | 0.567 | 54.4% precision, 1.32x lift |

Base is substantially more predictable. Solana needs Birdeye `net_inflow_usd` and `unique_traders_1h` to reach comparable accuracy.

### Alternative targets — model gets much better at predicting large moves

| Target | CV AUC | Best lift |
|---|---|---|
| >0% (win/loss) | 0.610 | 1.52x |
| >2% | 0.636 | 1.56x |
| >5% | 0.657 | 1.78x |
| **>10%** | **0.684** | **2.34x** |

The model is almost twice as good at identifying tokens that will move 10%+ as it is at simple win/loss. For auto-trading, the right target is predicting the big pumps.

### Top features (by SHAP importance)

| Rank | Feature | What it measures |
|---|---|---|
| 1 | `volume_5m` | Active buying in last 5 minutes |
| 2 | `txn_accel` | Buy transaction acceleration vs 1h baseline |
| 3 | `price_ch_6h` | Prior momentum — was there a build-up? |
| 4 | `net_txn_1h` | Net buy/sell delta over 1h |
| 5 | `age_minutes` | Token age — earlier generally better |
| 6 | `buy_pct_1h` | Buyer dominance sustained over 1h |
| 7 | `vl_ratio` | Volume/liquidity ratio |
| 8 | `liq_mcap_ratio` | Liquidity relative to market cap |

All pure momentum signals. No fundamental quality signals in the top features.

### Decision tree (interpretable rules, AUC 0.622)

The model's top-level split: `volume_5m > $53.45`. Tokens with less than $53 in 5-minute volume are almost all losers regardless of other factors. Within the high-volume set, the model looks at 6h price trend, sell pressure, and liquidity/mcap ratio to separate winners from losers.

### Time stability

The model decays quickly between training and validation periods:

| Period | Daily AUC | Prec @0.60 |
|---|---|---|
| Train (May 17–20) | 0.90–0.92 | 89–91% |
| Val (May 21–22) | 0.59–0.60 | 50–54% |

CV AUC of 0.61 is the honest forward-looking estimate. **The model needs to be retrained on a rolling weekly basis** to stay calibrated to current chain conditions (launchpad mix, bot behavior, liquidity patterns).

---

## Backtest Results

$10/trade flat, 1.5% round-trip cost (gas + swap fee + slippage), May 21–23.

| Strategy | Trades | Win % | Total P&L | Final ($100 start) | Max DD | Profit Factor |
|---|---|---|---|---|---|---|
| Random | 4,196 | 37% | −$556 | −$456 | −$562 | 0.86x |
| Hard filter only | 1,224 | 40% | +$199 | $299 | −$42 | 1.22x |
| Model ≥0.65 | 630 | 49% | +$300 | $400 | −$92 | 1.52x |
| Model ≥0.70 | 337 | 51% | +$281 | $381 | −$40 | 2.02x |
| Model ≥0.60 + filter | 525 | 51% | +$299 | $399 | −$59 | 1.74x |
| Base only ≥0.65 | 349 | 50% | +$210 | $310 | −$74 | 1.75x |
| **First-entry ≥0.70** | **134** | **55%** | **+$149** | **$249** | **−$11** | **2.50x** |
| Solana only ≥0.70 | 112 | 53% | +$117 | $217 | −$15 | 2.26x |

**First-entry ≥0.70** is the most practical strategy: each token is traded once (first time it crosses the threshold), $11 max drawdown, 55% win rate, profit factor 2.50x. Sustainable on a $100–200 wallet.

### Honest caveats

- **2.5 days of validation data** — results are directionally correct but sample is small. Needs 4+ more weeks before deploying real money with confidence.
- **1.5% cost assumption** — may be optimistic for thin-book tokens. At 3% cost the edge is squeezed but survives at ≥0.65 threshold.
- **Assumes fills at DexScreener prices** — real DEX fills may differ from quoted price, especially on very new tokens.
- **Model trained on 4 days immediately prior** — potentially the most "learnable" regime. Rolling retraining is essential.

---

## What's Missing

### Birdeye enrichment in collector

The single biggest gap. `net_inflow_usd` (real USD flowing into the token) and `unique_traders_1h` (unique wallet count) are not in the collector — they require a Birdeye API call at collection time. These are the strongest Base predictors and are completely absent from the current ML training data.

Adding Birdeye to the collector is one build session. Once added, the model retrains with this feature and Base accuracy should improve materially (scanner data showed net_inflow as the dominant Base predictor).

### More data

Current model is trained on 4 days and validated on 2.5 days. Minimum 4–6 weeks of data before committing real capital to the automated strategy.

### Chain-specific model deployment

The combined model gives 0.61 AUC. Separate chain models give 0.67 (Base) and 0.57 (Solana). Deploying chain-specific models is straightforward once the execution layer is built.

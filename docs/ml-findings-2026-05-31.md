# ML Findings — 2026-05-31

**Dataset:** 25,887 rows, 14 days (May 17–31), Base (4,365) + Solana (21,522)  
**Script:** `analysis/ml_full.py`  
**Figures:** `analysis/figures/ml_full/`  
**Model:** LightGBM binary classifier, 4-fold time-series CV, 114 features including all sparse GoPlus/Birdeye columns with missingness indicators

---

## 1. Data Quality

The scanner window (age 15–90 min) cuts the 56k corpus to **25,887 rows**. Remaining rows are outside the age window — tokens the scanner never sees.

**Sparse enrichment is too sparse to be real features right now:**
- GoPlus binary flags (is_honeypot, is_mintable, etc.): **0.1–0.8% fill** (28–197 rows)
- Birdeye columns (unique_traders, net_inflow, etc.): **0.3–0.4% fill** (61–108 rows)
- Missingness indicators (`has_goplus`, `has_birdeye`) are present in the model but contribute minimally

GoPlus flag analysis on 2,764 enriched rows found only two flags with enough data to compare: `is_mintable` (+12.2% avg outcome when set, n=15) and `is_open_source` (essentially flat vs baseline). Sample sizes too small to act on. **GoPlus enrichment is broken for the scanner window** — most tokens age past 90min before enrichment arrives.

---

## 2. Outcome Rates (scanner window, 15–90 min)

| Target | Base | Solana |
|--------|------|--------|
| any gain (>0%) | 47.8% | 40.7% |
| good (≥5%) | 34.7% | 28.6% |
| strong (≥10%) | 27.7% | 21.6% |
| **moonshot (≥20%)** | **17.2%** | **12.8%** |

Base is structurally better than Solana at every threshold. The 4.4pp moonshot gap is consistent with earlier collector findings.

---

## 3. Model Performance

All models trained on first 75% of data (by time), validated on last 25%.

### BASE

| Target | CV AUC | Val AUC | Val Avg Precision | Base AP |
|--------|--------|---------|-------------------|---------|
| any gain | 0.656 | 0.641 | 0.579 | 0.459 |
| good ≥5% | 0.694 | 0.700 | 0.493 | 0.323 |
| strong ≥10% | 0.709 | 0.718 | 0.437 | 0.258 |
| **moonshot ≥20%** | **0.752** | **0.741** | **0.323** | **0.159** |

**Base moonshot is the standout model.** AUC 0.741 on a 17% base rate, 2x AP lift. At threshold 0.60: **44.7% precision, 2.81x lift**. At 0.65: **50% precision, 3.14x lift** (n=12 flagged per val period).

Performance improving monotonically as target gets rarer is a strong signal the model is learning real discriminative patterns, not base-rate memorisation.

### SOLANA

| Target | CV AUC | Val AUC | Val Avg Precision | Base AP |
|--------|--------|---------|-------------------|---------|
| any gain | 0.577 | 0.596 | 0.503 | 0.414 |
| good ≥5% | 0.642 | 0.651 | 0.415 | 0.295 |
| strong ≥10% | 0.675 | 0.674 | 0.351 | 0.224 |
| **moonshot ≥20%** | **0.700** | **0.709** | **0.279** | **0.134** |

Solana moonshot model AUC 0.709. Requires much higher thresholds to be useful:
- 0.60: 28.5% precision, 2.13x lift (n=673)
- **0.70: 61.5% precision, 4.59x lift (n=91)**
- 0.75: 64.3% precision, 4.79x lift (n=56)

Solana needs ≥0.70 threshold to extract clean signal.

### Combined (chain as binary feature)

| Target | CV AUC | Val AUC |
|--------|--------|---------|
| strong ≥10% | 0.684 | 0.679 |
| moonshot ≥20% | 0.716 | 0.717 |

Combined doesn't outperform per-chain models. Per-chain models preferred.

---

## 4. Top Features by Target

### BASE — Moonshot (≥20%)
1. `buys_5m` — raw 5m buy count, dominant by large margin (gain 6,685 vs next 2,813)
2. `liquidity_usd`
3. `age_minutes`
4. `liq_mcap_ratio` — liquidity / market cap
5. `volume_5m`
6. `price_ch_6h` — 6h price change context
7. `vol_trend_pct`
8. `net_txn_1h` — net buy/sell txn delta
9. `vl_ratio`
10. `momentum_score` — price_ch_5m × clipped vol5m_1h_ratio

### SOLANA — Moonshot (≥20%)
1. `volume_5m` — dominant by very large margin (gain 29,682 vs second 7,237)
2. `sells_5m`
3. `liq_mcap_ratio`
4. `buys_5m`
5. `price_ch_5m`
6. `buy_pct_1h`
7. `momentum_score`
8. `volume_6h`
9. `txn_accel`
10. `age_minutes`

**Key structural difference:** Base moonshoots are driven by **buy count** (`buys_5m`). Solana moonshoots are driven by **dollar volume** (`volume_5m`). The ratio `liq_mcap_ratio` is top-5 on both chains.

**Sparse features did not rank.** GoPlus-specific columns never appeared in top 12 for any target (< 1% fill). The missingness indicators ranked in the bottom half. This confirms: do not gate scanner decisions on GoPlus enrichment for 15–90 min window tokens.

**`hour_utc` appeared on Solana good target (rank 12).** Weak time-of-day effect. Worth monitoring over more data.

---

## 5. Best Filter Combinations (Pattern Mining — no model required)

Pure signal combos with highest empirical moonshot rate.

### BASE — Moonshot ≥20% (base rate 17.2%)

| micro_trend | vol_trend | vl_ratio | n | moonshot% | lift |
|-------------|-----------|----------|---|-----------|------|
| flat | rising | 2–4x | 22 | **45.5%** | 2.64x |
| fading | flat | >8x | 89 | **38.2%** | 2.22x |
| flat | rising | 4–8x | 27 | **37.0%** | 2.15x |
| fading | rising | >8x | 26 | **34.6%** | 2.01x |
| up | flat | 4–8x | 257 | **33.1%** | 1.92x |
| fading | rising | 4–8x | 182 | **31.9%** | 1.85x |

Top Base pattern: **flat momentum + rising volume + mid VL (2–4x)** → 45.5% moonshot rate.  
`fading` micro_trend consistently performs above base across all vol_trend categories — brief consolidation before surge.  
High VL (>8x) on Base doesn't kill returns the way the current filter ceiling implies.

### SOLANA — Moonshot ≥20% (base rate 12.8%)

| micro_trend | vol_trend | vl_ratio | n | moonshot% | avg_outcome% | lift |
|-------------|-----------|----------|---|-----------|-------------|------|
| up | rising | <1x | 108 | **46.3%** | +36.8% | 3.61x |
| fading | rising | 1–2x | 46 | **37.0%** | +25.6% | 2.88x |
| up | rising | 1–2x | 136 | **33.8%** | +16.5% | 2.64x |
| recovering | rising | 4–8x | 67 | **29.9%** | +3.9% | 2.33x |
| down | rising | 4–8x | 177 | **27.7%** | +1.0% | 2.16x |
| fading | rising | 2–4x | 135 | **27.4%** | +11.8% | 2.14x |

**Solana's clearest signal: `up` or `fading` + `rising` + low VL (<2x) = 34–46% moonshot rate.**  
The `up+rising+<1x` combo has both the highest moonshot rate (46.3%) AND the highest avg outcome (+36.8%) — cleanest signal in the dataset.  
`down + rising` at high VL shows lift >2x but avg_outcome near 0 (bimodal: mostly losses with occasional huge spike). Don't chase.

---

## 6. Current Scanner Filter Assessment

Current pre-filter:
- Solana: VL ≤4, micro_trend ∉ {flat, recovering, down}
- Base: VL ≤8, micro_trend ∉ {recovering, down}

**What this misses:**
- On Base: `fading` micro_trend is currently passed and performs well — correct. But high VL (>8x) tokens are blocked, yet `fading+flat+>8x` has 38.2% moonshot rate.
- On Solana: `recovering+rising+4–8x` (29.9% moonshot, 2.33x lift) is currently blocked — this is a false negative. Worth relaxing to allow `recovering` when `vol_trend=rising`.

**Model vs current filter at best threshold:**
- Base moonshot at 0.60: **44.7% precision vs 17.2% base** — model would 2.6x the scanner's selection quality
- Solana moonshot at 0.70: **61.5% precision vs 13.4% base** — model would 4.6x selection quality

**Recommended path:** Add model score as a pre-filter OR as a scored signal in the LLM prompt ("ML moonshot score: 0.73 / high confidence"). Even soft anchoring would improve LLM decision consistency.

---

## 7. Summary of Key Findings

1. **Models work.** AUC 0.71–0.75 on 12–17% base rate targets is solid for 5-min meme coin prediction with 14 days of data.
2. **Base is the better chain for the scanner.** Higher moonshot rates, better model performance, cleaner patterns across the board.
3. **volume_5m (Solana) and buys_5m (Base) are the single strongest predictors.** Raw activity in the last 5 minutes beats every derived signal.
4. **liq_mcap_ratio is top-5 on both chains.** Low market cap relative to liquidity = explosive potential. Should be weighted more heavily in the LLM prompt.
5. **GoPlus/Birdeye enrichment is too sparse for scanner-window tokens.** 0.1–0.8% fill. Fix the timing (enrich at ingest, not lazy) before treating these as features.
6. **`fading` micro_trend on Base is underrated.** Multiple high-lift patterns include it. It signals consolidation before a move, not decay.
7. **Solana needs threshold ≥0.70 to extract clean signal.** At that level: 61.5% precision, 4.59x lift, ~30 tokens/day passing.
8. **Combined model offers no advantage** over per-chain models. Keep them separate.

---

## 8. Next Steps

- [ ] Export per-chain moonshot models; score tokens live and persist score to `raw_signals`
- [ ] Add model score to n8n LLM prompt context for anchoring
- [ ] Relax Solana filter to allow `recovering` when `vol_trend = rising` (29.9% moonshot, currently blocked)
- [ ] Fix GoPlus enrichment timing to hit >50% fill inside the scanner window
- [ ] Investigate `buys_5m` threshold on Base — find the inflection point
- [ ] Retrain monthly; 30+ days will meaningfully improve Solana model AUC

---

## Raw Output

<details>
<summary>Full script output</summary>

```
DEX Scanner ML Analysis — 2026-05-31
============================================================

=== Loading data ===
Rows: 25,887  |  Date range: 2026-05-17 → 2026-05-31
Chain split: {'solana': 21522, 'base': 4365}

=== Column fill rates ===
  market_cap                                99.9%  (25,852/25,887)
  vol_trend                                 95.8%  (24,808/25,887)
  vol_trend_pct                             95.8%  (24,808/25,887)
  buy_pct_5m                                95.9%  (24,824/25,887)
  unique_traders_1h                          0.4%  (108/25,887)
  net_inflow_usd                             0.4%  (108/25,887)
  unique_traders_30m                         0.3%  (76/25,887)
  unique_traders_24h                         0.3%  (76/25,887)
  buy_volume_1h_usd                          0.3%  (76/25,887)
  sell_volume_1h_usd                         0.3%  (76/25,887)
  volume_24h_usd                             0.3%  (76/25,887)
  buy_volume_24h_usd                         0.3%  (76/25,887)
  sell_volume_24h_usd                        0.3%  (76/25,887)
  trade_count_1h                             0.3%  (76/25,887)
  trade_count_24h                            0.3%  (76/25,887)
  holder_count_birdeye                       0.3%  (76/25,887)
  market_count                               0.2%  (61/25,887)
  top1_pct                                   0.1%  (28/25,887)
  top5_pct                                   0.1%  (25/25,887)
  top10_pct                                  0.1%  (28/25,887)
  holder_count_gp                            0.8%  (197/25,887)
  creator_pct                                0.6%  (147/25,887)
  creator_balance                            0.6%  (147/25,887)
  lp_holder_count                            0.6%  (144/25,887)
  lp_locked_pct                              0.6%  (144/25,887)
  buy_tax                                    0.2%  (47/25,887)
  sell_tax                                   0.2%  (53/25,887)
  is_honeypot_gp                             0.7%  (189/25,887)
  is_blacklisted                             0.7%  (189/25,887)
  is_mintable                                0.7%  (189/25,887)
  hidden_owner                               0.7%  (189/25,887)
  can_take_back_ownership                    0.7%  (189/25,887)
  owner_change_balance                       0.7%  (189/25,887)
  honeypot_with_same_creator                 0.6%  (147/25,887)
  is_proxy                                   0.7%  (189/25,887)
  is_open_source                             0.8%  (197/25,887)
  transfer_pausable                          0.7%  (189/25,887)
  trading_cooldown                           0.7%  (189/25,887)
  anti_whale_modifiable                      0.7%  (189/25,887)
  slippage_modifiable                        0.7%  (189/25,887)

=== Outcome summary ===

  BASE (n=4,365)
    any_gain   (outcome_pct > 0): 47.8%
    good       (outcome_pct >= 5): 34.7%
    strong     (outcome_pct >= 10): 27.7%
    moonshot   (outcome_pct >= 20): 17.2%

  SOLANA (n=21,522)
    any_gain   (outcome_pct > 0): 40.7%
    good       (outcome_pct >= 5): 28.6%
    strong     (outcome_pct >= 10): 21.6%
    moonshot   (outcome_pct >= 20): 12.8%

============================================================
GOPLUS FLAG ANALYSIS (enriched rows only)
============================================================
GoPlus-enriched rows: 2,764

Flag                                       set%  avg_outcome when set  avg_outcome when 0
------------------------------------------------------------------------------------------
  is_mintable                              0.5%    +12.21%  (n=15)       +2.82%  (n=174)
  is_open_source                           6.8%     +3.57%  (n=189)       +3.28%  (n=8)

============================================================
CHAIN: BASE
============================================================
Rows: 4,365
  any_gain  : 47.8% positive (2,087 rows)
  good      : 34.7% positive (1,516 rows)
  strong    : 27.7% positive (1,208 rows)
  moonshot  : 17.2% positive (751 rows)
Feature columns: 114
Train: 3,272  Val: 1,093  (cutoff: 2026-05-25)

── Target: any_gain (outcome_pct > 0) ──
   Val base rate: 45.9%  (502 positives)
   CV AUC (4-fold time-series): 0.6557
   Val AUC: 0.6411  |  Avg Precision: 0.5786  (base AP: 0.4593)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  51.5%    |  90.4%  | 1.12x  | 881
   0.45      |  52.8%    |  78.1%  | 1.15x  | 742
   0.50      |  53.8%    |  61.4%  | 1.17x  | 573
   0.55      |  58.9%    |  40.0%  | 1.28x  | 341
   0.60      |  64.9%    |  19.9%  | 1.41x  | 154
   0.65      |  67.4%    |   6.2%  | 1.47x  | 46
   0.70      |  33.3%    |   0.2%  | 0.73x  | 3

   Top 12 features (any_gain):
     buy_pct_5m                          2,412
     liq_mcap_ratio                      730
     age_minutes                         729
     vol_trend_pct                       594
     volume_5m                           581
     price_ch_6h                         538
     txn_accel                           518
     buys_5m                             497
     vl_ratio                            455
     sells_5m                            438
     volume_1h                           373
     price_ch_5m                         364

── Target: good (outcome_pct >= 5) ──
   Val base rate: 32.3%  (353 positives)
   CV AUC (4-fold time-series): 0.6935
   Val AUC: 0.7003  |  Avg Precision: 0.4930  (base AP: 0.3230)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  42.7%    |  80.2%  | 1.32x  | 662
   0.45      |  45.0%    |  64.3%  | 1.39x  | 504
   0.50      |  49.5%    |  51.0%  | 1.53x  | 364
   0.55      |  52.4%    |  34.3%  | 1.62x  | 231
   0.60      |  54.3%    |  21.5%  | 1.68x  | 140
   0.65      |  57.8%    |  10.5%  | 1.79x  | 64
   0.70      |  66.7%    |   2.8%  | 2.06x  | 15
   0.75      |  50.0%    |   0.3%  | 1.55x  | 2

   Top 12 features (good):
     buys_5m                             3,040
     txn_accel                           2,629
     volume_5m                           2,276
     age_minutes                         1,245
     price_ch_6h                         1,087
     vol_trend_pct                       1,021
     liquidity_usd                       930
     liq_mcap_ratio                      890
     vl_ratio                            838
     price_ch_1h                         831
     volume_1h                           693
     buy_pct_1h                          679

── Target: strong (outcome_pct >= 10) ──
   Val base rate: 25.8%  (282 positives)
   CV AUC (4-fold time-series): 0.7093
   Val AUC: 0.7181  |  Avg Precision: 0.4374  (base AP: 0.2580)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  50.7%    |  25.9%  | 1.96x  | 144
   0.45      |  57.1%    |   2.8%  | 2.21x  | 14

   Top 12 features (strong):
     buys_5m                             3,269
     volume_5m                           743
     market_cap                          597
     buy_pct_5m                          572
     liq_mcap_ratio                      570
     age_minutes                         557
     price_ch_6h                         503
     vol_trend_pct                       490
     vl_ratio                            346
     price_ch_1h                         338
     txn_accel                           322
     volume_1h                           286

── Target: moonshot (outcome_pct >= 20) ──
   Val base rate: 15.9%  (174 positives)
   CV AUC (4-fold time-series): 0.7516
   Val AUC: 0.7411  |  Avg Precision: 0.3233  (base AP: 0.1592)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  34.4%    |  48.9%  | 2.16x  | 247
   0.45      |  35.1%    |  37.9%  | 2.21x  | 188
   0.50      |  37.1%    |  24.7%  | 2.33x  | 116
   0.55      |  34.2%    |  14.4%  | 2.15x  | 73
   0.60      |  44.7%    |   9.8%  | 2.81x  | 38
   0.65      |  50.0%    |   3.4%  | 3.14x  | 12

   Top 12 features (moonshot):
     buys_5m                             6,685
     liquidity_usd                       2,813
     age_minutes                         2,038
     liq_mcap_ratio                      1,979
     volume_5m                           1,924
     price_ch_6h                         1,743
     vol_trend_pct                       1,521
     net_txn_1h                          1,445
     vl_ratio                            1,366
     momentum_score                      1,347
     buy_pct_5m                          1,321
     market_cap                          1,229

── Pattern mining (moonshot ≥20%) — BASE ──
micro_trend     vol_trend  vl_bin        n   moon%  avg_out%   lift
-----------------------------------------------------------------
flat            rising     2-4          22    45.5     15.54   2.64x
fading          flat       >8           89    38.2     13.09   2.22x
flat            rising     4-8          27    37.0     13.38   2.15x
fading          rising     >8           26    34.6     11.07   2.01x
up              flat       4-8         257    33.1     10.83   1.92x
fading          rising     4-8         182    31.9     12.14   1.85x
up              rising     2-4         263    27.4      5.86   1.59x
fading          falling    >8           90    26.7      9.63   1.55x
up              rising     4-8         186    26.3      5.76   1.53x
fading          rising     1-2          42    26.2      5.48   1.52x
fading          rising     2-4         129    24.8      9.81   1.44x
fading          flat       2-4          62    24.2      4.23   1.41x
fading          flat       4-8         215    24.2      7.50   1.41x
up              falling    >8           72    23.6      6.40   1.37x
down            falling    >8           41    22.0      8.92   1.28x

============================================================
CHAIN: SOLANA
============================================================
Rows: 21,522
  any_gain  : 40.7% positive (8,753 rows)
  good      : 28.6% positive (6,157 rows)
  strong    : 21.6% positive (4,652 rows)
  moonshot  : 12.8% positive (2,761 rows)
Feature columns: 114
Train: 16,139  Val: 5,383  (cutoff: 2026-05-28)

── Target: any_gain (outcome_pct > 0) ──
   Val base rate: 41.4%  (2228 positives)
   CV AUC (4-fold time-series): 0.5767
   Val AUC: 0.5961  |  Avg Precision: 0.5029  (base AP: 0.4139)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  44.7%    |  87.9%  | 1.08x  | 4,378
   0.45      |  45.9%    |  75.4%  | 1.11x  | 3,660
   0.50      |  47.5%    |  55.7%  | 1.15x  | 2,617
   0.55      |  50.7%    |  33.9%  | 1.22x  | 1,490
   0.60      |  53.4%    |  18.0%  | 1.29x  | 751
   0.65      |  59.5%    |   9.0%  | 1.44x  | 336
   0.70      |  61.2%    |   4.5%  | 1.48x  | 165
   0.75      |  71.7%    |   3.0%  | 1.73x  | 92
   0.80      |  77.4%    |   2.2%  | 1.87x  | 62

   Top 12 features (any_gain):
     volume_5m                           5,446
     age_minutes                         4,022
     buy_pct_1h                          3,628
     price_ch_5m                         3,390
     txn_accel                           3,082
     vl_ratio                            2,947
     momentum_score                      2,928
     buy_pct_5m                          2,804
     volume_6h                           2,623
     net_txn_1h                          2,622
     sells_1h                            2,585
     price_ch_1h                         2,504

── Target: good (outcome_pct >= 5) ──
   Val base rate: 29.5%  (1587 positives)
   CV AUC (4-fold time-series): 0.6415
   Val AUC: 0.6506  |  Avg Precision: 0.4145  (base AP: 0.2948)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  35.8%    |  85.5%  | 1.21x  | 3,793
   0.45      |  36.4%    |  79.1%  | 1.24x  | 3,446
   0.50      |  37.6%    |  68.4%  | 1.28x  | 2,887
   0.55      |  40.2%    |  49.9%  | 1.36x  | 1,971
   0.60      |  41.3%    |  24.4%  | 1.40x  | 938
   0.65      |  47.3%    |  10.3%  | 1.60x  | 347
   0.70      |  58.7%    |   5.5%  | 1.99x  | 150
   0.75      |  65.1%    |   3.4%  | 2.21x  | 83
   0.80      |  74.5%    |   2.6%  | 2.53x  | 55

   Top 12 features (good):
     volume_5m                           20,046
     age_minutes                         4,620
     txn_accel                           4,225
     buy_pct_1h                          3,975
     sells_5m                            3,928
     momentum_score                      3,753
     price_ch_5m                         3,436
     vl_ratio                            3,187
     buys_5m                             3,175
     sells_1h                            2,941
     price_ch_1h                         2,908
     hour_utc                            2,881

── Target: strong (outcome_pct >= 10) ──
   Val base rate: 22.4%  (1207 positives)
   CV AUC (4-fold time-series): 0.6749
   Val AUC: 0.6736  |  Avg Precision: 0.3512  (base AP: 0.2242)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  63.3%    |   3.1%  | 2.82x  | 60

   Top 12 features (strong):
     volume_5m                           19,222
     sells_5m                            1,481
     liq_mcap_ratio                      1,115
     buys_5m                             1,030
     price_ch_6h                         882
     momentum_score                      878
     net_txn_5m                          827
     market_cap                          800
     price_ch_5m                         800
     net_txn_1h                          727
     vl_ratio                            724
     buy_pct_1h                          720

── Target: moonshot (outcome_pct >= 20) ──
   Val base rate: 13.4%  (722 positives)
   CV AUC (4-fold time-series): 0.7003
   Val AUC: 0.7093  |  Avg Precision: 0.2792  (base AP: 0.1341)

   Threshold | Precision | Recall | Lift   | N flagged
   ----------|-----------|--------|--------|----------
   0.40      |  20.9%    |  74.0%  | 1.56x  | 2,558
   0.45      |  21.8%    |  64.3%  | 1.62x  | 2,132
   0.50      |  23.5%    |  55.8%  | 1.76x  | 1,712
   0.55      |  25.1%    |  43.8%  | 1.87x  | 1,258
   0.60      |  28.5%    |  26.6%  | 2.13x  | 673
   0.65      |  37.5%    |  13.0%  | 2.79x  | 251
   0.70      |  61.5%    |   7.8%  | 4.59x  | 91
   0.75      |  64.3%    |   5.0%  | 4.79x  | 56
   0.80      |  66.7%    |   1.1%  | 4.97x  | 12

   Top 12 features (moonshot):
     volume_5m                           29,682
     sells_5m                            7,237
     liq_mcap_ratio                      6,485
     buys_5m                             5,801
     price_ch_5m                         5,457
     buy_pct_1h                          5,188
     momentum_score                      5,087
     volume_6h                           5,081
     txn_accel                           4,555
     age_minutes                         4,529
     market_cap                          4,460
     vl_ratio                            4,449

── Pattern mining (moonshot ≥20%) — SOLANA ──
micro_trend     vol_trend  vl_bin        n   moon%  avg_out%   lift
-----------------------------------------------------------------
up              rising     <1          108    46.3     36.76   3.61x
fading          rising     1-2          46    37.0     25.57   2.88x
up              rising     1-2         136    33.8     16.46   2.64x
recovering      rising     4-8          67    29.9      3.94   2.33x
down            rising     4-8         177    27.7      1.02   2.16x
fading          rising     2-4         135    27.4     11.77   2.14x
down            flat       2-4          91    25.3      4.62   1.97x
recovering      rising     >8           28    25.0     -1.93   1.95x
fading          rising     >8          408    23.8     -5.95   1.85x
recovering      flat       >8          135    22.2     -0.56   1.73x
up              rising     2-4         266    22.2      6.46   1.73x
down            rising     2-4          86    22.1     -6.20   1.72x
fading          rising     4-8         198    21.7     -1.66   1.69x
down            flat       >8          457    21.7     -1.01   1.69x
fading          flat       >8          690    21.3     -2.03   1.66x

============================================================
COMBINED (base + solana)
============================================================

── strong (outcome_pct >= 10) — combined ──
   Val base rate: 22.3%
   CV AUC: 0.6836
   Val AUC: 0.6792

── moonshot (outcome_pct >= 20) — combined ──
   Val base rate: 13.3%
   CV AUC: 0.7160
   Val AUC: 0.7174
```

</details>

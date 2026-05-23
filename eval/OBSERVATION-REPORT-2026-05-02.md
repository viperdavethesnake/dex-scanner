# DEX Scanner — 1-Hour Observation Report

**Date:** 2026-05-02  
**Window:** 15:57 – 17:00 ET  
**Samples:** 12 (one every 5 minutes)  
**Model:** Qwen3.6-35B-A3B Q6_K, thinking on

---

## Summary

| Metric | Value |
|--------|-------|
| Total token launches seen | 89 |
| Sent to LLM (fresh, passed safety) | 32 (36%) |
| Pre-filtered as stale (>90 min) | 57 (64%) |
| INTERESTING | 7 (22% of rated) |
| WATCH | 9 (28% of rated) |
| SKIP | 16 (50% of rated) |
| Scan latency (min/avg/max) | 24s / 35s / 51s |

---

## Per-Sample Breakdown

| # | Time | Launches | INTER | WATCH | SKIP | Stale | Stale% | Elapsed |
|---|------|----------|-------|-------|------|-------|--------|---------|
| 01 | 15:57 | 10 | 0 | 3 | 2 | 5 | 50% | 36s |
| 02 | 16:03 | 9 | 1 | 0 | 2 | 6 | 66% | 37s |
| 03 | 16:09 | 7 | 1 | 0 | 0 | 6 | 85% | 26s |
| 04 | 16:14 | 6 | 1 | 0 | 0 | 5 | 83% | 31s |
| 05 | 16:20 | 7 | 1 | 0 | 0 | 6 | 85% | 28s |
| 06 | 16:25 | 7 | 0 | 0 | 1 | 6 | 85% | 24s |
| 07 | 16:31 | 9 | 1 | 0 | 1 | 7 | 77% | 29s |
| 08 | 16:36 | 7 | 0 | 0 | 3 | 4 | 57% | 51s |
| 09 | 16:42 | 7 | 1 | 1 | 1 | 4 | 57% | 39s |
| 10 | 16:48 | 7 | 0 | 0 | 4 | 3 | 42% | 51s |
| 11 | 16:53 | 6 | 1 | 2 | 1 | 2 | 33% | 35s |
| 12 | 16:59 | 7 | 0 | 3 | 1 | 3 | 42% | 35s |

---

## Notable Tokens

### MOGCOIN — appeared in 6 of 12 samples

The standout signal of the hour. Appeared consecutively from sample 3 through sample 12 (with a gap at sample 6 where no tokens reached the LLM). Correctly tracked by the model as momentum decayed:

- **Samples 3–7:** INTERESTING — "rising volume (+363%), 65% buy pressure, upward micro trend, no disqualifying failures"
- **Sample 11:** INTERESTING → degrading — "minor V/L ratio deviation is the only soft signal failure"
- **Sample 12:** WATCH — "strong 62% buy pressure with flat micro suggests a potential bounce"

The model caught the transition accurately. But the dashboard showed the same INTERESTING card for 30+ minutes with no indication it was a repeat.

### MOG — appeared in 3 samples (1, 11, 12)

Bookended the hour: WATCH in sample 1 (early momentum, 1h drop failure), INTERESTING in sample 11 (momentum confirmed), WATCH in sample 12 (1h drawdown returned).

### BUTT, TOLL — short-lived, appeared only in samples 11–12

Fresh launches arriving in the final 10 minutes of the window.

---

## Gaps and Issues

### 1. Stale rate is the dominant problem — avg 64%, peak 85%

Between 16:09 and 16:31 (samples 3–7), 77–85% of the DexScreener feed was >90 minutes old. During that stretch, the LLM was seeing **1 token per scan**. Samples 3, 4, 5 sent only 1 token to the LLM each — technically working but near-useless as a screener.

Root cause: DexScreener's `/token-profiles/latest/v1` endpoint appears to refresh slowly and recycles old profiles. The screener has no control over this, but the current behavior isn't surfaced clearly — the header says "2 pre-filtered (>90m)" but doesn't signal when the whole batch is essentially dead.

### 2. No cross-scan deduplication

MOGCOIN appeared as INTERESTING for ~30 consecutive minutes. A user polling every 5 minutes sees the same card over and over with no indication it's a repeat. There's no "seen before / already alerted" state. If someone acted on the first INTERESTING signal, the subsequent identical cards are noise — or worse, could trigger repeated entries.

### 3. Latency outliers at 51s (samples 8, 10)

Both outliers had 3–4 tokens in the prompt with multiple signal failures each. The thinking model generates longer reasoning for tokens with more complex failure patterns. 51s is fine, but at heavier token counts it could push toward the 180s timeout during a busy market period.

### 4. V/L ratio is the primary WATCH bottleneck

The most common single-failure pattern across all WATCH tokens was V/L out of range (>8×). This suggests either: (a) the 8× upper limit is too tight for the current market, or (b) new launches naturally spike V/L before it normalizes. Several tokens showed strong buy pressure and volume trend but were blocked from INTERESTING solely by V/L.

### 5. No output when LLM sees 0 tokens

Samples 3, 4, 5 had only 1 fresh token each. Sample 6 had 1 fresh token that was SKIP — so the response was just "1. TOKEN — SKIP — ...". The dashboard handles this correctly (no cards shown), but there's no signal to the user that the scanner effectively found nothing to work with this cycle.

---

## Suggested Improvements

### High impact

**A. Token deduplication across scans with age tracking**

Track which tokens have already been surfaced and their first-seen rating. On repeat appearances:
- Show a "seen Xm ago" tag on the card
- Suppress repeats that haven't changed rating
- Highlight rating changes (e.g., INTERESTING → WATCH is a sell signal)

This alone would significantly reduce noise for active users.

**B. Push notification on INTERESTING**

Add an n8n webhook step that POSTs to a Telegram bot or Discord when any token scores INTERESTING. The current model requires manual page refreshes to catch a signal. With a 5-minute polling interval, the average alert delay is 2.5 minutes — acceptable for a notification but unacceptable if you're watching manually.

**C. "Dead batch" fast-path**

If >80% of the batch is stale and ≤1 token reaches the LLM, skip the LLM call entirely and return a minimal response: "No fresh tokens this cycle — X stale." Saves 25–30s of scan time and model load on cycles that can't produce actionable output.

### Medium impact

**D. Widen the V/L upper bound or make it soft**

The 8× ceiling is blocking otherwise strong tokens frequently. Consider raising to 12× or treating it as a soft signal (WATCH, not INTERESTING ineligibility) when all other signals are green. Would require A/B testing against outcome data.

**E. Stale rate trend in header**

Show the stale % explicitly: "6 pre-filtered (86% of batch — feed is stale)". This tells the user whether the session is worth watching without needing to understand the raw counts.

**F. Token age on cards**

Cards show "Age: 20m" which is the token's launch age at scan time. Add a "first seen" timestamp so users know if a WATCH token is freshly appearing vs. been sitting in the feed for 4 scans.

### Lower impact / future

**G. DexScreener supplemental feed**

Consider also polling the chain-specific endpoints (`/latest/dex/tokens/solana`, `/latest/dex/tokens/base`) which may surface different or fresher tokens than the profile endpoint. Could increase the fresh token pool during slow periods.

**H. Outcome tracking stub**

No way to know whether INTERESTING calls were correct. Even a simple log of "INTERESTING at time T, price P" with a cron job checking price at T+30m would let you measure model accuracy over time. Currently the eval was on format/reasoning quality only — not on whether the trades worked.

---

## Model Performance Assessment

The Qwen3.6-35B-A3B (thinking on) performed cleanly across all 12 samples:

- **Format compliance:** 12/12 samples had correctly structured output
- **Constraint adherence:** No observed violations (no scenarios on SKIP, no INTERESTING on 2+ failures)
- **Reasoning quality:** Consistently specific — cited exact percentages, named signal failures by type, caught the MOGCOIN momentum decay transition correctly
- **Latency:** Acceptable at 24–51s end-to-end. Never approached the 180s timeout.

The thinking overhead (~1700 tokens) appears to add qualitative value for edge cases. MOGCOIN's downgrade from INTERESTING to WATCH on sample 11 was a nuanced call that required weighing a single V/L deviation against otherwise strong signals — the kind of judgment call where thinking likely helped.

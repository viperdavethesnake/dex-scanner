# DEX Scanner — 1-Hour Observation Report (Run 2)

**Date:** 2026-05-02  
**Window:** 17:02 – 18:04 ET  
**Samples:** 12 (one every 5 minutes)  
**Model:** Qwen3.6-35B-A3B Q6_K, thinking on  
**Reference:** Compare to Run 1 (15:57–17:00 ET)

---

## Summary

| Metric | Run 2 (17:02–18:04) | Run 1 (15:57–17:00) | Delta |
|--------|---------------------|---------------------|-------|
| Total launches | 87 | 89 | −2 |
| Sent to LLM | 43 (49%) | 32 (36%) | +13 |
| Stale (>90 min) | 44 (53%) | 57 (64%) | −11 pts |
| INTERESTING | 3 (7%) | 7 (22%) | −15 pts |
| WATCH | 13 (30%) | 9 (28%) | +2 pts |
| SKIP | 27 (63%) | 16 (50%) | +13 pts |
| Scan latency avg | 38s | 35s | +3s |
| Scan latency max | 51s | 51s | same |
| Zero-launch samples | 1 | 0 | new |

---

## Per-Sample Breakdown

| # | Time | Launches | INTER | WATCH | SKIP | Stale | Stale% | Elapsed |
|---|------|----------|-------|-------|------|-------|--------|---------|
| 01 | 17:02 | 8 | 0 | 2 | 2 | 4 | 50% | 41s |
| 02 | 17:07 | 7 | 0 | 2 | 2 | 3 | 42% | 33s |
| 03 | 17:13 | 8 | 0 | 3 | 2 | 3 | 37% | 49s |
| 04 | 17:19 | **0** | 0 | 0 | 0 | 0 | — | 10s |
| 05 | 17:24 | 6 | 0 | 2 | 1 | 3 | 50% | 40s |
| 06 | 17:29 | 9 | 1 | 1 | 2 | 5 | 55% | 41s |
| 07 | 17:35 | 9 | 0 | 0 | 4 | 5 | 55% | 51s |
| 08 | 17:41 | 9 | 1 | 0 | 3 | 5 | 55% | 49s |
| 09 | 17:47 | 9 | 0 | 2 | 2 | 5 | 55% | 45s |
| 10 | 17:53 | 9 | 0 | 1 | 3 | 5 | 55% | 29s |
| 11 | 17:58 | 8 | 0 | 0 | 4 | 4 | 50% | 29s |
| 12 | 18:04 | 5 | 1 | 0 | 2 | 2 | 40% | 47s |

---

## Notable Tokens

### MOGCOIN — carried over from Run 1, faded out by sample 3

Still present at 17:02 (WATCH, "strong 65% buy pressure, upward micro") and 17:07 (WATCH, "potential stabilization"), then appeared once more at 17:13 (WATCH, "70% buy pressure, declining micro"). Gone by 17:19. The model tracked its continued decay correctly — it never recovered INTERESTING status in this window.

MOGCOIN appeared in 6 Run 1 samples as INTERESTING, transitioned to WATCH across Run 1's final samples, and carried its degraded status into Run 2 before dropping out. Total run: ~80 minutes visible in the feed.

### BUTT — persistent WATCH, 5 samples (17:02–17:35)

Same V/L overshoot pattern as MOGCOIN. Strong buy pressure (52–56%) but volume/liquidity ratio too high each time. Never cleared the threshold, never broke down. Exactly the kind of token the deduplication enhancement would suppress — same card, same reasoning, five scans in a row.

### CATHOLIC — 3 samples (17:13–17:47), always WATCH

Appeared with mixed signals each time: rising volume and high buy pressure, but either fading micro or a 5m pullback after a 6h run. The model held it at WATCH across all three appearances, citing consistent reasoning. By sample 9 (17:47): "falling volume trend contradicts the rising volume requirement for INTERESTING, but micro uptrend and buy pressure above 52% suggest a potential scalp setup." It was downgraded and gone by 17:53.

### BUTTCRACK — 2 samples (17:47, 17:53), WATCH

New token, single V/L soft failure, strong buy pressure. Brief appearance, not enough data to assess outcome.

### BREASTCOIN — appeared in sample 6 as WATCH, sample 8 as INTERESTING

The one token that demonstrated healthy progression: appeared at 17:29 as WATCH ("low liquidity creating slippage risk"), then returned at 17:41 as INTERESTING with "rising volume +170%, 61% buy pressure, recovering micro trend." Scenarios: Aggressive entry $X, target +18%, stop −10%. This is the intended workflow — token matures, signals strengthen, model upgrades rating.

### catholic (lowercase) — sample 12 INTERESTING

Late-session token, strong momentum: "+10.59% 5m, 72% buy pressure, upward micro." Only failure was falling volume which the model judged overridden by the buy pressure and price action. Aggressive scenario: entry $0.0004424, target $0.0005220 (+18%). Last scan of the hour — no follow-up data.

---

## New Finding: Zero-Launch Sample (17:19)

Sample 4 returned 0 launches in 10 seconds. DexScreener's `/token-profiles/latest/v1` returned an empty or unparseable payload. The pipeline handled it gracefully (no crash, no n8n error), and the HTML response was minimal. However:

- The user sees a blank page with "0 launches" — no indication whether this is a market lull, an API failure, or a transient error
- No retry logic exists — a single API hiccup produces a dead scan
- This is a new failure mode not observed in Run 1

---

## Key Observations vs. Run 1

### Market was colder / noisier in Run 2

INTERESTING dropped from 22% → 7% of rated tokens. SKIP increased from 50% → 63%. This isn't a model issue — the reasoning quality remained high and consistent. The market simply produced fewer tokens with clean momentum profiles during the 17:00–18:00 ET window.

### SKIP failure patterns (run 2)

Most common reasons the model rejected tokens:

| Failure | Count |
|---------|-------|
| Micro trend down | 11 |
| Falling volume | 7 |
| 1h price drop | 6 |
| Low buy pressure (<52%) | 4 |
| Low liquidity (<$15k) | 3 |

Micro trend down is the dominant disqualifier in a cooling market — tokens that launched with momentum but are now fading.

### V/L ratio is still the main WATCH bottleneck

5 of 13 WATCH tokens were held at WATCH solely due to V/L overshoot (>8×), with all other signals green. Same pattern as Run 1. This threshold is worth revisiting — it's blocking otherwise actionable signals repeatedly across both hours.

### Stale rate improved but stabilized at ~55% mid-session

Run 2 averaged 53% stale vs 64% in Run 1. The improvement came from the 17:02–17:20 window where fresh tokens were available. By 17:29, stale rate locked at 55% for six consecutive samples — the DexScreener feed appears to have a natural "settled" state where approximately half the profiles are over 90 minutes old.

---

## Gaps and Issues

### New: Zero-launch API response (sample 4)

The pipeline has no handling for an empty DexScreener response. Current behavior: scan completes in 10s with "0 launches" shown. Needs:
- A distinction between "0 fresh tokens after filtering" vs "API returned nothing"
- At minimum, a different header message ("API returned empty batch — retry in 1 minute")

### Confirmed: No cross-scan deduplication

BUTT appeared as WATCH in 5 consecutive samples with identical reasoning. MOGCOIN bridged two full hours across both runs. A user watching the dashboard sees the same cards cycle through with no staleness indicator.

### Confirmed: WATCH-dominant scans produce no actionable cards

Samples 1, 2, 3, 5 each had 2–3 WATCH tokens but 0 INTERESTING — meaning no cards were shown (WATCH cards are shown, actually, per the pipeline). Wait — WATCH cards *are* shown in the output. So those scans did display cards. But without a notification system, a user would need to be actively watching to catch them.

### Model latency is consistent but slightly higher in complex scans

Samples with 4 SKIP tokens (7, 11) hit 29–51s. Samples 3 and 8 with richer mixed batches hit 49s. The range is acceptable but hints that heavier SKIP batches — where the model must reason through multiple failures per token — push toward the upper bound.

---

## Suggested Improvements (updated after two hours)

### Confirmed from Run 1 — still needed

**A. Cross-scan deduplication with age/streak tracking**
MOGCOIN ran 80+ minutes across both observation windows. Tokens that persist that long need: first-seen timestamp, appearance count, and rating trend. The card should show "WATCH for 35m | was INTERESTING" not just the current rating.

**B. Push notification on INTERESTING**
Three INTERESTING signals in 60 minutes, spread across samples 6, 8, and 12. None aligned with each other — a user refreshing manually at 5-minute intervals would likely miss at least one.

**C. Dead/empty batch handling**
Two cases now: the zero-launch API response, and the "1 token, all SKIP" quasi-dead scan from Run 1. Both need explicit messaging and should probably skip the LLM entirely.

### New from Run 2

**D. Retry on empty DexScreener response**
One retry with a 10-second delay before declaring a zero-launch scan would recover transient API failures without user impact. The 180s webhook timeout has plenty of headroom.

**E. V/L upper bound review**
Two hours of data confirm V/L > 8× is the dominant WATCH reason. Raising the ceiling to 10–12× or reclassifying it as soft-only (never blocks INTERESTING alone) would surface more actionable signals. Risk: higher V/L often does indicate wash trading — would need outcome tracking to validate.

**F. Time-of-day awareness in header**
The scanner operates differently at 4 PM ET vs 5–6 PM ET. Showing the current UTC or ET time prominently (it does show a timestamp) is fine — but a note like "typically slower 17:00–20:00 ET" would help users calibrate expectations. Low-effort, useful context.

**G. INTERESTING progression tracking**
BREASTCOIN's WATCH → INTERESTING arc (samples 6→8) is exactly the kind of signal worth surfacing explicitly. A "↑ upgraded from WATCH" badge would distinguish fresh promotions from tokens that have been INTERESTING for 40 minutes.

---

## Two-Run Summary

Across 24 samples and ~2 hours of live market data:

- **174 total token launches processed**
- **10 INTERESTING signals** (across both runs combined)
- **22 WATCH signals**
- **43 SKIP**
- **101 stale** — 58% of all launches never reached the LLM
- **Avg scan latency: 36s end-to-end**

The model performs cleanly and consistently. The pipeline's core limitation is the data layer: the DexScreener feed is stale more than half the time, and there is no memory between scans. Those two gaps — feed freshness and cross-scan state — are the highest-leverage improvements available.

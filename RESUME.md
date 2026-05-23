# DEX Scanner — Resume

**Last updated:** 2026-05-17 (end of session)

---

## Bring it back up

The stack now has two independent groups. Start them separately.

### Collector only (no GPU needed)
```bash
cd /space/docker/containers/dex-scanner
docker compose up -d dex-collector-db dex-collector
docker logs dex-collector -f   # confirm polling
```

### Scanner stack (GPU required)
```bash
docker compose up -d llamacpp timescaledb n8n
curl http://192.168.33.231:8080/health   # wait for {"status":"ok"} before scanning
```
Model load takes ~2 minutes. n8n won't start until llama-server is healthy.

### Stop independently
```bash
# Stop scanner (free GPU), keep collector running
docker compose stop n8n timescaledb llamacpp

# Stop collector
docker compose stop dex-collector dex-collector-db
```

---

## Current state

### Stack

| Service | Status | Notes |
|---------|--------|-------|
| `dex-collector-db` | **running** | Bridge network, port 5434 |
| `dex-collector` | **running** | Polling every 5 min, GPU-independent |
| `dex-llamacpp` | stopped | GPU free |
| `dex-timescale` | stopped | |
| `dex-n8n` | stopped | Auto-scanner was stopped before shutdown |

### Workflows

| ID | Name | Active |
|----|------|--------|
| `bZ7P0LR4SML0MUv6` | DEX Scanner — Base & Solana | always on |
| `svREuu5gTMgumndn` | DEX Auto-Scanner | **ON** — runs every 5 min |
| `2MFJc5cEvhQZNDlc` | DEX Scan Control | always on |
| `3lSEjGrScilFstmS` | DEX Outcome Tracker | always on |
| `uTQ0gfzDS1gf8bDu` | DEX Birdeye Enricher | always on |
| `GwTtxU5MgTEoeqHK` | DEX Status | always on |

Auto-scanner resumes automatically on `docker compose up -d`. To run manual scans only:
```
http://192.168.33.231:5678/webhook/dex-scan-control?action=stop
```

### Model

`Qwen_Qwen3.6-35B-A3B-Q6_K.gguf` — selected by eval (Run E, 2026-05-02). Thinking enabled, `max_tokens: 4096`. See `eval/RESULTS.md` for full eval record.

### DB state (as of 2026-05-17)

- 2,228 total signals, 15 days of data (May 3–17)
- Last scan: 2026-05-17 ~10:41 AM PDT

### Querying DB with stack down

Data is a bind mount at `./timescale_data`. Start a one-off container:
```bash
docker run --rm \
  -v /space/docker/containers/dex-scanner/timescale_data:/var/lib/postgresql/data \
  timescale/timescaledb:latest-pg16 \
  psql -U dex -d dex_signals -c "SELECT COUNT(*) FROM token_signals;"
```
No model load required. Works as long as the stack is fully stopped (no lock conflict).

---

## What's built

### Phase 1 — Core scanner (completed 2026-05-02)
- DexScreener-based scan pipeline: profiles → pair data → signals → safety checks → LLM scoring → HTML response
- Model eval completed: 5 models × 2 fixtures. Winner: Qwen3.6-35B-A3B, thinking on, 30–34s wall clock, 26/30 soft score.
- Auto-scanner, Scan Control, Status page workflows added.

### Phase 2 — TimescaleDB + Birdeye enrichment (completed 2026-05-03)
- TimescaleDB (`dex-timescale`) added to stack. Stores `token_signals` and `scan_summary`.
- Outcome Tracker workflow backfills price_at_5m/15m/30m, target_hit, stop_hit.
- Birdeye Enricher backfills `unique_traders_1h` + `net_inflow_usd` every 2 min for both Base and Solana (enricher is chain-aware; `x-chain` header set dynamically).
- DB schema: `init.sql`, 33 columns in `token_signals`.

### Phase 3 — Outcome tracker bug fix (completed 2026-05-11)
- Fixed 3 compounding bugs in DEX Outcome Tracker (`3lSEjGrScilFstmS`):
  1. **2-hour window** — tokens aged out before 15m/30m slots could be filled. Extended to 48 hours.
  2. **ORDER BY DESC** — new 5m-pending records crowded out older 15m/30m-pending. Fixed: records with 5m already filled (needing 15m/30m) prioritised first, newest-first within group.
  3. **No continueOnFail on HTTP node** — parallel DexScreener calls triggered rate limits, silently dropping items. Added `continueOnFail`.
- Backfilled 50 fresh 15m records via one-shot serial script. `has_15m`: 40 → 87.

### Phase 4 — Filter tightening + chain badges (completed 2026-05-14)
- **V/L ceiling lowered 12× → 8×** (Build Prompt node). Data across 490 signals: 8–12× avg -19% at 5m, 12×+ avg -44% at 15m — both net-negative at every horizon. This also materially improves Solana's expected 5m performance (estimated -3.6% → +1.7% by removing the high-V/L drag).
- **Chain badges added to token cards** — BASE shown in blue, SOLANA in purple. Immediately visible on every card.
- **Birdeye "Solana-only" myth corrected** — enricher was always chain-aware. Base coverage 98.8%, Solana 87.9%. Both chains have valid trader/inflow data feeding the LLM.
- **Empirical observations documented** in `DEX-SCANNER.md`: V/L bucket performance table, chain gap table, hold-duration trap (gains reverse sharply at 15m+ even in the best V/L bucket).

### Phase 7 — Meme coin intelligence reframe (completed 2026-05-16)

Complete rewrite of Safety Filter and Build Prompt based on how experienced meme coin traders actually validate tokens.

**Safety Filter — new hard fails (previously undetected):**
- `HIDDEN_OWNER` — hidden contract ownership
- `CAN_RECLAIM_OWNERSHIP` — owner can reclaim contract
- `CREATOR_HONEYPOT_HISTORY` — creator wallet has honeypot history
- `WHALE_CONCENTRATION` — single wallet >40% supply

**Safety Filter — new enrichment fields passed to LLM (previously discarded):**
- `holderCount` — actual total holders (not just a flag)
- `top1Pct`, `top5Pct` — top holder concentration %
- `creatorPct`, `creatorBalance` — is creator still holding?
- `lpLocked`, `lpBurned` — LP safety status
- `lpProviderCount` — how distributed is the liquidity?
- `insiderCount`, `insiderNetworkSummary` — RugCheck graph insider detection
- `launchpad` — Pump.Fun vs other platform (Solana)
- `rcScore` — full RugCheck score, not just binary flag
- `topHolderLines` — top 5 holders with insider flags

**LLM prompt rewrite — new job:**
- Was: re-evaluate the same signals as SKIP/WATCH/INTERESTING
- Now: meme coin conviction analyst — lifecycle stage, organic vs manufactured, conviction sizing ($50? $500? nothing?)
- New format: `N. SYMBOL — RATING — [conviction: $X] — one sentence`
- Context added for: Pump.Fun graduation dynamics, insider/cabal patterns, holder velocity, LP burn, creator wallet behavior

### Phase 6 — Data-driven filter tightening (completed 2026-05-16)

Full DB analysis (2,148 signals, Python/GBM). Five changes pushed to `bZ7P0LR4SML0MUv6`:

1. **Pre-filter: min age 15 min** — 0–15m tokens avg -0.3%, 44% win rate. Excluded at pre-filter.
2. **Pre-filter: micro_trend `recovering` and `down`** — -17.4% and -6.6% avg, 26–32% win. Hard-filtered.
3. **Buy pressure floor 52% → 55%** — GBM partial dependence: likes 64%, hates 43%.
4. **Solana-specific V/L ceiling: 4x** (Base stays 8x) — Solana 4–6x avg -12.3%, Base 4–6x +18.7%.
5. **Base net_inflow >$20k flag** — Base >$20k: +25.4% avg, 67% win. Flagged prominently in LLM prompt.

Key findings from the analysis:
- **Base WATCH ≈ Base INTERESTING** (+20.2% vs +19.3% at 5m) — chain is everything for Base
- **LLM rating is the least predictive feature** in GBM (importance 0.004) — it recapitulates signals it sees
- **V/L filter change (May-14) worked dramatically**: Solana INTERESTING -2.1% → +22.4% post-filter
- **Unique traders negatively correlated** with outcome — crowded moves tend to fail
- GBM classifier at p≥0.65 threshold: 61.8% precision vs 42% base rate (1.47x lift)

### Phase 8 — V/L ceiling hard enforcement fix (completed 2026-05-16)

**Bug:** After Phase 7 renamed "NOT ELIGIBLE" to "SIGNAL WARNINGS" and rewrote the system prompt, the V/L ceiling became advisory — the LLM could override it. Confirmed live: 83% of Solana WATCH and 40% of Solana INTERESTING tokens violated the 4x ceiling. SCAMCOIN at V/L 19.9x rated INTERESTING.

**Fix:** V/L ceiling moved to hard pre-filter alongside age and micro_trend checks:
```javascript
const vlMax = item.json.chain === 'solana' ? 4.0 : 8.0;
return ageMin >= 15 && ageMin <= 90 && micro !== 'recovering' && micro !== 'down' && vl <= vlMax;
```
No token above the ceiling reaches the LLM. Redundant ceiling check removed from advisory block.

### V/L filter correction — non-linear Solana zones (completed 2026-05-17)

**Finding:** DB analysis (1,826 Solana pre-Phase-8 signals) revealed the Solana V/L danger zone is non-linear:
- 0–4x: +9.5% avg, ~48% win ✅
- 4–6x: **-12.3% avg, 32.1% win** ❌ (danger zone)
- 6–8x: **+6.1% avg, 62.7% win** ✅ (incorrectly filtered by the 4x ceiling)
- 8–12x: -13.5% avg, 32.5% win ❌
- 12x+: -4.4% avg, 35.2% win ❌

The flat 4x ceiling was blocking the 6–8x bucket which actually outperforms 0–4x in win rate.

**Fix:** Pre-filter logic updated to pass Solana 0–4x and 6–8x, block 4–6x and 8x+:
```javascript
const vlPass = chain === 'solana'
  ? (vl <= 4.0 || (vl > 6.0 && vl <= 8.0))
  : (vl <= 8.0);
```

### Phase 9 — Collector-driven Solana filter correction (completed 2026-05-23)

5 days of unbiased collector data (19,639 signals) revealed two errors in the Solana filter. Both fixed in `bZ7P0LR4SML0MUv6` Build Prompt node.

**Fix 1 — Revert Solana V/L 6–8x re-admission (was a mistake):**
- May-17 correction was based on biased scanner data (only tokens surviving LLM scoring)
- Collector data (unbiased): 6–8x is -1.42% avg, 43.9% win overall; -0.56% avg within filter-eligible set
- The 4–6x zone it was supposed to be worse than is actually +0.31%, 45.2% win
- Reverted to flat ≤4x ceiling for Solana

**Fix 2 — Add `flat` to Solana micro_trend exclusions:**
- Solana `flat` in filter-pass: n=662, -1.47% avg, 27.5% win — pure noise
- Now Solana excludes: `recovering`, `down`, `flat` (Base still only excludes `recovering` and `down`)
- Base `flat` not excluded: +1.55% avg, 28.7% win — low win rate but positive avg

**Combined projected impact:** Solana filter-pass avg +0.19% → +1.85%, win rate 39.7% → 48.4%

```javascript
// New filter (Solana-specific micro and V/L logic)
const vlPass = chain === 'solana' ? (vl <= 4.0) : (vl <= 8.0);
const microPass = chain === 'solana'
  ? (micro !== 'recovering' && micro !== 'down' && micro !== 'flat')
  : (micro !== 'recovering' && micro !== 'down');
return ageMin >= 15 && ageMin <= 90 && microPass && vlPass;
```

### Bug fix — Empty batch short-circuit (completed 2026-05-17)

**Bug:** When all tokens in a batch were pre-filtered (by the Phase 8 combined age/micro_trend/V/L check), Build Prompt still sent an empty token list to the LLM. With thinking enabled, the LLM burned ~30s then responded as a chatbot: "Ready. Drop the token list... Waiting on your data." Format Response then rendered this as the AI Analysis section.

**Fix — three changes to workflow `bZ7P0LR4SML0MUv6`:**
1. **Build Prompt:** when `freshItems.length === 0`, generate a clean "all pre-filtered" HTML page inline and return `{empty: true, html}` — LLM is never called, response is near-instant.
2. **LLM Analysis:** added `onError: continueRegularOutput` — needed so the `{empty: true}` item (which has no `system`/`user` fields) passes through to Format Response rather than hard-failing the workflow.
3. **Format Response:** added `buildNode.empty` check that reads from Build Prompt directly (via `$('Build Prompt').all()[0].json`) and returns the pre-built HTML immediately. Also fixed misleading ">90m" label — now shows "pre-filtered" since age is only one of three filter reasons.

---

### Phase 5 — DEX Deep Dive (completed 2026-05-15)
- **New standalone workflow** `O9P2SbCe0KE4ue9R` — on-demand in-depth AI analysis of a single token.
- **Trigger:** `http://192.168.33.231:5678/webhook/dex-deep-dive?token=<address>&chain=solana|base`
- **Data pipeline:** DexScreener full pair data → Birdeye overview + recent trades → GoPlus full security → RugCheck (Solana) → Honeypot.is (Base)
- **LLM prompt:** 6-section conviction analysis — Volume Authenticity, Momentum Quality, Security Verdict, Chain Fit, Conviction Verdict (ENTER/WAIT/PASS with specific levels), Invalidation Scenarios. Thinking enabled, max_tokens 6144.
- **Output:** Dark-themed HTML page with stat cards + full analysis. ~50–90s response time.
- **Deep Dive link added to scanner cards** — purple button after Solscan on every token card. One click from the scan page triggers the deep dive in a new tab.
- **Free tier note:** Birdeye trade history works; wallet/inflow data returns N/A (paid tier gates those endpoints). GoPlus + RugCheck fully functional.

---

## n8n API key

JWT stored at `keys/n8n-api-key.txt`. **Expires 2026-06-09** — no action needed until then.

---

## dex-collector (new — 2026-05-17)

Dedicated data collection service, GPU-independent. Runs on its own bridge network (`dex-collector-net`) with its own TimescaleDB (`dex-collector-db`, port 5434).

**What it does:** Polls DexScreener every 5 min, computes the same signals as the scanner, records filter pass/fail + reason for EVERY token (not just survivors), backfills 5m outcomes.

**DB:** `psql -h 192.168.33.231 -p 5434 -U collector -d collector_signals`

**Files:** `collector/` directory — `main.py`, `api.py`, `signals.py`, `db.py`, `init.sql`, `Dockerfile`

**Bring up independently (no GPU needed):**
```bash
docker compose up -d dex-collector-db dex-collector
docker logs dex-collector -f
```

**Key query after 1 week of data:**
```sql
SELECT micro_trend, filter_pass, COUNT(*) n, AVG(outcome_pct) avg_5m
FROM raw_signals WHERE price_at_5m IS NOT NULL
GROUP BY 1, 2 ORDER BY 1, 2;
```

---

## Next session

- Continue collecting data — collector has 19,639 signals (May 17–23), well past targets
- Let Phase 9 filters accumulate data to validate improvement (target: 500+ post-Phase-9 Solana signals with 5m)
- Remaining candidates (need net_inflow_usd from Birdeye — not in collector):
  1. **Base hard pre-filter: net_inflow_usd < $5k → drop** — 9% win rate, -13.3% avg (needs scanner data)
  2. **LLM prompt: flag Base rising vol_trend prominently** — +7.22% avg vs +3.53% overall
- Cancelled candidates (data disproved or not worth it):
  - ~~Solana buy_pct_5m > 75%~~ — 75–85% is fine (+2.31%, 43.5% win); only >85% is bad (n=61, too small)
  - ~~Age floor 15→20m~~ — Base 15–20m is +18.82%, 64.9% win (best bucket — do NOT filter it)
- Check conviction sizing calibration once Phase 7 signals have 5m outcomes

---

## Data insights (19,639 collector signals, May 17–23)

- **Base outperforms Solana at all horizons:** Base overall +3.53% avg, 44.1% win. Solana -1.00% avg, 40.5% win.
- **Current filter (Phase 8+9):** Base pass +5.27%, 48.3% win (n=2,029). Solana pass pre-Phase-9: +0.19%, 39.7% win.
- **Solana V/L — flat ceiling ≤4x is correct:** 6–8x bucket is -1.42%, 43.9% — the re-admission (May-17) made things worse. Reverted 2026-05-23.
- **Solana `flat` micro_trend is noise:** n=662 filter-pass tokens at -1.47% avg, 27.5% win. Now excluded for Solana. Filtered 2026-05-23.
- **Projected Phase 9 Solana improvement:** +0.19% → +1.85% avg, 39.7% → 48.4% win rate (based on collector data).
- **Hold-duration trap:** Still applies — 5m events, not holds. Best bucket (Base up) turns negative at 15m+.
- **LLM rating is not predictive** (GBM importance 0.004) — the chain matters far more than the LLM's rating.

---

## Status page

`http://192.168.33.231:5678/webhook/dex-status` — scan history + signal log, auto-refreshes 60s.

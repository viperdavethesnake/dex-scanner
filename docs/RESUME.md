# DEX Scanner — Resume

**Last updated:** 2026-05-30 (session 8 — intake-gap diagnostic; report pending at ~06:20 UTC)

---

## Bring it back up

The stack has three independent groups. Start them separately.

### Collector + Trader (no GPU needed)
```bash
cd /space/docker/containers/dex-scanner
docker compose up -d dex-collector-db dex-collector dex-trader-db dex-trader
docker logs dex-collector -f    # confirm polling
docker logs dex-trader -f       # confirm shadow loop running
```

### Scanner stack (GPU required)
```bash
docker compose up -d llamacpp timescaledb n8n
curl http://192.168.33.231:8080/health   # wait for {"status":"ok"} before scanning
```
Model load takes ~2 minutes. n8n won't start until llama-server is healthy.

### Stop independently
```bash
# Stop scanner (free GPU), keep collector + trader running
docker compose stop n8n timescaledb llamacpp

# Stop trader
docker compose stop dex-trader dex-trader-db

# Stop collector
docker compose stop dex-collector dex-collector-db
```

---

## Current state

### Stack

| Service | Status | Notes |
|---------|--------|-------|
| `dex-llamacpp` | **stopped** | GPU free |
| `dex-timescale` | **running** | |
| `dex-n8n` | **stopped** | |
| `dex-collector-db` | **running** | port 5434 |
| `dex-collector` | **running** | polling every 5 min |
| `dex-trader-db` | **running** | port 5435 |
| `dex-trader` | **running** | shadow mode, drift gate v2 |

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

### Shadow Trader — Phase 3 (completed + live 2026-05-25)

Full shadow-mode trading service (`dex-trader/`) scaffolded, hardened, and started live.

**Architecture:**
- Single-process poll loop (5s interval) — ingests `raw_signals` from collector DB, scores with LightGBM, gets aggregator quotes, records simulated fills and P&L
- Two-threshold conviction: `SHADOW=0.65` (entry floor), `LIVE=0.70` (band classifier)
- Exit timer: 5-minute hold, then aggregator exit quote + DexScreener parallel truth
- All risk controls active in shadow: position limit (3), daily loss cap ($50), re-entry lockout (30m), hourly rate limit (20), kill switch

**Services added to compose.yaml:**
- `dex-trader-db` — TimescaleDB, port 5435, `dex-trader-net`
- `dex-trader` — builds from repo root, dual-network (trader + collector)

**Key files:**
```
dex-trader/           main.py, db.py, signals.py, scorer.py, security.py
                      eth_price.py, simulator.py, risk.py, token_decimals.py
dex-trader/aggregators/   __init__.py, types.py, zerox.py, aerodrome.py,
                          uniswap.py, jupiter.py (stub)
dex-trader/init.sql   trades hypertable, trader_state, signal_watermark
analysis/features.py  Shared feature engineering — single source of truth
```

**13 hardening fixes shipped before first start (P0/P1 + P2):**
- Correct token decimals (on-chain ERC20 lookup, two-level cache)
- Per-token `price_usd` in sell direction (was total USDC received)
- `cost_delta_pct = (backtest_gross - net_pct) - 1.5` captures full round-trip friction
- `slippage_bps` from `minBuyAmount/buyAmount` (0x v2 field, not v1 `guaranteedPrice`)
- Route label from `route.fills` (0x v2 structure, not v1 `sources[]`)
- Slippage gate for on-chain fallbacks (signal_price vs quote_price)
- `record_entry()` called only after confirmed fill (not on quote/security failure)
- Exits fire before ingest each loop cycle
- `signal_features` JSONB stored per trade row
- Web3 reconnect helper with 3-failure threshold + 30s backoff
- Shared `analysis/features.py` — `engineer_features()` verified bit-identical (13/13 features, <1e-12)
- Kill-switch read once per cycle, passed through to avoid redundant DB queries
- `libgomp1` added to Dockerfile (LightGBM runtime dep missing from `python:3.12-slim`)

**Startup verification (2026-05-25 08:31 UTC):** all required log lines present, health endpoint `{"status":"ok", "shadow_mode":true}`, watermark advancing, zero restarts.

**Four bugs found and fixed (session 4 — 2026-05-25):**

The trader ran from 08:31→19:26 UTC (11h) with zero successful scores. All four bugs were stacked: any signal surviving hard_filter would crash at scorer.py before a trade row could be created.

| # | Commit | Bug | Root cause |
|---|--------|-----|-----------|
| 1 | `1ea05bd` | `ValueError: pandas dtype must be int/float/bool` | psycopg2 returns `NUMERIC` as `Decimal`; pandas infers `object` dtype; LightGBM rejects it |
| 2 | `e8e8ac5` | `TypeError: datetime not JSON serializable` | `scanned_at`/`pair_created_at` datetime fields from collector row passed to `psycopg2.extras.Json()` |
| 3 | `deb2ab4` | `fill_price_usd = 0` on every fill | `data.get("price", 0)` — 0x v2 has no `"price"` field; price must be derived from `buyAmount + tok_dec` |
| 4 | `8ec282b` | Silent categorical miscoding | `.astype("category")` on single-row inference derives codes from that one row. `micro_trend="up"` got code 0 (="down" at training) — every score was wrong |

**v0.3 tag context:** `v0.3` (`372ba32`) marks the architectural milestone — service scaffolded, all 13 hardening fixes in, first startup verified. It is a **pre-discovery snapshot** — no real scoring ever succeeded under that tag. Real shadow data starts at commit `8ec282b`, container restart 19:26 UTC 2026-05-25.

**Do not re-tag v0.3.** Tag `v0.3.1` once 24 hours of clean data have accumulated in the trades table.

**Dirty trade:** `trade_id=1` (GEODE, exited) has `fill_price_usd=0` (pre-fix zero). `gross_pct=0, net_pct=0`. The exit quote was real (`exit_price_usd=$0.000001652`) but P&L is uncomputable. Ignore this row in analysis — all subsequent rows are clean.

**Model re-exported after categorical fix:**
- `trained_at: 2026-05-25T19:25:48Z`
- `val_auc: 0.589` (was 0.620 — different val window, not a regression; 0.620 was honest math on wrong scores, 0.589 is honest math on correct scores)
- `precision@0.65: 56.5%`, `precision@0.70: 59.2%`
- `categorical_mappings` now in `metadata.json` — `micro_trend` 5 values, `vol_trend` 4 values
- Note: `dex` has only 1 trained category (`uniswap`) — effectively a constant, zero feature importance. Drop from model on next retrain.

### Session 7 — Health checks + idle-in-transaction deadlock fix (completed 2026-05-28)

**Health checks added to all services** — previously only `dex-llamacpp`, `dex-n8n`, `dex-trader` had them:

| Service | Check |
|---------|-------|
| `dex-timescale` | `pg_isready -U dex -d dex_signals` |
| `dex-collector-db` | `pg_isready -U collector -d collector_signals` |
| `dex-trader-db` | `pg_isready -U trader -d trader` |
| `dex-collector` | Heartbeat file `/tmp/heartbeat` — touched each poll cycle; checked for age <660s |

All `depends_on` conditions upgraded from `service_started` to `service_healthy`. CDI device syntax on `dex-llamacpp` corrected to short form (`- nvidia.com/gpu=all`).

**Idle-in-transaction deadlock fixed** — `migrate()` in `collector/db.py` was blocked on startup whenever the trader was running:

- Root cause: `collector/db.py:fetch_pending_outcomes()` and `dex-trader/main.py:_ingest_signals()` both ran `SELECT` queries on `collector_signals` with `autocommit=False` and never committed. The connection sat `idle in transaction` for the full 300s sleep interval. On collector restart, the next `ALTER TABLE ADD COLUMN` (DDL needs AccessExclusiveLock) blocked permanently — the trader's periodic re-reads kept resetting the idle timer, so the 60s DB timeout never fired.
- Fix 1: `conn.commit()` added after the SELECT in both functions.
- Fix 2: `idle_in_transaction_session_timeout=60000` added to `dex-collector-db` command as a safety net.

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

JWT stored in `.env` as `N8N_JWT`. **Expires 2026-06-20** — no action needed until then.

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

### Stack state at close (2026-05-28, session 7)

All 7 services running and healthy. GPU active.

| Service | Status | Notes |
|---------|--------|-------|
| `dex-llamacpp` | **running** | GPU active (RTX 8000) — stop to free GPU: `docker compose stop n8n timescaledb llamacpp` |
| `dex-timescale` | **running** | |
| `dex-n8n` | **running** | auto-scanner running |
| `dex-collector-db` | **running** | port 5434 |
| `dex-collector` | **running** | Birdeye enrichment ENABLED at rate=0.02 |
| `dex-trader-db` | **running** | port 5435 |
| `dex-trader` | **running** | shadow mode, drift gate v2 |

### Shadow trader health (2026-05-30 session 8)

- `kill_switch = false`, `shadow_mode = true`
- `trades` table: **120 total** (38 exited at **-8.52% avg net**, 82 skipped). Legacy trades 1–62 under old gate. v2 gate epoch: id=63+.
- **Drift gate v2** live since restart 14:53 UTC 2026-05-27.
- Still well below ≥200 exit target before drawing P&L conclusions.
- No stop-loss exists — two rugs dominate losses. Top pending improvement.

Health endpoint: `docker exec dex-trader curl -s http://localhost:8090/health`

### Gate v2 baseline query (run next session to see first batch under new logic)
```sql
-- New trades under drift gate v2 only
SELECT 
  status,
  failure_reason,
  COUNT(*),
  ROUND(AVG(net_pct)::numeric, 2) avg_net
FROM trades
WHERE id > 62
GROUP BY status, failure_reason
ORDER BY status, count DESC;

-- Gate decision breakdown (v2 era)
SELECT
  COUNT(*) FILTER (WHERE failure_reason LIKE 'momentum_failed%') AS momentum_failed,
  COUNT(*) FILTER (WHERE failure_reason LIKE 'drift_too_high%')  AS drift_too_high,
  COUNT(*) FILTER (WHERE status = 'exited')                      AS exited,
  ROUND(AVG(net_pct) FILTER (WHERE status='exited')::numeric, 2) AS avg_net_pct
FROM trades WHERE id > 62;
```

### Kill switch (emergency stop)
```bash
docker exec dex-trader-db psql -U trader -d trader \
  -c "UPDATE trader_state SET value='true' WHERE key='kill_switch';"
```

### Phase 4 checkpoint queries (run after ≥200 trades exited)
```sql
-- Overall P&L vs backtest assumption
SELECT COUNT(*) n,
       ROUND(AVG(net_pct)::numeric,2)         avg_net_pct,
       ROUND(AVG(cost_delta_pct)::numeric,2)  avg_cost_delta,
       ROUND(AVG(entry_cost_pct)::numeric,2)  avg_entry_cost
FROM trades WHERE status='exited';

-- conviction_band breakdown
SELECT conviction_band, COUNT(*), ROUND(AVG(net_pct)::numeric,2) avg_net
FROM trades WHERE status='exited' GROUP BY conviction_band ORDER BY 1;

-- Quote source coverage (who is actually filling)
SELECT quote_source, COUNT(*) FROM trades WHERE fill_ts IS NOT NULL GROUP BY 1;

-- Skip reason breakdown (includes legacy quote_drift% and new v2 reasons)
SELECT failure_reason, COUNT(*)
FROM trades WHERE status='skipped' GROUP BY 1 ORDER BY 2 DESC;

-- Drift gate v2 breakdown (trades from 2026-05-27 onwards)
SELECT
  COUNT(*) FILTER (WHERE status='skipped' AND failure_reason LIKE 'quote_drift%')    AS legacy_quote_drift,
  COUNT(*) FILTER (WHERE status='skipped' AND failure_reason LIKE 'momentum_failed%') AS momentum_failed,
  COUNT(*) FILTER (WHERE status='skipped' AND failure_reason LIKE 'drift_too_high%')  AS drift_too_high
FROM trades;
```

### Pending work

**Intake gap — action required (session 8 finding, 2026-05-30):**
- DexScreener `/token-profiles` captures only ~6% of Base on-chain launches. Training corpus has severe survivorship bias.
- Full diagnostic: `analysis/intake-gap-diagnostic-2026-05-30.md` (written at session 8 close)
- Decision doc: `docs/decisions/INTAKE-GAP-2026-05-30.md`
- **Immediate free win:** add `token_profiles_updates/recent-updates/v1` to collector poller union — 28 Base tokens/7 min vs 1 from `token_profiles`. Zero cost, ~1 day work.
- **Medium-term:** add Birdeye `/defi/v2/tokens/new_listing` — Base 144/hr with liquidity, Solana 324/hr. Requires new collector feed (~3-5 days).
- Do NOT use `search?q=base` for discovery — it is a name search, not a chain filter.

**Stop-loss for shadow trader (highest-value, blocked on data):**
- No stop-loss exists. Two rugs dominate losses. Implement -15% to -20% intra-hold check once ≥200 exits accumulated.

**Shadow trader — let it accumulate under drift gate v2:**
- Gate v2 epoch starts at id=63 (restart 14:53 UTC 2026-05-27). Trades 1–62 are legacy (old gate).
- Ignore trade id=1 (dirty fill_price=0).
- At 38 exits, target ≥200. At -8.52% avg net.
- Key things to watch: `drift_too_high` rate; `momentum_failed` rate; win rate should trend toward model's 56.5% precision.

**Data accumulation — just let it run:**
- Collector Birdeye enrichment at 100% Base sample rate, GoPlus both chains 100%.
- Phase 9 filters need more outcomes (target: 500+ post-Phase-9 Solana signals with 5m).

**Solana reset test — automatic:**
- Cron fires 2026-06-24 09:00 UTC → `analysis/SOLANA-RESET-TEST-20260624.md`.
- No action until then.

**Filter candidates — needs more enriched data first:**
1. **Base pre-filter: net_inflow_usd < $5k → drop** — 9% win, -13.3% avg. Re-run analysis once collector has ~1,000 enriched Base rows.
2. **LLM prompt: flag Base rising vol_trend** — +7.22% avg vs +3.53% overall. Low-effort prompt tweak when scanner is back up.

**Cancelled (data disproved):**
- ~~Solana buy_pct_5m > 75%~~ — only >85% is bad (n=61, too small to act on)
- ~~Age floor 15→20m~~ — Base 15–20m is +18.82%, 64.9% win, do NOT filter

**When Base auto-trading goes live (Phase 4+):**
- Upgrade Birdeye to Lite ($39/month) → Solana enrichment + faster responses.
- Bump `COLLECTOR_BIRDEYE_SAMPLE_RATE` to 0.2 (1.5M CU limit vs current 30k).

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

---

## Collector Birdeye enrichment (implemented + validated 2026-05-23)

`unique_traders_1h` and `net_inflow_usd` collected at insert time for Base tokens.

**Current state: ENABLED** (`COLLECTOR_BIRDEYE_ENRICHMENT=true` in `.env`)  
**Sample rate:** 100% (`COLLECTOR_BIRDEYE_SAMPLE_RATE=1.0`, raised from 0.02 in session 8 — GoPlus+Birdeye both at 100%, capped at 30 calls/cycle each)  
**CU budget:** check bds.birdeye.so dashboard — raised from 2% to 100% changes monthly estimate significantly.

### First-hour validation results (2026-05-23 ~13:00–17:00 UTC)

| Metric | Result | Target | Status |
|---|---|---|---|
| Chain | 100% base | 100% base | ✅ |
| HTTP 200 rate | 5/5 = 100% | >95% | ✅ |
| Failures | 0 | 0 | ✅ |
| avg response time | **826ms** | <500ms | ⚠️ note |
| CU header | not returned | — | Standard tier doesn't send it |

**826ms note:** Standard tier Birdeye runs slower than hoped but well within the 5s timeout. Not actionable — no failures, no risk.

**Sample counts (5 calls over ~42 cycles):** ~12% of cycles produce a call — matches math for 2% rate with 5–6 Base tokens per cycle.

### Sample enriched rows

| Symbol | unique_traders_1h | net_inflow_usd |
|---|---|---|
| CLUSTER | 712 | +$24,746 |
| GITBOOK | 214 | +$15,874 |
| lntentFi | 92 | −$2,901 |
| DEXTER | 0 | $0 |
| AT | 98 | +$424 |

### Validation query

```bash
docker exec dex-collector-db psql -U collector -d collector_signals -c "
SELECT chain, http_status, COUNT(*) calls,
       ROUND(AVG(response_ms)::numeric, 0) avg_ms, MAX(called_at)::time last_call
FROM birdeye_calls GROUP BY 1, 2;"
```

### To disable / rollback

```bash
# In .env: COLLECTOR_BIRDEYE_ENRICHMENT=false
docker compose up -d --no-deps dex-collector
```
No data loss — existing enriched rows kept, new rows insert with NULL Birdeye fields.

### Increase sample rate

From 0.02 → 0.03 only after verifying scanner-enricher CU on bds.birdeye.so is below 8,000/month.

### Solana reset test

Cron fires **2026-06-24 09:00 UTC** → `analysis/SOLANA-RESET-TEST-20260624.md`.  
If both SOL + BONK return HTTP 200, Solana was CU-exhaustion not tier-gated — enable Solana enrichment free.

---

## Repo history purge (2026-05-23)

**What happened:** Birdeye API key `129b43ea...` was hardcoded in `workflows/dex-birdeye-enricher.json` and `workflows/dex-deep-dive-workflow.json` and committed to the public GitHub repo. Discovered and rotated immediately.

**What was done:**
1. Key revoked at bds.birdeye.so — new key in `.env` (gitignored)
2. New key stored in n8n credential store (`Birdeye API`, ID `LWcHDmU166QRmUAv`) — workflow JSONs now reference it by ID, no raw value in any file
3. Full git history purged via orphan branch — repo now has a single clean initial commit (`98ebfd5`)
4. All old tags deleted from remote (`v1.0`, `v1.0-pre-major-changes`, `v6-filter-baseline`, `v9-ml-baseline`)
5. `gitleaks` pre-commit hook installed — blocks future secret commits
6. `.env.example` added documenting the secret contract
7. CLAUDE.md updated with secret-handling rules

**Backups (on host, not in repo):**
- `~/dex-scanner-backup-20260523-011254.tar.gz` — working tree snapshot
- `~/dex-scanner-remote-mirror-20260523-011307.git` — full old history mirror
- `~/dex-scanner-audit-logs/PHASE1-PREFLIGHT-REPORT-20260523.md` — full pre-purge audit

**Birdeye Solana 400s:** Separate issue — `token_overview` for Solana returns `"Compute units usage limit exceeded"` on current plan. Base calls work fine. Needs plan-tier investigation or endpoint substitution.

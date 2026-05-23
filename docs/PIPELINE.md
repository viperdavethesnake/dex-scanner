# DEX Scanner — Base & Solana

On-demand screener for new token launches on Base and Solana. Triggered via HTTP GET, returns a dark-mode HTML page with AI-scored token cards.

**Endpoint:** `http://192.168.33.231:5678/webhook/dex-scan`  
**n8n workflow ID:** `bZ7P0LR4SML0MUv6`  
**Workflow backup:** `workflows/dex-scanner-workflow.json` (all workflows in `workflows/`)

---

## What it does

1. Pulls the latest token profiles from DexScreener
2. Filters to Base and Solana only
3. Fetches full pair data (price, volume, liquidity, txn counts)
4. Computes volume trend, micro-trend, and a sparkline from existing data
5. Runs each token through three safety APIs in sequence
6. Removes honeypots and blacklisted contracts
7. Pre-filters tokens that fail any hard rule (age out of 15–90m window, micro_trend `recovering`/`down`, or V/L above chain ceiling). If all tokens are filtered, returns a fast "nothing to analyze" page directly — LLM is not called.
8. Sends the surviving tokens to the local LLM (via llama-server) for SKIP / WATCH / INTERESTING scoring
9. Returns a styled HTML page: AI analysis block + one card per non-SKIP token

---

## Pipeline

```
Webhook GET
  → DexScreener /token-profiles/latest/v1
  → Filter Base & Solana
  → Fetch Pair Data (per token)
  → Normalize Pairs
  → Prep OHLCV
  → Fetch & Process OHLCV
  → Prep GoPlus
  → GoPlus Security Check
  → RugCheck (Solana; continueOnFail)
  → Honeypot.is (Base; continueOnFail)
  → Safety Filter
  → Build Prompt (pre-filters age/micro_trend/V/L; short-circuits if all filtered)
  → LLM Analysis
  → Format Response (SKIP hidden; INTERESTING first, then WATCH)
  → Send HTML Response
```

---

## Filters and thresholds

### Data intake (Normalize Pairs)

| Filter | Value |
|--------|-------|
| Chains | Base, Solana only |
| Min liquidity | $10,000 USD |
| Max age | 48 hours |
| Deduplication | Keep highest-liquidity pair per token address |

### Hard pre-filter (Build Prompt — tokens failing these never reach the LLM)

Applied before the LLM sees any data. Tokens failing any condition are silently dropped (counted in the stale figure in the scan header).

| Rule | Threshold |
|------|-----------|
| Min age | ≥ 15 minutes |
| Max age | ≤ 90 minutes |
| Micro-trend (both chains) | Not `recovering` or `down` |
| Micro-trend (Solana only) | Also not `flat` (added Phase 9) |
| V/L ceiling (Solana) | ≤ 4.0× |
| V/L ceiling (Base) | ≤ 8.0× |

These are hard rules. The LLM cannot override them.

```javascript
const vlPass = chain === 'solana' ? (vl <= 4.0) : (vl <= 8.0);
const microPass = chain === 'solana'
  ? (micro !== 'recovering' && micro !== 'down' && micro !== 'flat')
  : (micro !== 'recovering' && micro !== 'down');
return ageMin >= 15 && ageMin <= 90 && microPass && vlPass;
```

**Phase 9 note (2026-05-23):** An earlier version (May 17) re-admitted Solana 6–8× V/L based on scanner data showing 62.7% win rate in that bucket. Unbiased collector data (19,700 signals) showed this was survivorship bias — the 6–8× bucket is actually −1.42% avg, 43.9% win rate. Filter reverted to flat ≤4× ceiling. Solana `flat` micro-trend was also added as a hard exclusion (n=662 filter-pass tokens, −1.47% avg, 27.5% win rate).

### Signal warnings (Build Prompt — shown to LLM as advisory context)

Tokens that pass the hard pre-filter but miss these checks are flagged in the LLM prompt as `SIGNAL WARNINGS`. The LLM uses them to calibrate conviction sizing, not as pass/fail gates.

| Check | Threshold |
|-------|-----------|
| 1h price change | ≥ −15% |
| Volume + micro trend | Not (volTrend=falling AND microTrend=down or fading) |
| 5m buy pressure | ≥ 55% |
| V/L floor | ≥ 0.3× |
| Base net inflow | Flagged prominently if > $20k |

---

## Safety checks

Three APIs run sequentially. All flags are merged before the Safety Filter node. GoPlus, RugCheck, and Honeypot.is all have `continueOnFail: true`.

### GoPlus (all chains)

API: `https://api.gopluslabs.io/api/v1/token_security/{chainId}?contract_addresses={address}`

Chain IDs: `solana` for Solana, `8453` for Base.

| Flag | Condition |
|------|-----------|
| `HONEYPOT` | GoPlus `is_honeypot=1` |
| `BLACKLISTED` | GoPlus `is_blacklisted=1` |
| `MINTABLE` | GoPlus `is_mintable=1` |
| `OWNER_CAN_CHANGE_BALANCE` | GoPlus flag set |
| `HIGH_BUY_TAX` | Buy tax > 10% |
| `HIGH_SELL_TAX` | Sell tax > 10% |
| `FEW_HOLDERS` | Holder count 1–9 |

Also extracts `holderCount` for display.

### RugCheck (Solana only)

API: `https://api.rugcheck.xyz/v1/tokens/{address}/report/summary`

| Flag | Condition |
|------|-----------|
| `RC_HONEYPOT` | Risk name contains HONEYPOT or FREEZE at DANGER level |
| `RC_MINTABLE` | Risk name contains MINT at DANGER level |
| `RC_RUGPULL` | Risk name contains RUGPULL or RUG |
| `RC_LOW_SCORE` | `score_normalised` < 20 |

### Honeypot.is (Base only)

API: `https://api.honeypot.is/v2/IsHoneypot?address={address}&chainID=8453`

| Flag | Condition |
|------|-----------|
| `HONEYPOT` | `honeypotResult.isHoneypot=true` |
| `HIGH_BUY_TAX` | Simulation buy tax > 10% |
| `HIGH_SELL_TAX` | Simulation sell tax > 10% |
| `UNVERIFIED_CONTRACT` | `isOpenSource=false` |
| `CREATOR_CONTROL` | `flags` array contains "control" |

### Additional hard fails added in Phase 7 (GoPlus)

| Flag | Condition |
|------|-----------|
| `HIDDEN_OWNER` | GoPlus `hidden_owner=1` — contract ownership concealed |
| `CAN_RECLAIM_OWNERSHIP` | GoPlus `can_take_back_ownership=1` |
| `CREATOR_HONEYPOT_HISTORY` | GoPlus `honeypot_with_same_creator=1` |
| `WHALE_CONCENTRATION` | Single wallet holds > 40% of supply (derived from GoPlus holder data) |

### Additional enrichment fields passed to LLM (Phase 7)

Previously discarded before the prompt. Now passed as structured context:

`holderCount`, `top1Pct`, `top5Pct`, `creatorPct`, `creatorBalance`, `lpLocked`, `lpBurned`, `lpProviderCount`, `insiderCount`, `insiderNetworkSummary` (RugCheck graph), `launchpad` (Pump.Fun vs other), `rcScore`, `topHolderLines` (top 5 holders with insider flags)

### Safety Filter outcome

Tokens with any of `HONEYPOT`, `RC_HONEYPOT`, `BLACKLISTED`, `HIDDEN_OWNER`, `CAN_RECLAIM_OWNERSHIP`, `CREATOR_HONEYPOT_HISTORY` are **removed entirely** before the LLM step. All others pass through with their flags attached. Flags are deduplicated before risk level is computed.

Risk levels shown on cards:
- `LOW` — no flags
- `MEDIUM` — 1–2 flags
- `HIGH` — 3+ flags

---

## Signal computation

### Volume trend

Computed from existing DexScreener data — no additional API call.

```
projected_hourly = volume_5m × 12
vol_trend_pct    = ((projected_hourly - volume_1h) / volume_1h) × 100
vol_trend        = rising  if vol_trend_pct ≥ +30%
                   falling if vol_trend_pct ≤ −30%
                   flat    otherwise
```

### Micro-trend

Derived from 5m and 1h price changes:

| 5m | 1h | Label |
|----|----|-------|
| > +2% | > 0 | `up` |
| < −2% | < 0 | `down` |
| > +2% | < 0 | `recovering` |
| < −2% | > 0 | `fading` |
| otherwise | — | `flat` |

### V/L ratio

```
vlRatio = volume_1h / liquidity_usd
```

High (> 8×): volume far exceeds liquidity — indicates churn/wash trading. Filter ceiling. Tokens above this threshold are ineligible for INTERESTING.  
Low (< 0.3×): almost no volume relative to pool size — dead token.

### Buy momentum decay

```
buyDecay = buyPct_5m - buyPct_1h
```

Positive = accelerating buy pressure. Negative = buyers backing off.

### Sparkline

4-point ASCII bar chart using price changes as shape proxy: `[0, 6h%, 1h%, 5m%]`. Mapped to Unicode block characters ▁▂▃▄▅▆▇█.

---

## LLM integration

- **Endpoint:** `http://127.0.0.1:8080/v1/chat/completions` (llama-server, localhost)
- **Model:** `Qwen_Qwen3.6-35B-A3B-Q6_K.gguf` (MoE, 30 GB, RTX 8000)
- **Timeout:** 180 seconds
- **Thinking enabled:** `chat_template_kwargs: {enable_thinking: true}`, `max_tokens: 4096` — thinking block (~1700 tok) is separated into `reasoning_content`; only `content` reaches the workflow. Requires `max_tokens ≥ 4096` to leave room for output after thinking.

The LLM's job is **meme coin conviction analysis**, not signal re-evaluation. It reasons about:
- Lifecycle stage: Phase 1 (launch) → Phase 2 (discovery) → Phase 3 (viral) → Phase 4 (peak/distribution)
- Pattern recognition: organic degen momentum vs. cabal/insider pump vs. bot wash trading
- Conviction sizing: would you risk $50? $500? nothing?

It answers three questions per token: LIFECYCLE, PATTERN, CONVICTION.

Output format per token:
```
N. SYMBOL — SKIP/WATCH/INTERESTING — [conviction: $X] — one sentence
```

Conviction amounts: `$0` (pass), `$25`, `$50`, `$100`, `$500`, `$1000`. Calibrated to risk — INTERESTING ≠ automatically high conviction.

The LLM receives full meme coin context: Pump.Fun graduation mechanics, LP burn status, insider/cabal detection (RugCheck graph), holder velocity, creator wallet behavior. It does not re-calculate signals the filters already cover.

---

## Output

The HTML response contains:
1. **Header:** timestamp, token count, active filters, stale pre-filter count
2. **AI Analysis block:** the LLM's numbered list with SKIP/WATCH/INTERESTING badges highlighted
3. **Token cards grid:** INTERESTING first, then WATCH; SKIP tokens are hidden. Each card shows:
   - Name, symbol, chain, DEX
   - Age, liquidity, market cap
   - 5m / 1h / 6h price changes (green/red)
   - Buys/sells 1h, buy% 5m
   - Sparkline, vol trend + micro-trend
   - **Streak badge** (if seen in a prior scan): `3× ↑ · was WATCH · 25m` — appearance count, rating trend arrow, previous rating, minutes since first seen
   - Contract address (monospace)
   - Links: DexScreener · CoinMarketCap · BaseScan/Solscan · **Deep Dive** (purple — triggers deep-dive analysis in new tab)

---

## Stack

### Scanner stack (GPU required — share llamacpp network namespace)

| Service | Container | URL / Address |
|---------|-----------|---------------|
| llama-server | `dex-llamacpp` | http://192.168.33.231:8080 |
| n8n | `dex-n8n` | http://192.168.33.231:5678 |
| TimescaleDB | `dex-timescale` | 192.168.33.231:5432 |
| DEX Scanner (webhook) | — | http://192.168.33.231:5678/webhook/dex-scan |

`dex-llamacpp` owns the macvlan IP 192.168.33.231. `dex-n8n` and `dex-timescale` use `network_mode: service:llamacpp` and communicate via localhost. **All three stop if llamacpp stops.**

### Collector stack (GPU-independent — bridge network)

| Service | Container | Port |
|---------|-----------|------|
| Collector DB | `dex-collector-db` | 5434 |
| Collector | `dex-collector` | — |

Runs independently on `dex-collector-net` bridge. No dependency on llamacpp, n8n, or dex-timescale.
Connect: `psql -h 192.168.33.231 -p 5434 -U collector -d collector_signals`

```bash
# Start/stop independently
docker compose up -d dex-collector-db dex-collector
docker compose stop dex-collector dex-collector-db
```

### n8n Workflow Inventory

| ID | Name | Active | Purpose |
|----|------|--------|---------|
| `bZ7P0LR4SML0MUv6` | DEX Scanner — Base & Solana | always | On-demand scan via webhook |
| `svREuu5gTMgumndn` | DEX Auto-Scanner | **user-controlled** | Scheduled scan every 5 min |
| `2MFJc5cEvhQZNDlc` | DEX Scan Control | always | Start/stop auto-scanner via URL |
| `3lSEjGrScilFstmS` | DEX Outcome Tracker | always | Backfills price_at_5m/15m/30m, target_hit, stop_hit within 48h of scan |
| `uTQ0gfzDS1gf8bDu` | DEX Birdeye Enricher | always | Backfills unique_traders_1h + net_inflow_usd every 2 min |
| `GwTtxU5MgTEoeqHK` | DEX Status | always | Status page — scan history + signal log |
| `O9P2SbCe0KE4ue9R` | DEX Deep Dive | always | On-demand deep analysis of a single token |

Workflow backups: `workflows/` directory.

### Signal Database

**TimescaleDB** at `192.168.33.231:5432`, database `dex_signals`, user `dex`.

Tables:
- `token_signals` — one row per WATCH/INTERESTING token per scan. Includes all signals, LLM rating and reasoning, and entry/target/stop prices for INTERESTING tokens. Birdeye columns (`unique_traders_1h`, `net_inflow_usd`) are backfilled by the Birdeye Enricher within 2 minutes for both Base and Solana — the enricher sets the `x-chain` header dynamically (`solana` or `base`); Birdeye's `/defi/token_overview` endpoint supports both chains. Coverage: ~99% Base, ~88% Solana. Outcome columns (`price_at_5m`, `price_at_15m`, `price_at_30m`, `price_peak_30m`, `target_hit`, `stop_hit`) are backfilled by the Outcome Tracker within 48 hours of each scan. The tracker prioritises records that already have 5m data (needing 15m/30m) over fresh 5m fills, and processes newest-first within each group to favour tokens still active on DexScreener.
- `scan_summary` — one row per scan run: counts, stale rate, trigger type (`manual` vs `auto`).

Connect from anywhere on the LAN:
```
psql -h 192.168.33.231 -U dex -d dex_signals
```

---

## Auto-Scan Control

Start/stop the 5-minute auto-scanner from any browser on the LAN:

```
Start:  http://192.168.33.231:5678/webhook/dex-scan-control?action=start
Stop:   http://192.168.33.231:5678/webhook/dex-scan-control?action=stop
```

**Status page:** `http://192.168.33.231:5678/webhook/dex-status` — scan history, signal log, live auto-scanner state, outcome columns fill in as the tracker backfills. Auto-refreshes every 60 seconds.

The auto-scanner (Workflow B) is **off by default**. Run 2–3 manual scans first to verify latency before enabling. The GPU is only used when a scan fires — stopping auto-scan frees it immediately.

The outcome tracker runs independently of the auto-scanner and has no GPU impact.

---

## Operations

### Run a scan

```
http://192.168.33.231:5678/webhook/dex-scan
```

Open in any browser. Response takes 15–30 seconds depending on model load and API latency.

### Start/stop the stack

```bash
cd /space/docker/containers/dex-scanner

docker compose up -d      # start
docker compose down       # stop
docker compose ps         # status
```

**Never restart dex-llamacpp alone** — it destroys the shared network namespace and breaks DNS in n8n. Always restart the full stack:

```bash
docker compose restart
```

### Update the workflow via n8n UI

Navigate to `http://192.168.33.231:5678`, open the **DEX Scanner — Base & Solana** workflow.

### Backup workflows

After making changes in the UI, re-export via n8n API or UI download and overwrite the relevant file in `workflows/`:

```bash
N8N_KEY=<token>
curl -s http://192.168.33.231:5678/api/v1/workflows/bZ7P0LR4SML0MUv6 \
  -H "X-N8N-API-KEY: $N8N_KEY" > workflows/dex-scanner-workflow.json
```

---

## Known behaviors

- **"All N tokens were pre-filtered" page:** Normal when no tokens in the current batch pass all hard rules (age 15–90m, micro_trend not `recovering`/`down`, V/L ≤ chain ceiling). The LLM is not called; page returns in a few seconds. Header shows "N pre-filtered" count.
- **"⚠ DexScreener returned no token data after two attempts":** The API returned an empty batch on both the initial call and a 10-second retry. Transient — try again in 1–2 minutes.
- **Streak badge on card:** Token appeared in a prior scan. Badge shows `N× [↑/↓] · was RATING · Xm` where N is appearance count, arrow indicates promotion (↑) or demotion (↓) vs previous scan, and Xm is minutes since first seen. State persists for 3 hours then auto-expires.
- **RugCheck on Base tokens:** Solana-native API — returns 404 or empty for Base tokens. `continueOnFail: true` is set; Safety Filter handles missing RugCheck gracefully.
- **Honeypot.is on Solana tokens:** EVM-only API — Solana tokens pass through with a silently ignored error.
- **Model loading:** llama-server loads the GGUF at container start. Check `curl http://192.168.33.231:8080/health` returns `{"status":"ok"}` before running a scan. n8n waits for healthy before starting, but the first scan immediately after stack-up may still catch the model mid-load.
- **No OHLCV API call:** Volume trend and sparkline are derived from data already in the DexScreener pair payload. No separate candle API is called — the sparkline is a shape approximation from 4 data points.

---

## DEX Deep Dive

On-demand, in-depth conviction analysis for a single token. Separate workflow — no interaction with the scanner pipeline.

**Endpoint:** `http://192.168.33.231:5678/webhook/dex-deep-dive?token=<address>&chain=solana|base`

Triggered from scanner cards via the purple **Deep Dive** button, or by pasting the URL directly. Returns a dark-themed HTML page in ~50–90 seconds.

### Data pipeline

```
Webhook GET ?token=&chain=
  → Parse Input (validate + derive chain-specific params)
  → DexScreener /latest/dex/tokens/<address>    — full pair data, all timeframes
  → Birdeye /defi/token_overview                — volume, unique wallets, net flow
  → Birdeye /defi/txs/token                     — recent trade history (free tier; may be limited)
  → GoPlus token_security                       — full security flags, holder data, tax rates
  → RugCheck /report/summary                    — Solana only; continueOnFail
  → Honeypot.is IsHoneypot                      — Base only; continueOnFail
  → Build Prompt                                — assembles all data into structured prompt
  → LLM Analysis (max_tokens 6144, thinking on) — 6-section conviction analysis
  → Format HTML
  → Respond to Webhook
```

### LLM analysis sections

1. **Volume Authenticity** — organic vs. wash trading / bot activity
2. **Momentum Quality** — building, sustained, or decaying
3. **Security Verdict** — full flag walkthrough, LOW/MEDIUM/HIGH/CRITICAL rating
4. **Chain & Profile Fit** — vs. historical dataset patterns for this chain
5. **Conviction Verdict** — ENTER NOW / WAIT FOR DIP / PASS with specific price levels
6. **Invalidation Scenarios** — 2–3 specific conditions that kill the thesis

### Free tier limitations

Birdeye's wallet and net-flow enrichment returns N/A on free tier (endpoints gated). Trade history and token overview work. GoPlus, RugCheck, and Honeypot.is are fully functional. Upgrade Birdeye (~$50/mo) to unlock holder distribution and inflow data.

---

## Empirical observations (updated 2026-05-23)

Based on 19,700+ unbiased collector signals (2026-05-17 to present) plus 2,228 scanner signals (2026-05-03 to 2026-05-17). Collector data is unbiased — all tokens seen, not just filter survivors. See `ML-FINDINGS.md` for full analysis.

### Chain performance

| Chain | n (collector) | avg 5m | win rate |
|-------|--------------|--------|----------|
| Base | 5,295 | +3.53% | 44.1% |
| Solana | 14,344 | −1.00% | 40.5% |

Within scanner window (15–90m):

| Chain | avg 5m | win rate |
|-------|--------|----------|
| Base | +5.41% | 47.6% |
| Solana | −1.26% | 41.1% |

Chain is the single strongest predictor. Base returns positive expected value without any model. Solana is marginally negative without filtering.

### V/L ratio — Solana flat ≤4× ceiling (Phase 9 confirmed)

Unbiased collector data confirmed flat ≤4× is the correct Solana ceiling. The May-17 "non-linear" 6–8× re-admission was a survivorship bias artifact from scanner-only data.

| Solana V/L | collector avg 5m | win rate | Filter |
|------------|-----------------|----------|--------|
| 0–4× | −0.1 to −0.6% | 36–43% | ✅ passes |
| 4–6× | −0.75% | 44.4% | ❌ blocked |
| 6–8× | −1.42% | 43.9% | ❌ blocked |
| 8×+ | −1.4 to −2.1% | 40–42% | ❌ blocked |

Base ceiling remains 8×. Base 4–6× is +18.7% avg — chains behave oppositely in this zone.

**Hold duration caveat:** Even the best V/L bucket turns negative by 15m. The scanner surfaces 5-minute momentum events, not holds.

### Micro-trend by chain

| Chain | micro_trend | avg 5m | win rate | Filter |
|-------|-------------|--------|----------|--------|
| Base | up | +5.40% | 55.3% | ✅ passes |
| Base | fading | +6.08% | 52.6% | ✅ passes |
| Base | flat | +0.55% | 25.8% | ✅ passes (poor win rate but positive avg) |
| Base | recovering | +2.31% | 53.0% | ❌ blocked |
| Base | down | +2.19% | 46.2% | ❌ blocked |
| Solana | up | +0.54% | 46.0% | ✅ passes |
| Solana | fading | −1.07% | 45.2% | ✅ passes |
| Solana | flat | −0.64% | 32.5% | ❌ blocked (Phase 9) |
| Solana | down | −1.47% | 42.0% | ❌ blocked |
| Solana | recovering | −2.57% | 40.3% | ❌ blocked |

### Volume trend

| Chain | vol_trend | avg 5m | win rate |
|-------|-----------|--------|----------|
| Base | rising | +7.22% | 52.7% |
| Base | flat | +5.48% | 54.6% |
| Base | falling | +1.55% | 47.7% |
| Solana | rising | −0.08% | 45.1% |
| Solana | flat | −1.02% | 46.9% |
| Solana | falling | −1.43% | 39.9% |

Rising volume on Base is a strong positive signal. On Solana, rising volume has no meaningful edge — chains behave oppositely.

### Age buckets

Base 15–20m is the best-performing bucket (+18.82%, 64.9% win). Do not raise the Base age floor above 15m. Solana 15–20m is the worst Solana bucket (−3.40%, 43.7% win).

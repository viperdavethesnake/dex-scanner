# START HERE — DEX Scanner handoff (2026-06-02)

This document is for a fresh Claude session picking up this project.
Read this first, then read the files it points to. Do not wing it from git log.

---

## What this project is

A DEX scanner for new token launches on Base and Solana.
Primary tool: an n8n webhook that screens tokens via DexScreener, runs safety
checks, and returns an HTML page with INTERESTING / WATCH / SKIP cards powered
by a local LLM (Qwen3.6-35B on RTX 8000).

Secondary layer (currently stopped and broken): a shadow trader that scores
signals with LightGBM and simulates fills via 0x aggregator quotes.

---

## Current state (everything is stopped)

All Docker containers were stopped intentionally on 2026-06-02 after the shadow
trader produced anti-predictive results. No container is running.

Read `docs/CURRENT-STATE-2026-06-02.md` for the full picture.

**To restart just the scanner (safe, works):**
```bash
cd /space/docker/containers/dex-scanner
docker compose up -d llamacpp timescaledb n8n
```

**Do not restart the shadow trader** until the bugs in this document are fixed.

---

## The three bugs to fix (trader)

All three are documented in `docs/RECOMMENDATIONS.md`. Summary with file refs:

### Bug 1: Double-momentum selection — HIGHEST PRIORITY
**File:** `dex-trader/signals.py`  
**Problem:** `hard_filter()` removes `micro_trend: down/recovering` and tokens
with high sell pressure BEFORE model scoring. The model also assigns high scores
to momentum tokens. Combined: selects end-of-pump, not beginning-of-pump.
Shadow trader got 30.8% win rate vs 48.8% random base rate — worse than random.  
**Fix:** Strip `micro_trend` and `sell_pressure_5m` from `hard_filter()`. Keep
only: age window (15–90m), extreme V/L (>10), liquidity floor if any.
Let the model do the momentum selection.

### Bug 2: Stop-loss blind on rug pulls
**File:** `dex-trader/main.py`, function `_manage_open_positions`  
**Problem:** `_fetch_dexscreener_price()` returns `None` for delisted/rugged pairs.
`drawdown_pct` stays `None`, stop-loss check is skipped, position holds for 5 min
and exits at crashed aggregator price. Example: OL token, -78% exit via timer.  
**Fix:** If `current_dex is None` AND position has been held for >60 seconds,
treat it as stop-loss trigger. Exit at fallback price (`fill_price * 0.5` or
aggregator quote, whichever is available).

### Bug 3: Position size too small
**File:** `compose.yaml`, env var `TRADE_SIZE_USD=10.0`  
**Problem:** Real round-trip cost for $10 trades on thin Base AMMs is ~8.3%.
Model edge at ≥0.70 threshold is ~15pp lift in win rate. Expected net ≈ +2% —
too thin. At $100, gas drops from ~5% to ~0.5% of trade; expected net ≈ +9%.  
**Fix:** Set `TRADE_SIZE_USD=50` (or 100) for the next shadow run. Do not go
live at $10 regardless of shadow results.

---

## Before retraining the model

**Read first:** `docs/ML-ANALYSIS-2026-06-02.md`

Key facts:
- Walk-forward AUC: 0.63–0.66 (real signal, not noise)
- At ≥0.70 threshold: 63–67% win rate on held-out Base data
- Features with most signal: `log_volume_5m` (r=0.25), `log_liquidity_usd` (r=0.15)
- The model is fine. The hard_filter was the problem.

**Do not retrain until Bug 1 is fixed.** A model trained against hard-filtered
data learns the wrong distribution. Train on the full collector population
(age 15–90m, no micro_trend filter), then apply model threshold at inference.

When retraining:
```bash
cd /space/docker/containers/dex-scanner/analysis
source venv/bin/activate
python3 export_model.py --chain base --train-cutoff 2026-05-30
```
This writes `models/lgbm_base.txt`, `models/feature_list.json`, `models/metadata.json`.
The trader hot-reloads the model on mtime change (no restart needed).

---

## Infrastructure reference

| Service | Container | Port | Notes |
|---------|-----------|------|-------|
| llama.cpp | dex-llamacpp | 8080 | Qwen3.6-35B-A3B-Q6_K, RTX 8000 (GPU 1) |
| n8n | dex-n8n | 5678 | shares network with llamacpp |
| Scanner DB | dex-timescale | — | dex_signals, internal only |
| Collector | dex-collector | — | polls DexScreener every 5 min |
| Collector DB | dex-collector-db | 5434 | collector_signals; 64k rows |
| Trader DB | dex-trader-db | 5435 | trader; 58 exits recorded |
| Shadow trader | dex-trader | 8090 | DO NOT start yet |

**Never restart dex-llamacpp alone.** It owns the macvlan IP. n8n shares its
network namespace. Restarting llamacpp alone breaks n8n DNS.
Always restart the full stack: `docker compose restart`

**n8n API key:** Expires ~2026-06-20. Generate fresh from n8n UI → Settings → API.
Store in `.env` as `N8N_JWT=`. Use as `X-N8N-API-KEY:` header.
Base URL: `http://192.168.33.231:5678/api/v1`
Workflow ID: `bZ7P0LR4SML0MUv6`

---

## Key files to read

| File | What it covers |
|------|----------------|
| `CLAUDE.md` | Project-level instructions (read first, always) |
| `docs/CURRENT-STATE-2026-06-02.md` | Full state at shutdown |
| `docs/RECOMMENDATIONS.md` | What to fix, what to avoid |
| `docs/ML-ANALYSIS-2026-06-02.md` | ML findings, walk-forward results |
| `docs/PIPELINE.md` | Full scanner pipeline spec |
| `dex-trader/signals.py` | hard_filter (Bug 1 is here) |
| `dex-trader/main.py` | Main loop, position management (Bug 2 is here) |
| `analysis/features.py` | Single source of truth for feature engineering |
| `analysis/export_model.py` | Model training and export script |
| `compose.yaml` | All service definitions, env vars, networks |

---

## What NOT to do

- Do not add new data sources (Birdeye Phase 2, on-chain enrichment) before the
  3 trader bugs are fixed. More data does not fix execution problems.
- Do not add complexity to the scanner. It works. Leave it alone.
- Do not use the `n8n import:workflow` CLI while n8n is running — corrupts webhook
  registration. Use the REST API to push workflow changes.
- Do not restart llamacpp independently. Always restart the full stack together.
- Do not commit secrets. The `gitleaks` pre-commit hook will block you. Secrets
  live in `.env` (gitignored).

---

## Suggested session order

1. Read `CLAUDE.md` + this file
2. Read `docs/RECOMMENDATIONS.md`
3. Fix Bug 1 (`dex-trader/signals.py`) — 15-min job
4. Fix Bug 2 (`dex-trader/main.py`) — 30-min job
5. Update `compose.yaml` TRADE_SIZE_USD to 50
6. Retrain model (`analysis/export_model.py`)
7. Start stack with trader, run for 1 week, collect ≥200 exits
8. Evaluate shadow results against the walk-forward baseline (63–67% win rate)

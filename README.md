# DEX Scanner

Automated system for identifying and trading momentum pumps on new token launches on Base and Solana DEXes.

---

## What This Is

New tokens launch on DEXes constantly. Most are junk. But junk pumps — hype, bots, and degens pile in, price moves 10–30% in minutes, then collapses. This system identifies which tokens are being pumped *right now*, scores them with ML and an LLM, and is being built toward automatically executing 5-minute scalp trades.

This is not a long-term investment system. It does not evaluate project quality. It finds momentum and gets in and out before the pump reverses. Pure DEX scalping.

---

## What's Been Built

### 1. DEX Scanner (live — manual use)

An AI-powered token screener that runs on-demand or on a 5-minute automatic cycle. Pulls new Base and Solana token launches from DexScreener, applies hard momentum and safety filters, then sends surviving tokens to a local LLM for conviction analysis.

**Output:** Dark-mode HTML page with token cards — SKIP / WATCH / INTERESTING ratings, chain, age, liquidity, volume trend, price changes, safety flags, and a one-click Deep Dive button.

**Access:** `http://192.168.33.231:5678/webhook/dex-scan`

### 2. Data Collector (live — running 24/7)

A GPU-independent Python service that polls DexScreener every 5 minutes and records *every* token it sees — not just ones that pass the scanner's filters. Backfills actual 5-minute price outcomes for every row.

This is the unbiased training dataset for the ML model. Currently: **19,700+ rows, 830 unique tokens, May 17–present.**

### 3. ML Model (built — not yet deployed)

A LightGBM classifier trained on collector data. Predicts which tokens will move in the next 5 minutes based purely on momentum signals (volume acceleration, buy pressure, price trend). No fundamentals. No project quality.

Key results on 2.5 days of out-of-sample validation:

| Strategy | Win Rate | Profit Factor | Max Drawdown |
|---|---|---|---|
| No filter (random) | 37% | 0.86x | −$562 |
| Current hard filter | 40% | 1.22x | −$42 |
| Model ≥0.65 | 49% | 1.52x | −$92 |
| Model ≥0.70 | 51% | 2.02x | −$40 |
| First-entry ≥0.70 | 55% | 2.50x | −$11 |

*$10/trade flat, 1.5% round-trip cost, May 21–23 validation set.*

---

## The Goal

**Auto-trade the 5-minute pump.** When the ML model scores a token above a threshold, the system buys automatically on-chain via DEX router, sets a hard 5-minute exit timer, and sells everything. No human in the loop. Small wallet, controlled risk.

---

## Stack

| Component | Technology | Status |
|---|---|---|
| Workflow engine | n8n (Docker) | ✅ Live |
| LLM inference | llama.cpp + Qwen3 35B Q6_K | ✅ Live |
| Signal database | TimescaleDB | ✅ Live |
| Data collector | Python + TimescaleDB | ✅ Running 24/7 |
| ML model | LightGBM (Python) | ✅ Built, not deployed |
| Execution layer | DEX router + wallet | ❌ Not built |

**Hardware:** RTX 8000 (48 GB VRAM) on GPU 1. Model loads in ~2 minutes. Scanner stack shares a macvlan IP (192.168.33.231) — llamacpp owns it, n8n and TimescaleDB share its network namespace.

---

## Docs

| File | Contents |
|---|---|
| `README.md` | This file — project overview |
| `docs/THESIS.md` | Trading strategy — what we're doing and why |
| `docs/ML-FINDINGS.md` | Data analysis and ML model findings |
| `docs/ROADMAP.md` | What needs to be built next |
| `docs/PIPELINE.md` | Technical reference — pipeline, filters, signals, APIs |
| `docs/RESUME.md` | Internal session notes — stack state, next steps |

---

## Quick Start

```bash
cd /space/docker/containers/dex-scanner

# Start scanner stack (GPU required)
docker compose up -d

# Wait for model to load (~2 min), then scan
curl http://192.168.33.231:8080/health   # {"status":"ok"} when ready
# Open http://192.168.33.231:5678/webhook/dex-scan in browser

# Collector runs independently (no GPU)
docker compose up -d dex-collector-db dex-collector
```

# Current State — 2026-06-02

Stack intentionally stopped. All application containers down. This document records
what was built, what is broken, and the state of the data at shutdown.

---

## Stack status at shutdown

| Container | Status | Notes |
|-----------|--------|-------|
| dex-llamacpp | STOPPED | Qwen3.6-35B MoE, RTX 8000 |
| dex-n8n | STOPPED | Scanner workflow intact, not deleted |
| dex-timescale | STOPPED | Scanner DB (dex_signals) — scanner signals, unused by ML |
| dex-collector | STOPPED | 15 days of data collected |
| dex-collector-db | STOPPED | 64,624 raw_signals rows (27,881 in age 15-90m window) |
| dex-trader | STOPPED | Shadow mode, 58 exits recorded |
| dex-trader-db | STOPPED | trades table intact |

All containers stopped via `docker stop` (not `docker compose down` — data volumes preserved).
To restart scanner only: `docker compose up -d llamacpp timescaledb n8n`

---

## What was built (in order)

### Original scanner (working)
- n8n webhook → DexScreener API → safety checks (GoPlus, RugCheck, Honeypot.is)
- Signal computation: vol_trend, micro_trend, V/L ratio, sparkline
- LLM analysis: Qwen3.6-35B-A3B via llama.cpp, thinking enabled
- HTML response: INTERESTING / WATCH / SKIP cards
- **Status: intact and working. Nothing broken here.**

### Collector (working, running passively)
- Python service polling DexScreener profiles every 5 minutes
- Records raw_signals with 5-minute price outcomes
- Phase 1 completed: unions `/token-profiles` and `/token-profiles/updates` endpoints
- 64,624 rows collected since 2026-05-17; 15 days of data
- Birdeye and GoPlus enrichment wired but only ~11% and ~26% of rows enriched (Birdeye free tier limits)
- **Status: was working. Stopped along with rest of stack.**

### Shadow trader (broken, stopped)
- LightGBM model scoring signals from collector in real-time
- 0x aggregator quotes for Base tokens
- Simulated fills and exits at T+5min or stop-loss
- 58 exits recorded: 32.8% win rate, -7.34% avg net PnL
- **Status: broken. See ML analysis and recommendations.**

---

## Shadow trader results (58 exits, 2026-05-25 → 2026-06-02)

| Metric | Value |
|--------|-------|
| Total records | 233 (58 exited, 175 skipped) |
| Win rate | 32.8% |
| Avg gross PnL | -5.41% |
| Avg net PnL | -7.34% |
| Avg cost (real) | ~1.93% slippage+gas |
| Avg real total friction | ~8.3% (vs 1.5% assumed) |
| Stop-loss exits | 3 (avg -42%) |
| Timer exits | 55 (avg -5.44% net) |
| Worst single trade | -98% (SBS, stop-loss) |
| Worst timer trade | -79% (OL — stop-loss failed to fire) |

Random base rate for same period: 48.8% win, +4.15% avg outcome.
The model selected worse than random.

---

## Model state

- File: `analysis/models/lgbm_base.txt`
- Trained at: 2026-05-25T19:25:48 UTC
- Train cutoff: 2026-05-23 (trained on May 17-23 data only)
- Val AUC: 0.5887 (against the full dataset including Solana)
- Walk-forward AUC on Base only: 0.63-0.66 (the model has real signal)
- At ≥0.70 threshold in walk-forward: 63-67% win rate on DexScreener metric
- In production: 30.8% win rate (see ML analysis for why)

---

## Known bugs at shutdown

1. **Stop-loss blind spot**: `_fetch_dexscreener_price` returns None for rugged/delisted
   pairs. `drawdown_pct` stays None. Stop-loss never fires. Token waits 5 minutes
   and exits via aggregator at crashed price. See `main.py:_manage_open_positions`.

2. **Hard-filter + model double-momentum**: `signals.py:hard_filter` pre-selects
   momentum tokens (good micro_trend, high buy pressure). The model also assigns
   high scores to momentum tokens. Combined, the system selects end-of-pump tokens
   rather than beginning-of-pump. Explains the anti-predictive live results.

3. **Cost assumption wrong by ~6x**: Backtest used 1.5% round-trip. Real measured
   cost for $10 positions on thin Base AMMs: ~8.3% total friction (entry drift +
   exit slippage + gas). Any edge under 8.3%/trade is a losing strategy at this size.

4. **Phase 2 intake (Birdeye new_listing) never implemented**: Planned, not built.
   The intake gap remains. Collector captures ~100 Base tokens/hr vs ~160/hr from
   token_profiles_updates; Birdeye new_listing would add more but was deprioritized.

---

## Data volumes at shutdown

| Table | Rows | Notes |
|-------|------|-------|
| raw_signals (collector) | 64,624 | 15 days; all with 5m outcomes |
| raw_signals (age 15-90m) | 28,881 | ML-relevant window |
| trades (trader) | 233 | 58 exited, 175 skipped |

---

## Active prompt file

`prompts/2026-05-24-shadow-trader.md` — shadow trader build plan, Phases 1-4.
Phase 3 and 4 are complete (trader deployed, shadow validation run).
This file should be deleted; it is now redundant with git history and this doc.

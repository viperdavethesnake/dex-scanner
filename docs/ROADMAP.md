# Roadmap — Auto-Trading

The goal: automated 5-minute scalp trades on new DEX token launches, Base and Solana, using a small funded wallet and the ML model as the entry signal.

---

## Current State

| Component | Status |
|---|---|
| Signal pipeline (DexScreener → signals → filters) | ✅ Live |
| LLM scanner (manual sessions) | ✅ Live |
| Data collector (continuous, GPU-free) | ✅ Running 24/7 |
| ML model (LightGBM, Base 0.67 / Solana 0.57 AUC) | ✅ Built, pending deployment via shadow trader |
| Birdeye enrichment in collector | ✅ Live (2026-05-23, Base only, sample rate 0.02) |
| Shadow trader design | ✅ Approved 2026-05-25, see `docs/decisions/SHADOW-TRADER-DESIGN.md` |
| Execution layer | 🛠️ In progress (shadow mode first) |
| Wallet integration | 🛠️ In progress (shadow mode — no real keys in use) |

---

## Phase 1 — Improve the Model (ongoing, no new builds needed)

The collector runs automatically. Each week of data improves the model.

**Priorities:**
- Add Birdeye enrichment to the collector (`net_inflow_usd`, `unique_traders_1h`) — ✅ done (Base-only, 2026-05-23)
- Retrain weekly on a rolling window — model decays; a 4-day-old model degrades to ~0.59 AUC
- Train separate Base and Solana models (Base: 0.67 AUC, Solana: 0.57) — straightforward once execution layer is built
- Switch training target to `>10% move` (AUC 0.684) — experiment result; see `docs/ML-FINDINGS.md`. Deferred until shadow mode is validated.

**Gate to Phase 2:** Shadow trader running for ≥5 days AND average `cost_delta_pct` (measured real cost vs backtest 1.5% assumption) stays under +1.0 percentage points. Time-based data gates do not apply — each token launch is an independent ~hour-long event, not a market trend. Thousands of independent samples already exist in the collector; the remaining unknown is execution cost, which only live aggregator quotes can answer.

---

## Phase 2 — Execution Layer

A new Python service (`dex-trader`) added to the Docker Compose stack. No dependency on n8n or the LLM.

**Decision loop:**
```
Every 5 seconds:
  1. Pick up new signals from collector DB (raw_signals, past watermark)
  2. Apply hard pre-filters (age 15–90m, micro_trend exclusions, V/L ceilings — same as scanner)
  3. Score with ML model
  4. For each token scoring ≥ threshold:
       - Check: not already in a position for this token
       - Check: open positions < max simultaneous limit
       - Check: daily loss < circuit breaker
       → Submit buy swap on DEX
       → Record position (token, entry price, entry time)
  5. For each open position where entry_time + 5m has elapsed:
       → Submit sell swap on DEX
       → Record outcome
```

**Exit rules (non-negotiable):**
- Hard 5-minute timer — sell everything regardless of price
- No "let's see where it goes"
- No re-entry on the same token within 30 minutes

**Circuit breakers:**
- Max simultaneous open positions: 3
- Max position size: $10–15 per trade
- Daily loss limit: $50 (pause for 24 hours if hit)
- Emergency stop: manual webhook or env var flag

---

## Phase 3 — Wallet and DEX Integration

**Chain: Base first.** Better model accuracy, Coinbase native infrastructure, lower and more predictable gas fees.

**Wallet:** Direct `web3.py` + `eth-account` with `PRIVATE_KEY` env var. Decided in `docs/decisions/SHADOW-TRADER-DESIGN.md §6`. No third-party key custody, full local control, straightforward key rotation. CDP SDK and similar add unnecessary infrastructure dependencies at this wallet size.

**DEX routing (decided — see `docs/decisions/SHADOW-TRADER-DESIGN.md §5`):**
- Primary: 0x Swap API v2 — aggregates across Base DEXes, free tier, 1 RPS
- Fallback: Aerodrome Router — fires for ~10–25% of candidates (new tokens too thin for 0x)
- Secondary fallback: Uniswap V3 QuoterV2
- Minimum liquidity check at execution time: ≥$10k (re-verified at quote time)

**Key implementation details:**
- Token approval transaction required on first buy of any ERC-20 — handle gracefully, add ~$0.05 gas and ~5s latency
- Slippage tolerance: 2–3% (aggressive enough to fill, conservative enough not to get wrecked on thin books)
- Gas estimation before submission — if gas cost > 20% of trade size, skip
- Failed transaction handling — log, don't retry, move on

**Solana (after Base is stable):**
- Jupiter aggregator for swap routing
- Solana wallet via keypair file
- Same circuit breakers, separate position tracking

---

## Phase 4 — Paper Trading (mandatory before real money)

Run the execution layer with real signals and real threshold logic but do not submit actual transactions. Log every "would have bought" and "would have sold" event.

**Duration:** Until shadow has logged ≥200 completed trades and `cost_delta_pct` has stabilized within ±0.5pp over a rolling 50-trade window.  
**Validation:** shadow trade P&L should match backtest within 30%  
**What to watch:** fill simulation accuracy, signal frequency vs backtest, threshold stability

If shadow results diverge significantly from backtest, investigate before proceeding. Common causes: slippage assumption wrong, model threshold needs adjustment, or the data distribution has shifted (new launchpad dominant, new bot behavior, new chain conditions).

---

## Phase 5 — Live Trading, Small Wallet

**Wallet size:** $200–300  
**Per trade:** $10–15 flat  
**Max simultaneous:** 3 positions ($30–45 peak exposure)  
**Daily loss limit:** $50  
**Review cadence:** daily P&L check, weekly model retrain

Start with Base only. Add Solana after Base live has logged ≥30 profitable days with profit factor ≥1.5x AND Solana Birdeye access is unblocked (free tier reset test confirms or Lite tier upgrade approved).

**Success criteria to scale up:**
- ≥200 completed live trades
- Profit factor ≥ 1.5x sustained (not just lucky early run)
- Max drawdown within ±50% of shadow-measured drawdown (model does not predict drawdown directly; this is a measured stability check against the shadow baseline)
- No execution failures (failed txs, stuck positions, approval hangs)

---

## Architecture (target state)

```
Docker Compose Stack
├── Scanner group (GPU)
│   ├── dex-llamacpp        LLM inference
│   ├── dex-n8n             Workflow engine (manual scans, LLM output)
│   └── dex-timescale       Scanner signal DB
│
├── Collector group (no GPU)
│   ├── dex-collector       Data collection + outcome backfill
│   └── dex-collector-db    Collector signal DB (training data)
│
└── Trader group (new — no GPU)         ← Phase 3 implementation in progress
    ├── dex-trader          Decision loop + execution
    ├── dex-trader-db       Trade log, position tracking
    └── dex-model-server    (optional) FastAPI model scoring endpoint
```

The trader group is fully independent of the scanner and LLM. It runs continuously, GPU-free, funded by a separate small wallet. The scanner remains available for manual sessions alongside the automated trader.

---

## Open Questions (live)

Resolved questions are recorded in `docs/decisions/`. Active open questions:

1. **Birdeye tier upgrade timing.** Currently Standard (free), 0.02 sample rate. Lite ($39/mo) unlocks Solana enrichment if reset test (2026-06-24) confirms tier-gating. Decision deferred until Base shadow proves profitable execution.

2. **Solana wallet integration.** Solana is deferred until Base live is stable. When activated: keypair file via `solders` + `solana-py`, Jupiter aggregator. Design stub exists at `dex-trader/aggregators/jupiter.py`.

3. **MEV / sandwich protection.** Shadow mode does not measure MEV cost. Live mode on Base will need either a private RPC (e.g., Flashbots Protect) or accept the MEV tax. Decision deferred until shadow-vs-live cost gap is measured.

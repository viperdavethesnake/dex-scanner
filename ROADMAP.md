# Roadmap — Auto-Trading

The goal: automated 5-minute scalp trades on new DEX token launches, Base and Solana, using a small funded wallet and the ML model as the entry signal.

---

## Current State

| Component | Status |
|---|---|
| Signal pipeline (DexScreener → signals → filters) | ✅ Live |
| LLM scanner (manual sessions) | ✅ Live |
| Data collector (continuous, GPU-free) | ✅ Running 24/7 |
| ML model (LightGBM, 0.61 AUC) | ✅ Built, not deployed |
| Execution layer | ❌ Not built |
| Wallet integration | ❌ Not built |

---

## Phase 1 — Improve the Model (ongoing, no new builds needed)

The collector runs automatically. Each week of data improves the model.

**Priorities:**
- Add Birdeye enrichment to the collector (`net_inflow_usd`, `unique_traders_1h`) — one build session, largest single improvement available
- Retrain weekly on a rolling window — model decays; a 4-day-old model degrades to ~0.59 AUC
- Switch training target to `>10% move` (AUC 0.684, 2.34x lift vs 0.61 for win/loss)
- Train separate Base and Solana models (Base: 0.67 AUC, Solana: 0.57)

**Gate to Phase 2:** 4+ weeks of data, stable out-of-sample AUC ≥ 0.62 on a rolling test set.

---

## Phase 2 — Execution Layer

A new Python service (`dex-trader`) added to the Docker Compose stack. No dependency on n8n or the LLM.

**Decision loop:**
```
Every 5 minutes:
  1. Pull latest DexScreener signals
  2. Compute signals (same logic as collector)
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

**Wallet options:**
- Coinbase Developer Platform (CDP SDK) — managed MPC wallet, Python library, native Base support
- Direct web3.py + private key — simpler, more portable, no third-party dependency
- Decision to be made based on Coinbase API access available

**DEX routing:**
- 0x API swap endpoint — aggregates across Base DEXes (Uniswap V3, Aerodrome, etc.), returns best price, handles token approvals
- OR: direct Uniswap V3 router — simpler dependency, slightly less optimal routing
- Minimum liquidity check at execution time: ≥$10k (already enforced in scanner, re-check before trade)

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

**Duration:** minimum 2 weeks  
**Validation:** paper trade P&L should match backtest within 30%  
**What to watch:** fill simulation accuracy, signal frequency vs backtest, threshold stability

If paper trade results diverge significantly from backtest, investigate before proceeding. Common causes: slippage assumption wrong, model threshold needs adjustment, market regime shifted.

---

## Phase 5 — Live Trading, Small Wallet

**Wallet size:** $200–300  
**Per trade:** $10–15 flat  
**Max simultaneous:** 3 positions ($30–45 peak exposure)  
**Daily loss limit:** $50  
**Review cadence:** daily P&L check, weekly model retrain

Start with Base only. Add Solana after 4+ weeks of stable Base performance.

**Success criteria to scale up:**
- 30+ days of live trading
- Profit factor ≥ 1.5x sustained (not just lucky early run)
- Max drawdown within model predictions
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
└── Trader group (new — no GPU)         ← Phase 2/3
    ├── dex-trader          Decision loop + execution
    ├── dex-trader-db       Trade log, position tracking
    └── dex-model-server    (optional) FastAPI model scoring endpoint
```

The trader group is fully independent of the scanner and LLM. It runs continuously, GPU-free, funded by a separate small wallet. The scanner remains available for manual sessions alongside the automated trader.

---

## Open Questions

1. **Coinbase API specifics** — which Coinbase product/SDK is being used for wallet and execution? CDP SDK, Coinbase Wallet, or something else? Determines Phase 3 implementation.

2. **Solana wallet** — Phantom? Keypair file? Solana doesn't route through Coinbase natively.

3. **Model serving** — inline in the trader script (simplest) or a separate FastAPI endpoint (more modular, allows hot reload on retrain)?

4. **Retraining automation** — manual weekly retrain via script, or a scheduled cron job that retrains and hot-swaps the model?

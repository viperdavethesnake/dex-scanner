# Next: Shadow Trader Service

**Issued:** 2026-05-24
**Status:** Investigation → Design → Approval Gate → Implement → Shadow validate
**Owner:** Claude Code

Build the `dex-trader` service in shadow mode. This is ROADMAP.md Phase 2 + Phase 3 prep collapsed into one build, because Phase 1 (data accumulation) is now passive and the long pole is execution-path uncertainty, not model quality.

The service will subscribe to scanner signals, score them with the current LightGBM model, request live aggregator quotes, simulate fills, and log everything. No broadcasts. No real money. The goal is to answer the only material remaining question before risking capital: **does the +1.85% / 49-55% post-cost edge survive contact with real Jupiter and 0x quotes?**

---

## Context (already decided strategically; don't re-litigate)

- Manual trading currently via Coinbase DEX. Bot trading will NOT use Coinbase DEX (no API). Bot uses own wallet + aggregators.
- Base-first. Solana deferred until reset test on 2026-06-24 and until Base auto-trading is proven profitable.
- Wallet size $200–300, trade size $10–15, conviction threshold ≥0.70 first-entry.
- LLM is OUT of the bot decision path — used for scanner narrative only. Trader scores with LightGBM only.
- 5-minute event window, not hold. Position manager exits at T+5min or earlier on TP/SL.
- ROADMAP.md Phase 5 architecture is the target: `dex-trader` + `dex-trader-db` on bridge network, GPU-free, isolated from scanner stack.

---

## Phase 1 — Investigation (no code, no design yet)

Read these and report back:

1. **Existing code state:**
   - `collector/` — pattern for a Python service in this project (db handling, error patterns, env var conventions)
   - `analysis/ml.py` — how the model is trained and persisted
   - `analysis/backtest.py` — how a "fill" is currently simulated and what cost assumptions are baked in
   - `init.sql` and `collector/init.sql` — schema patterns
   - `compose.yaml` — networking, env_file, healthcheck patterns
   - `docs/ROADMAP.md` Phase 2/3/5 — what the user already planned

2. **Aggregator API surface:**
   - For Solana: pull `https://station.jup.ag/docs/apis/swap-api` (Jupiter v6 / Ultra). Document the quote endpoint, required params, response shape, rate limits, auth (key needed?), and how to extract effective price and expected slippage.
   - For Base: pull `https://0x.org/docs/api` (0x Swap API v2). Same questions. Note that 0x API requires an API key now — check current free-tier limits.
   - For Base fallback when 0x doesn't have a quote (new tokens): note Aerodrome's router contract address and Uniswap V3 Quoter contract address on Base, and the call pattern for a direct quote.

3. **Model file artifact:**
   - Where does `analysis/ml.py` currently write the trained model? Path, format (pickle / lgb native / joblib)?
   - Is there one file or split files (model + feature list + thresholds)?

4. **Schema for trade logging:**
   - Survey what fields a `trades` table needs to answer the question "did real quotes match backtest assumptions?" — intent_ts, quote_ts, fill_ts, dexscreener_price_at_signal, aggregator_quote_price, simulated_exit_price, simulated_pnl, slippage_bps, gas_cost_estimate_usd, conviction_score, signal_features_snapshot (JSONB), etc.

Report all of this as a markdown investigation summary at `docs/decisions/SHADOW-TRADER-INVESTIGATION.md`. STOP here. Do not write the design yet. Wait for user feedback on the investigation.

---

## Phase 2 — Design proposal (after investigation feedback)

Write `docs/decisions/SHADOW-TRADER-DESIGN.md` covering everything below. Make explicit recommendations with rationale on every decision; the user will review and adjust.

**Required design sections:**

1. **Service architecture** — process model, loop structure, separation of concerns (signal subscriber / scorer / quoter / simulator / logger), restart behavior, healthcheck endpoint.

2. **Database** — new `dex-trader-db` (separate TimescaleDB on bridge network, isolated from scanner) vs. reuse `dex-collector-db`. Recommend one with reasoning. Schema for `trades`, `positions`, and any state tables. Migration strategy (init.sql vs in-code migrate()).

3. **Signal ingestion** — how the trader subscribes to scanner signals. Options to consider: (a) poll scanner DB directly for new `token_signals` rows past a watermark, (b) Redis stream emitted by scanner, (c) webhook from n8n. Recommend one with rationale. The choice affects scanner workflow modifications.

4. **Model serving** — where the model file lives, how the trader loads it, hot-reload mechanism on mtime change. Concrete file paths and watch interval.

5. **Aggregator integration** — propose the wrapper pattern. One module per aggregator (jupiter.py, zerox.py, aerodrome.py), unified `quote()` interface returning a normalized Quote object. Quote includes effective price, expected slippage, route, and source.

6. **Wallet/key management** — for Base. Investigate and recommend: direct `eth-account` + `web3.py` with PRIVATE_KEY env var, vs. Coinbase CDP server wallet SDK, vs. Privy/Turnkey. Trade-offs: complexity, cost, key custody, rotation, future-proofing. Pick one. For Solana, same investigation: `solders` keypair file vs alternatives. Note: in shadow mode no keys are actually used to sign; we still need to design the path so the same code works in live mode later.

7. **Simulated fill model** — given an aggregator quote, how do we compute the simulated entry and exit? Realistic AMM slippage formulas for the entry leg. For exit, three options to choose from: (a) replay DexScreener price at T+5min from the outcome tracker, (b) live-poll DexScreener at T+5min, (c) request a second aggregator quote at exit time. Recommend one.

8. **Position lifecycle state machine** — states (intent → quoted → simulated_filled → managed → simulated_exited → failed), transitions, persistence, recovery on restart.

9. **Risk controls (built in shadow but exercised in live)** — per-trade cap, per-day loss cap, per-hour trade rate limit, kill switch flag, slippage rejection threshold, quote-vs-signal-price drift threshold. Set conservative defaults. These must be testable in shadow mode (e.g., a kill switch flag should be respected even when not broadcasting).

10. **Observability** — what gets logged where, what metrics matter, simple SQL queries for the first-day and first-week reviews.

11. **Compose changes** — new service definition, network attachment, env additions, build context, healthcheck.

12. **Rollback plan** — how to turn the trader off cleanly without affecting collector or scanner.

13. **Open questions** — anything you can't decide without input, listed explicitly. We'll address these in design review rather than guessing.

STOP after the design doc is written. Wait for user approval before any code is written.

---

## Phase 3 — Implementation (after design approval)

Per the approved design:
- Scaffold `dex-trader/` directory with Dockerfile, main.py, db.py, signals.py, scorer.py, aggregators/, simulator.py, logger.py
- Apply schema migration to `dex-trader-db`
- Wire into `compose.yaml`
- Update `.env.example` with all new env vars (placeholders only)
- Add `dex-trader` section to `docs/PIPELINE.md`
- Add session note to `docs/RESUME.md`

Run with `BROADCAST_ENABLED=false` (or equivalent) hardcoded for shadow mode. The path to live broadcasting is deliberately not implemented in this pass.

---

## Phase 4 — Shadow validation (after deploy)

Run for one week minimum. Report at three checkpoints:

**T+1 hour:** Smoke test. Confirm trader is consuming signals, scoring, getting quotes, simulating fills, writing rows. Sample 5–10 trades end-to-end and verify each step.

**T+24 hours:** First real read. Query the `trades` table for distribution of quote latency, slippage vs. expected, conviction distribution, signal-to-trade conversion rate. Compare aggregator quote price vs. dexscreener price at signal time — this is the "scan latency cost" measurement.

**T+1 week:** Edge survival check. Compute simulated win rate, avg return per trade, and profit factor on shadow trades. Compare to backtest's 2.50x profit factor at 49–55% post-cost. If shadow is within 30% of backtest, the edge survives the API integration; we can plan live. If shadow is dramatically worse, identify the gap (slippage, latency, quote staleness, missing tokens) before any live trade is considered.

---

## Reporting

After each phase, push and report back with paths to the artifacts produced and a one-paragraph status. STOP at every gate marked above. Do not chain phases.

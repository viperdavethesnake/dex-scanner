# Phase 3 Pre-Flight — Questions & Responses

**Issued:** 2026-05-25  
**Resolved:** 2026-05-25  
**Purpose:** Capture every open question before Phase 3 (shadow trader implementation) begins. Responses recorded here become the authoritative decisions for the implementation pass.

---

## Q1 — Conviction threshold: 0.65 or 0.70?

**Conflict:** Two sources disagree.

| Source | Value | Evidence |
|---|---|---|
| `docs/ML-FINDINGS.md` | **0.70** | "First-entry ≥0.70 is the most practical strategy" — 55% win rate, 2.50x profit factor, $11 max drawdown on 2.5-day val set |
| `docs/decisions/SHADOW-TRADER-DESIGN.md §9` | **0.65** | Set in the design doc; incorrectly attributed the 2.50x PF to 0.65 (it belongs to 0.70) |
| `analysis/models/metadata.json` | **0.70** | Matches ML-FINDINGS as instructed |

**Trade-off:**
- **0.70** — fewer trades per day (~1–2 on current signal volume), higher precision (61.5% on current val), lower recall. More accurate to what the backtest actually measured.
- **0.65** — more trades (~3–4/day), 60.0% precision, 1.52x PF. More shadow data accumulates faster, useful if the goal is to hit the ≥200-trade gate quickly.

**Response:** Two-threshold system. Use `CONVICTION_THRESHOLD_SHADOW=0.65` as the shadow floor (faster data accumulation toward ≥200-trade gate) and `CONVICTION_THRESHOLD_LIVE=0.70` as the canonical live threshold (ML-FINDINGS optimum). Add `conviction_band TEXT` column to `trades` table: `'shadow_only'` (0.65–0.70) vs `'live_eligible'` (≥0.70). This enables post-hoc analysis of both cohorts before the shadow→live transition.

---

## Q2 — 0x API key (P2)

Needed for Base quote primary path (`api.0x.org/swap/permit2/quote`). Free at `dashboard.0x.org/apps` (1 RPS, 1M calls/month, no payment required).

**Response:** Key provided and in `.env` as `ZEROX_API_KEY`. Verified: API returns HTTP 400 (valid auth — sentinel taker address `0x000...001` rejected by 0x param validation, not auth failure). 0x `issues.balance` warnings with sentinel are expected in shadow mode and should be silently ignored.

---

## Q3 — Alchemy Base URL (P3)

Needed for Aerodrome and Uniswap V3 on-chain fallback quotes (web3.py). Free at `dashboard.alchemy.com`. Format: `https://base-mainnet.g.alchemy.com/v2/YOUR_KEY`.

Public Base RPC (`https://mainnet.base.org`) is the documented fallback if Alchemy is not set up yet — Phase 3 can start without it, but on-chain fallback quotes will be rate-limited.

**Response:** Alchemy Base URL provided and in `.env` as `ALCHEMY_BASE_URL`. Verified: HTTP 200 response from Alchemy endpoint. Alchemy is the primary RPC; public Base RPC is the fallback if unset.

---

## Q4 — Jupiter API key (P4)

Needed for Solana swap quotes. Solana is deferred — this is wired now so the module compiles and the key is in `.env.example`. Free at `developers.jup.ag/portal`.

Can be left blank for now (Jupiter module raises `NotImplementedError` in shadow mode). Not a Phase 3 blocker.

**Response:** Skip for now. `JUPITER_API_KEY` placeholder added to `.env.example`. Jupiter module stubs `NotImplementedError`; Solana is deferred to Phase 5+.

---

## Q5 — OQ4: ETH/USD price for gas cost calculation

The `cost_pct` field in `trades` requires converting `estimatedGas × gasPrice` to USD. Two options:

- **A. Coinbase public API** — `https://api.coinbase.com/v2/prices/ETH-USD/spot` (free, no key, cache 10min)
- **B. Fixed $3000 estimate** — rough constant; simpler, slightly less accurate

Either works for shadow mode. Choice affects `cost_pct` precision but not the primary `cost_delta_pct` measurement (which compares real vs assumed cost, not absolute gas USD).

**Response:** Coinbase public API. Cache 600s (`ETH_USD_CACHE_SECONDS=600`), hardcoded $3000 fallback if API unreachable. Implemented as `eth_price.py` module. Env var `ETH_USD_PRICE_URL=https://api.coinbase.com/v2/prices/ETH-USD/spot` in compose.

---

## Q6 — OQ5: `analysis/check_0x_coverage.py` scope

The design doc calls for a one-off script that queries the last 7 days of collector Base candidates and calls 0x for each to measure `liquidityAvailable` true/false rate by liquidity bucket. This would replace the ~10–25% estimate in the design with a real number.

Options:
- **A. Include in Phase 3** — write the script alongside the trader scaffold
- **B. Defer to Phase 4 pre-work** — run it manually after Phase 3 deploy, before first checkpoint review

**Response:** Phase 4. The 10–25% Aerodrome fallback estimate in SHADOW-TRADER-DESIGN.md §12 is sufficient for Phase 3. After 48h of shadow data, `SELECT quote_source, COUNT(*) FROM trades GROUP BY 1` gives the real empirical rate. The coverage script runs in Phase 4 pre-work before the first checkpoint review.

---

## Q7 — Wallet private key

Needed for Phase 3 code path design (key is read at startup only when `SHADOW_MODE=false`). In shadow mode the key is never loaded.

For Phase 3 implementation, the code needs to know the env var name and the address that will be used (for 0x `taker` parameter). A placeholder address can be used in shadow mode.

**Response:** Use sentinel `0x0000000000000000000000000000000000000001` in shadow mode — key is never loaded when `SHADOW_MODE=true`. `TRADER_WALLET_PRIVATE_KEY` env var will be provided before live mode. Do NOT add a real private key to `.env` until the shadow→live transition. 0x `issues.balance` warnings caused by the sentinel are expected and logged but not treated as errors.

---

## P5 — `./trader_data/` directory

**Response:** Created in the Phase 3 implementation commit (`mkdir -p trader_data && echo "# TimescaleDB volume — gitignored" > trader_data/.gitkeep`). The directory is gitignored (matches `timescale_data/` pattern; will add explicit entry for `trader_data/`).

---

## Summary checklist

| # | Item | Status |
|---|---|---|
| Q1 | Conviction threshold (0.65 vs 0.70) | ✅ Two-threshold: SHADOW=0.65, LIVE=0.70; conviction_band column |
| Q2 | 0x API key | ✅ In `.env`; verified (sentinel 400 is expected behavior, not auth failure) |
| Q3 | Alchemy Base URL | ✅ In `.env`; verified 200 |
| Q4 | Jupiter API key | ✅ Skipped (placeholder in `.env.example`; Solana deferred) |
| Q5 | ETH/USD gas price source | ✅ Coinbase public API, 600s cache, $3000 fallback |
| Q6 | `check_0x_coverage.py` scope | ✅ Deferred to Phase 4 pre-work |
| Q7 | Wallet private key / taker address | ✅ Sentinel `0x000...001` in shadow; real key at shadow→live transition |
| P5 | `./trader_data/` directory created on host | ✅ Created in Phase 3 implementation commit |

# Next Steps

**Issued:** 2026-05-23
**Status:** Pending execution
**Owner:** Claude Code

Two parallel tasks. Task A is a build with a design-approval gate; Task B is read-only research. Both target ROADMAP.md Phase 1's biggest gap (Birdeye enrichment in the collector) and the strategic question of whether/when to pay for Solana Birdeye access.

---

## Context

The dex-scanner repo has been cleaned and re-baselined. Current state:

- Birdeye free tier (Standard, 30k CUs/month). New key is in `.env`, wired into n8n credential `LWcHDmU166QRmUAv` for the existing n8n Birdeye Enricher workflow. **Do NOT modify or break that workflow** — it continues enriching the scanner DB for INTERESTING/WATCH tokens.
- Confirmed 2026-05-23: Base `token_overview` returns HTTP 200 on free tier; Solana `token_overview` returns HTTP 400 "Compute units usage limit exceeded" (Birdeye gates Solana data behind paid tier).
- Strategic decision: defer Solana paid tier. Port enrichment to the collector for Base only. Solana data accumulates in the collector without Birdeye enrichment (still feeds the ML model on non-Birdeye features).

The end goal of Task A: collector `raw_signals` rows for Base tokens carry `unique_traders_1h` and `net_inflow_usd` at insert time, so the rolling ML retrain has these features available. This is ROADMAP.md Phase 1's "single biggest gap."

---

## Task A — Collector Birdeye enrichment (Base-only)

### Step 1: Investigate first, do not write code yet

1. Read the collector source: `collector/main.py`, `collector/api.py`, `collector/signals.py`, `collector/db.py`, `collector/init.sql`, `collector/Dockerfile`. Report the poll loop structure, schema of `raw_signals`, and where signal computation/insert happens.
2. Confirm whether `raw_signals` already has columns `unique_traders_1h` (INT) and `net_inflow_usd` (NUMERIC). If not, design a migration.
3. Read the existing n8n Birdeye Enricher workflow (`workflows/dex-birdeye-enricher.json`) and document the exact endpoint, headers, request format, and response parsing it uses. We will reuse this logic verbatim — same endpoint, same field names extracted (`uniqueWallet1h`, `vBuy1hUSD - vSell1hUSD`).
4. Check `compose.yaml` — confirm `BIRDEYE_API_KEY` is already injected into the collector container's env. If not, propose the addition.

### Step 2: Present design before implementing

Write a short design proposal as `analysis/COLLECTOR-BIRDEYE-DESIGN.md` covering:

- Schema migration (if needed)
- Where in the poll loop the enrichment fires
- Error handling strategy (4xx, 5xx, timeout, network)
- Sampling controls (see Step 3 below)
- Instrumentation (CU tracking, success/failure counts, per-cycle stats)
- A rollback plan if the enrichment causes problems

**STOP after writing the design. Wait for user approval before implementing.**

### Step 3: Required design constraints

These are non-negotiable. Build the design around them.

**Chain filter — Base only.**

- Before any Birdeye call, check `chain == 'base'`. Solana tokens skip the enrichment entirely. Log skipped count per cycle so we know the Solana coverage gap.
- Do NOT call Birdeye with `x-chain: solana` under any circumstance. Calling it and handling the 400 wastes CU and pollutes the dashboard with avoidable errors.

**Feature flag — disabled by default.**

- New env var: `COLLECTOR_BIRDEYE_ENRICHMENT=false` (default).
- When false, the enrichment code path is a no-op. The collector continues as today.
- User flips to true in `.env` after reviewing the design and tail-following the first hour of logs.

**Sampling controls — start very conservative.**

- New env var: `COLLECTOR_BIRDEYE_SAMPLE_RATE=0.2` (default — enrich 1 in 5 Base tokens).
- Random sampling per row, deterministic seed per token+timestamp so the same token gets sampled consistently within a window (avoids the n8n enricher and the collector double-enriching the same token).
- New env var: `COLLECTOR_BIRDEYE_MAX_PER_CYCLE=20` (default — hard cap per 5-min poll cycle as a circuit breaker).
- Rationale: free tier is 30k CUs/month ≈ 1k/day ≈ 3.5/cycle. Without sampling we'd burn the monthly budget in hours. With these defaults we should stay under 6k CUs/day worst case, comfortably under free tier.

**Caching `/defi/networks`.**

- Call once at collector startup. Store in memory. Refresh weekly (or on restart, whichever comes first).
- Do not call this endpoint per-token. Ever.

**Per-call instrumentation, written to a new table `birdeye_calls`:**

```sql
CREATE TABLE IF NOT EXISTS birdeye_calls (
  called_at      TIMESTAMPTZ NOT NULL,
  endpoint       TEXT NOT NULL,
  chain          TEXT NOT NULL,
  address        TEXT,
  http_status    INT,
  cu_consumed    INT,            -- from response header `x-rate-limit-cu` if present, else NULL
  response_ms    INT,
  error_message  TEXT
);
SELECT create_hypertable('birdeye_calls', 'called_at', if_not_exists => TRUE);
```

This is the data needed to make the tier decision intelligently after a day of running.

**Error handling.**

- Timeout: 5 seconds, mark row as enrichment-failed (NULL columns), continue.
- 4xx: log to `birdeye_calls`, mark row as enrichment-failed, continue.
- 5xx / network: same, continue.
- Never throw out of the enrichment code into the main poll loop. Collector reliability > Birdeye coverage.

**Reuse the existing field extraction logic.**

- `uniqueWallet1h` → `unique_traders_1h` (int)
- `vBuy1hUSD - vSell1hUSD` → `net_inflow_usd` (rounded numeric)
- Use the same null/error guards as the n8n enricher's Prep Birdeye Update node.

### Step 4: Implement after approval

Once the user approves the design:

- Apply schema migration to `dex-collector-db` (the collector's own Timescale at port 5434).
- Update collector code per design.
- Update `compose.yaml` if env additions needed.
- Update `.env.example` with the new feature-flag vars (placeholder values).
- Add a section to `RESUME.md` documenting the addition.
- Rebuild and restart `dex-collector` only. Do not touch n8n or the scanner stack.

### Step 5: First-hour validation

After deploy, monitor for 60 minutes:

```sql
SELECT chain, http_status, COUNT(*), AVG(cu_consumed), AVG(response_ms)
FROM birdeye_calls
WHERE called_at > NOW() - INTERVAL '1 hour'
GROUP BY 1, 2;
```

Expected: 100% Base chain (no Solana attempts logged), >95% HTTP 200, CU consumed per call in the single digits, response_ms under 500.

If anything is off, set `COLLECTOR_BIRDEYE_ENRICHMENT=false` and report.

Report back to the user with the first-hour stats.

---

## Task B — Solana paid tier research (read-only, parallel)

Pull and analyze:

1. `https://docs.birdeye.so/docs/data-accessibility-by-packages` — full matrix of which endpoints work on which tier.
2. `https://docs.birdeye.so/docs/compute-unit-cost` — per-endpoint CU cost, especially for `/defi/token_overview` on Solana vs Base.
3. `https://docs.birdeye.so/docs/pricing` — re-confirm tier limits and overage pricing.

Then produce `analysis/BIRDEYE-SOLANA-TIER-RESEARCH.md` containing:

### Section 1 — Eligibility

For each tier (Standard, Lite, Starter, Premium, Business), does `/defi/token_overview` work on Solana? Yes/No table. If "No" up to some tier, that's the floor; CU math below that floor is moot.

### Section 2 — CU cost model

- CU cost per call for Solana `token_overview` at the lowest eligible tier.
- Compare to Base `token_overview` CU cost (works on free).
- If Birdeye has a cheaper endpoint that returns the same two fields (`uniqueWallet1h` + buy/sell volume), call it out as an alternative.

### Section 3 — Volume projection

Query the collector to get realistic numbers:

```sql
SELECT chain, COUNT(DISTINCT address) AS unique_tokens, COUNT(*) AS total_rows
FROM raw_signals
WHERE scanned_at > NOW() - INTERVAL '7 days'
GROUP BY chain;
```

Apply the same 0.2 sample rate and 20/cycle cap defaults from Task A. Compute:

- Projected Solana CU consumption per day at full sampling
- Projected Solana CU consumption per day at conservative sampling (matching Base defaults)
- Combined Base+Solana monthly CU burn at conservative defaults

### Section 4 — Recommendation matrix

| Tier | Monthly cost | Included CUs | Projected burn (conservative) | Projected overage | Total monthly cost | Notes |
|---|---|---|---|---|---|---|

Pick the tier that minimizes total monthly cost at the projected conservative burn. Note where the next tier up would make sense (e.g., "if scaling sampling to 1.0, jump to X").

### Section 5 — Decision

A one-paragraph recommendation: "stay on free tier and defer Solana enrichment indefinitely" vs "upgrade to $X tier when Base auto-trading is profitable" vs "the $39 Lite tier is cheap enough to enable now for research value."

This is read-only research. No code changes. No purchases. Report back with the document.

---

## Reporting

When both tasks are complete (Task A blocked at design-approval gate, Task B complete):

1. Path to `analysis/COLLECTOR-BIRDEYE-DESIGN.md`
2. Path to `analysis/BIRDEYE-SOLANA-TIER-RESEARCH.md`
3. Summary of any unknowns or ambiguities encountered
4. Wait for the user to approve the design before implementing Task A Step 4 and Step 5.

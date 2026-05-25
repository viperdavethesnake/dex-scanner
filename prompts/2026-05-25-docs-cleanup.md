# Docs Cleanup — Accuracy Pass

**Issued:** 2026-05-25
**Status:** Docs-only. No code changes. No service restarts.
**Owner:** Claude Code

Sweep the repo docs to reflect current state and strip stale requirements. The user is sensitive to scope creep and to securities-market metrics misapplied to crypto memecoin scalping. Every change must be defensible against either of those filters.

This is a careful editorial pass, not a rewrite. Preserve voice, structure, and section headings unless they're factually wrong.

---

## Files in scope (in priority order)

1. `docs/ROADMAP.md` — most stale, highest priority
2. `docs/ML-FINDINGS.md` — audit for stale numbers and any securities-thinking
3. `docs/THESIS.md` — light audit (likely clean — explicitly rejects securities thinking already)
4. `docs/RESUME.md` — minor update for in-flight work
5. `README.md` — minor cross-reference fixes only
6. `CLAUDE.md` — no changes expected (already updated for hygiene work)

DO NOT touch:
- `docs/PIPELINE.md` — wait for trader implementation
- `docs/decisions/*` — these are historical records, leave alone
- `prompts/*` — historical records, leave alone

---

## ROADMAP.md — required changes

Apply each of the following. After each, verify the surrounding text still reads naturally.

**1. Current State table — update statuses:**

| Component | New status |
|---|---|
| Birdeye enrichment in collector | ✅ Live (2026-05-23, Base only, sample 0.02) |
| ML model | ✅ Built (LightGBM, Base 0.67 / Solana 0.57 AUC), pending deployment via shadow trader |
| Shadow trader design | ✅ Approved 2026-05-25, see `docs/decisions/SHADOW-TRADER-DESIGN.md` |
| Execution layer | 🛠️ In progress (shadow mode first) |

**2. Phase 1 — replace the data gate:**

DELETE the line "Gate to Phase 2: 4+ weeks of data, stable out-of-sample AUC ≥ 0.62 on a rolling test set."

REPLACE with:

```
Gate to Phase 2: shadow trader running for ≥5 days AND average cost_delta_pct
(measured real cost vs backtest 1.5% assumption) stays under +1.0 percentage points.
Time-based gates do not apply here — each token launch is an independent ~hour-long
event, not a market trend. We already have thousands of independent samples in the
collector. The remaining unknown is execution cost, which only live aggregator quotes
can answer.
```

ALSO remove the "Switch training target to >10% move (AUC 0.684...)" bullet — that's an experiment record, not a roadmap item. Move it to ML-FINDINGS.md if it isn't already there.

**3. Phase 2 — fix the loop cadence:**

Change "Every 5 minutes:" to "Every 5 seconds:" in the decision loop pseudo-code. The collector poll runs every 5 minutes; the trader pickup runs every 5 seconds. This was conflated in the original draft.

**4. Phase 3 — remove "decision to be made" language:**

The Wallet Options section says "Decision to be made based on Coinbase API access available." That decision is made: direct `web3.py` + `eth-account` with `PRIVATE_KEY` env var. See SHADOW-TRADER-DESIGN.md §6. Update the bullet list to mark this as decided and link to the design doc.

Similarly, "DEX routing — 0x API swap endpoint OR direct Uniswap V3 router" is decided: 0x primary, Aerodrome fallback, Uniswap V3 secondary fallback. Update.

**5. Phase 4 — fix time-based language and securities thinking:**

The paragraph "Common causes: slippage assumption wrong, model threshold needs adjustment, market regime shifted" — strip "market regime shifted." This is a securities/macro concept. For memecoin scalping, "regime" isn't the unit of analysis — each token is its own micro-event. Replace with "or the data distribution has shifted (new launchpad dominant, new bot behavior, new chain conditions)."

Also: "Duration: minimum 2 weeks" — replace with milestone-based: "Duration: until shadow has logged ≥200 completed trades and cost_delta_pct has stabilized within ±0.5pp over a rolling 50-trade window."

**6. Phase 5 — replace all time-based gates with metric-based:**

DELETE: "Start with Base only. Add Solana after 4+ weeks of stable Base performance."
REPLACE: "Start with Base only. Add Solana after Base live has logged ≥30 winning days with profit factor ≥1.5x AND Solana Birdeye access is unblocked (free tier reset test or Lite tier upgrade)."

DELETE: "30+ days of live trading"
REPLACE: "≥200 completed live trades"

DELETE: "Max drawdown within model predictions"
REPLACE: "Max drawdown within ±50% of shadow-measured drawdown (model does not predict drawdown directly; this is a measured stability check, not a prediction match)"

"Profit factor ≥ 1.5x sustained" stays — profit factor is a generic P&L ratio, not securities-specific.

**7. Open Questions section — replace entirely:**

DELETE all four items. They are decided in `docs/decisions/SHADOW-TRADER-DESIGN.md`.

REPLACE with:

```
## Open Questions (live)

Resolved questions are recorded in `docs/decisions/`. Active open questions:

1. **Birdeye tier upgrade timing.** Currently Standard (free), 0.02 sample rate. Lite ($39/mo) unlocks Solana enrichment if reset test (2026-06-24) confirms tier-gating. Decision deferred until Base shadow proves profitable execution.

2. **Solana wallet integration.** Solana is deferred until Base live is stable. When activated: keypair file via `solders` + `solana-py`, Jupiter aggregator. Design stub exists at `dex-trader/aggregators/jupiter.py`.

3. **MEV / sandwich protection.** Shadow mode does not measure MEV cost. Live mode on Base will need either a private RPC (e.g., Flashbots Protect) or accept the MEV tax. Decision deferred until shadow-vs-live cost gap is measured.
```

**8. Architecture diagram — keep but note current vs target:**

The current diagram is correct as the *target* state. Add a "(Phase 3 implementation in progress)" note next to the Trader group lines.

---

## ML-FINDINGS.md — audit pass

Read the current version. Verify:

1. **AUC numbers** — confirm Base 0.67 / Solana 0.57 / overall ~0.61 are the canonical figures. If older numbers appear, update.

2. **Securities-thinking audit.** Search for and flag (do not auto-remove without listing them in the commit message):
   - "Sharpe", "Sortino", "alpha", "beta" — these are securities metrics
   - "correlation matrix", "market regime", "macro" — securities/macro thinking
   - "P/E", "DCF", "fundamentals", "intrinsic value" — investment thinking
   - "long-term", "buy and hold" — wrong timeframe for this strategy
   - "diversification" — applies in some contexts but be suspicious

3. **Conviction threshold** — confirm whether 0.65 or 0.70 is the canonical optimum. The shadow trader design defaults to 0.65; if ML-FINDINGS says 0.70 elsewhere, surface the conflict for the user to resolve.

4. **Hold-duration language** — confirm "5-minute events, not holds" is stated. This is the most important market-structure insight in the project.

If anything is stale or wrong, fix it. Otherwise leave as-is.

---

## THESIS.md — light audit

This file is largely clean. It has an explicit "What Traditional Market Thinking Gets Wrong Here" section that rejects securities thinking.

Only changes:
1. Verify all numbers in the "The Numbers" section at the bottom match current ML-FINDINGS.md.
2. The line "From 19,700+ unbiased observations" — update to the current collector count if materially different (probably ~28,000+ now).
3. No structural changes.

---

## RESUME.md — minor update

Add a "Track 2 — Shadow trader" subsection to the "Pending work" or "Next session" area, with bullets:

- Design approved 2026-05-25, see `docs/decisions/SHADOW-TRADER-DESIGN.md`
- Prereqs: `analysis/export_model.py` (P1), ZEROX/ALCHEMY/JUPITER API keys (P2-P4), `./trader_data/` dir (P5)
- Implementation: Phase 3 pending approval

No other changes needed. RESUME.md is largely current.

---

## README.md — cross-reference fixes only

Verify all links to other docs still resolve (some may point to the old flat-file structure pre-`docs/` reorganization). Fix any that don't.

No structural changes.

---

## CLAUDE.md — no changes expected

This was already updated during the hygiene pass (commit `136afd1`). Skim it for any stale roadmap references, but expect no changes.

---

## Commit message

Single commit with all the changes:

```
Docs: strip stale requirements and securities-market thinking

- ROADMAP.md: remove 4-week data gate (independent-event market structure
  invalidates time-based gates); update Phase 1 current state (Birdeye
  collector enrichment is live, ML model has shadow trader path);
  resolve all 4 Open Questions per SHADOW-TRADER-DESIGN.md; replace
  time-based Phase 4/5 gates with metric-based; strip "market regime"
  language; update Phase 2 loop cadence (5s pickup not 5min)
- ML-FINDINGS.md: AUC numbers verified canonical; securities-metric audit
- THESIS.md: number cross-check
- RESUME.md: add Track 2 shadow trader pending bullets
- README.md: doc link cross-references

No code changes. No requirement of stack restart.
```

---

## After commit

Report back with:
1. A diff summary of what changed in ROADMAP.md (the biggest delta)
2. The list of securities-thinking flags found in ML-FINDINGS.md (if any)
3. Any conflicts between docs you couldn't resolve without user input (e.g., conviction threshold canonical value)

Do not proceed to Phase 3 (shadow trader implementation) yet. Cleanup first, then user decides next step.

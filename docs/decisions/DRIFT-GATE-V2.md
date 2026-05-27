# Drift Gate v2 — Asymmetric momentum-aligned entry filter

**Date:** 2026-05-27  
**Status:** Implemented  
**Commit:** "Drift gate v2 (momentum-aligned) + exit-quote fallback cost estimates + w3 diagnostic"

---

## Problem

The original drift gate (`QUOTE_DRIFT_MAX_PCT=3.0`) was one-sided:

```python
if drift_pct > +3.0%:   # reject — token pumped since signal scan
    skip                 # silently accept ALL reversals (any negative drift)
```

**Evidence from 62 shadow trades (2026-05-25 → 2026-05-27):**

| Outcome | Count | Note |
|---------|-------|------|
| Skipped by drift gate | 44 | All positive drift — token still pumping |
| Skipped (no quote) | 1 | |
| Filled + exited | 17 | Negative or near-zero drift — token flat/reversing |

Win rate on 17 fills: **23.5%** (4W / 12L / 1 zero)  
Model's stated precision at 0.65 threshold: **56.5%**  
Gap: **33 percentage points** — attributable to selection bias from the gate.

### Why this is adversarial to a momentum strategy

The model was trained on raw DexScreener signals. A high-score signal means the model detected strong momentum indicators at scan time. Between scan and entry (typically 1–5 minutes):

- **Token continues pumping** → positive drift → old gate rejects → we miss the model's best signals
- **Token reverses** → negative drift → old gate passes → we enter an already-failed signal

The gate was literally inverting the model's selections.

---

## Solution — Asymmetric gate

```python
QUOTE_DRIFT_DOWN_MAX_PCT = 1.5   # reject if token dropped >1.5% since scan
QUOTE_DRIFT_UP_MAX_PCT   = 15.0  # reject if token already extended >15% since scan
```

**Down gate (`< -1.5%`):** Token has reversed since the scan. Momentum failed before we could enter. Skip.  
- 1.5% is ~1 DexScreener polling cycle's worth of noise — genuine reversals are larger.

**Up gate (`> +15%`):** Token has already extended significantly. For a 5-minute scalp, entering after a 15%+ pre-entry move means chasing a move that has already consumed most of its near-term upside. The remaining edge per-minute drops sharply after large pre-entry extension.  
- 15% is a calibrated starting point. Revisit after 200+ fills with drift distribution data.
- Retains all signals in the 0–15% "confirming" zone — the heart of the momentum signal.

**Result:** The gate now *allows* the confirming-momentum signals the model is strongest on, and *rejects* the two failure modes (reversal, over-extension). This aligns the entry filter with the model's assumptions.

---

## New failure_reason strings

| Old | New | Meaning |
|-----|-----|---------|
| `quote_drift:X.X%` | `momentum_failed:+X.XX%` | Token reversed > -1.5% |
| (not detected) | `drift_too_high:+X.XX%` | Token extended > +15% |

Legacy `quote_drift:%` rows (trades 1–62) are preserved unchanged.

---

## What to watch in Phase 4 checkpoint

After 100+ new fills under v2 gate:

1. **Fill rate** should increase substantially (most previously-rejected tokens were in 0–15% range).
2. **Win rate** should move toward model precision (target: 45–55% on `live_eligible` band).
3. **`drift_too_high` distribution** — if this is firing rarely (< 10%), the 15% ceiling might be droppable; if it's firing on 20%+, the model is scoring tokens after large run-ups and the up-gate is earning its keep.
4. **`momentum_failed` rate** — if it's high (>20% of candidates), the 1.5% floor may be too tight. Adjust to 3% if needed.

---

## Related fixes (same commit)

- **Exit-quote fallback cost estimates:** when 0x returns None at exit, apply `FALLBACK_EXIT_GAS_USD=0.20` and `FALLBACK_EXIT_SLIPPAGE_BPS=300` instead of zero. Prevents P&L inflation on illiquid-exit trades. Identify via `exit_quote_source='dexscreener_fallback'`.
- **web3 diagnostic:** surfaces actual exception from `is_connected()` instead of silently logging `connected=False`. Post-restart, the actual web3.py error will be visible in logs.

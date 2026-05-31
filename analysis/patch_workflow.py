"""Patch the dex-scanner workflow JSON with ML tuning changes."""
import json, sys, os

WF_PATH = os.path.join(os.path.dirname(__file__), '..', 'workflows', 'dex-scanner-workflow.json')

with open(WF_PATH) as f:
    wf = json.load(f)

nodes = {n['id']: n for n in wf['nodes']}
bp = nodes['build-prompt']
code = bp['parameters']['jsCode']

# ── Change 1: Solana filter — allow recovering when vol_trend=rising ──────────
OLD_FILTER = (
    "  const microPass = chain === 'solana'\n"
    "    ? (micro !== 'recovering' && micro !== 'down' && micro !== 'flat')\n"
    "    : (micro !== 'recovering' && micro !== 'down');"
)
NEW_FILTER = (
    "  const microPass = chain === 'solana'\n"
    "    ? ((micro !== 'recovering' || item.json.volTrend === 'rising') && micro !== 'down' && micro !== 'flat')\n"
    "    : (micro !== 'recovering' && micro !== 'down');"
)
if OLD_FILTER not in code:
    print("ERROR: old filter string not found", file=sys.stderr); sys.exit(1)
code = code.replace(OLD_FILTER, NEW_FILTER, 1)
print("Change 1 applied: Solana recovering+rising filter exception")

# ── Change 2: Inject liq_mcap_ratio + momentum_score computation ──────────────
OLD_FLAGS = "  const flags = t.safetyFlags.length > 0 ? 'FLAGS: ' + t.safetyFlags.join(', ') : 'Clean';"
NEW_FLAGS = (
    "  // ML-validated derived signals\n"
    "  const liqMcapRatio = (t.liquidity > 0 && t.marketCap > 0)\n"
    "    ? (t.liquidity / t.marketCap).toFixed(3) : null;\n"
    "  const vol5m1hRatio = t.volume1h > 0 ? (t.volume5m * 12 / t.volume1h) : null;\n"
    "  const momentumScore = (vol5m1hRatio !== null && t.priceChange5m != null)\n"
    "    ? (t.priceChange5m * Math.min(vol5m1hRatio, 10.0)).toFixed(2) : null;\n"
    "\n"
    "  const flags = t.safetyFlags.length > 0 ? 'FLAGS: ' + t.safetyFlags.join(', ') : 'Clean';"
)
if OLD_FLAGS not in code:
    print("ERROR: flags marker not found", file=sys.stderr); sys.exit(1)
code = code.replace(OLD_FLAGS, NEW_FLAGS, 1)
print("Change 2 applied: liq_mcap_ratio + momentum_score computation added")

# ── Change 3: Add new signals to the per-token prompt line ───────────────────
OLD_VOL = "    + '\\n   Vol5m: $' + Math.round(t.volume5m).toLocaleString() + ' | Vol1h: $' + Math.round(t.volume1h).toLocaleString()"
NEW_VOL = (
    "    + '\\n   Vol5m: $' + Math.round(t.volume5m).toLocaleString() + ' | Vol1h: $' + Math.round(t.volume1h).toLocaleString()\n"
    "    + (liqMcapRatio ? '\\n   Liq/MCap: ' + liqMcapRatio + 'x' + (momentumScore ? ' | Momentum score: ' + momentumScore : '') : '')"
)
if OLD_VOL not in code:
    print("ERROR: vol line not found — searching for context...")
    idx = code.find('Vol5m')
    print(repr(code[idx:idx+300]))
    sys.exit(1)
code = code.replace(OLD_VOL, NEW_VOL, 1)
print("Change 3 applied: Liq/MCap and Momentum score added to prompt line")

# ── Change 4: Add ML-calibrated signals paragraph to system prompt ────────────
ML_ADDENDUM = (
    "\n\nML-CALIBRATED SIGNALS (from 25,000+ unbiased observations, 14 days):\n"
    "- Base: buys_5m (raw 5m buy count) and Liq/MCap ratio are the top predictors of >=20% moves. "
    "High buys_5m + Liq/MCap < 0.3 = maximum conviction setup. Fading micro-trend on Base often "
    "precedes a surge — treat as consolidation, not decay.\n"
    "- Solana: volume_5m dollar magnitude and Liq/MCap ratio dominate. Volume spike with Liq/MCap "
    "< 0.3 = strongest Solana signal. Focus on volume magnitude, not VL ratio (already pre-filtered).\n"
    "- Both chains: rising vol_trend + low Liq/MCap is the cleanest observed pattern. Momentum score "
    "(shown when available) combines price velocity with volume acceleration — large positive "
    "reinforces INTERESTING; near-zero or negative argues WATCH/SKIP."
)

# The system string ends with the SIGNAL WARNINGS sentence then a closing '
SYS_END_MARKER = "eaningful signal\\n- SIGNAL WARNINGS in the data = filter borderline cases — factor into conviction sizing, not automatic skip';"
if SYS_END_MARKER not in code:
    print("ERROR: system prompt end marker not found", file=sys.stderr)
    idx = code.rfind('SIGNAL WARNINGS')
    print(repr(code[idx-30:idx+200]))
    sys.exit(1)

NEW_SYS_END = (
    "eaningful signal\\n- SIGNAL WARNINGS in the data = filter borderline cases — factor into conviction sizing, not automatic skip"
    + ML_ADDENDUM.replace('\n', '\\n').replace('—', '—') + "';"
)
code = code.replace(SYS_END_MARKER, NEW_SYS_END, 1)
print("Change 4 applied: ML-calibrated signals paragraph added to system prompt")

# ── Write back ────────────────────────────────────────────────────────────────
bp['parameters']['jsCode'] = code
wf['nodes'] = [n if n['id'] != 'build-prompt' else bp for n in wf['nodes']]

with open(WF_PATH, 'w') as f:
    json.dump(wf, f, indent=2)

print("\nAll changes written to dex-scanner-workflow.json")

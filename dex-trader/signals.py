"""Hard filters — scanner replica.

KEEP IN SYNC WITH: n8n workflow "Safety Filter" node (dex-scanner-workflow.json)
Filter version: Phase 10 (2026-06-06)
"""

BASE_VL_CEILING    = 10.0
SOLANA_VL_CEILING  = 4.0
AGE_MIN_MINUTES    = 15
AGE_MAX_MINUTES    = 90


def hard_filter(signal: dict) -> tuple[bool, str]:
    """Returns (passes, reason). reason is '' when passes=True."""
    age   = signal.get("age_minutes") or 0
    vl    = signal.get("vl_ratio")    or 0.0
    chain = signal.get("chain")       or ""

    if not (AGE_MIN_MINUTES <= age <= AGE_MAX_MINUTES):
        return False, f"age_out_of_window:{age:.0f}m"

    vl_ceil = BASE_VL_CEILING if chain == "base" else SOLANA_VL_CEILING
    if vl > vl_ceil:
        return False, f"vl_too_high:{vl:.1f}>{vl_ceil}"

    return True, ""

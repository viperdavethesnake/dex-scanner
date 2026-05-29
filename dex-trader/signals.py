"""Hard filters — scanner replica.

KEEP IN SYNC WITH: n8n workflow "Safety Filter" node (dex-scanner-workflow.json)
Filter version: Phase 9 (2026-05-23)
"""
import os

BASE_VL_CEILING    = 8.0
SOLANA_VL_CEILING  = 4.0
AGE_MIN_MINUTES    = 15
AGE_MAX_MINUTES    = 90
SELL_PRESSURE_MAX_5M = float(os.environ.get("SELL_PRESSURE_MAX_5M", "0.70"))

# micro_trend values excluded by chain
EXCLUDED_MICRO = {
    "base":   {"down", "recovering"},
    "solana": {"down", "recovering", "flat"},
}


def hard_filter(signal: dict) -> tuple[bool, str]:
    """Returns (passes, reason). reason is '' when passes=True."""
    age   = signal.get("age_minutes") or 0
    vl    = signal.get("vl_ratio")    or 0.0
    micro = signal.get("micro_trend") or ""
    chain = signal.get("chain")       or ""

    if not (AGE_MIN_MINUTES <= age <= AGE_MAX_MINUTES):
        return False, f"age_out_of_window:{age:.0f}m"

    vl_ceil = BASE_VL_CEILING if chain == "base" else SOLANA_VL_CEILING
    if vl > vl_ceil:
        return False, f"vl_too_high:{vl:.1f}>{vl_ceil}"

    excluded = EXCLUDED_MICRO.get(chain, set())
    if micro in excluded:
        return False, f"micro_excluded:{micro}"

    # Sell pressure 5m — reject tokens where sellers dominate buyers
    buys_5m  = signal.get("buys_5m")  or 0
    sells_5m = signal.get("sells_5m") or 0
    total_5m = buys_5m + sells_5m
    if total_5m > 0:
        sell_pressure_5m = sells_5m / total_5m
        if sell_pressure_5m > SELL_PRESSURE_MAX_5M:
            return False, f"sell_pressure_too_high:{sell_pressure_5m:.2f}"

    return True, ""

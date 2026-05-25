"""Shared feature engineering — single source of truth for DEX Scanner.

Used by:
  - dex-trader/main.py          real-time inference (single-signal dict path)
  - analysis/export_model.py    training batch (applied row-by-row via DataFrame wrapper)

Pure Python + math only — no pandas, no numpy, no training-specific deps.

NaN semantics:
  Zero-denominator ratios return float('nan').
  NaN propagates through momentum_score (matches training; LightGBM treats as missing).
  This matches the pandas vectorised behaviour in export_model.py and is the canonical
  value for both training and inference.
"""
import math


def engineer_features(signal: dict) -> dict:
    """
    Compute derived features from a raw signal dict.

    Input:  signal dict with raw columns from raw_signals table.
    Output: shallow copy of input dict with derived feature columns appended.
            Original columns are preserved unchanged.
    """

    def sdiv(a, b, fill=float("nan")):
        """Safe divide; returns fill when denominator is zero or falsy."""
        try:
            return a / b if b and b > 0 else fill
        except Exception:
            return fill

    vol5m  = float(signal.get("volume_5m")     or 0)
    vol1h  = float(signal.get("volume_1h")     or 0)
    vol6h  = float(signal.get("volume_6h")     or 0)
    liq    = float(signal.get("liquidity_usd") or 0)
    mcap   = float(signal.get("market_cap")    or 0)
    buys5  = float(signal.get("buys_5m")       or 0)
    sell5  = float(signal.get("sells_5m")      or 0)
    buys1h = float(signal.get("buys_1h")       or 0)
    sell1h = float(signal.get("sells_1h")      or 0)
    pch5m  = float(signal.get("price_ch_5m")   or 0)

    e = dict(signal)

    e["vol5m_1h_ratio"]   = sdiv(vol5m * 12, vol1h)
    e["vol1h_6h_ratio"]   = sdiv(vol1h * 6,  vol6h)
    e["liq_mcap_ratio"]   = sdiv(liq,         mcap)
    e["net_txn_5m"]       = buys5 - sell5
    e["net_txn_1h"]       = buys1h - sell1h
    e["txn_accel"]        = sdiv(buys5 * 12,  buys1h)
    e["sell_pressure_5m"] = sdiv(sell5, max(buys5 + sell5, 1))

    # NaN propagates when vol5m_1h_ratio is undefined — matches pandas .clip(upper=10)
    # behaviour on NaN (NaN * anything = NaN; LightGBM handles as missing value).
    vol5m_1h_r = e["vol5m_1h_ratio"]
    if math.isnan(vol5m_1h_r):
        e["momentum_score"] = float("nan")
    else:
        e["momentum_score"] = pch5m * min(vol5m_1h_r, 10.0)

    for col in ["liquidity_usd", "market_cap", "volume_5m", "volume_1h", "volume_6h"]:
        val = float(signal.get(col) or 0)
        e[f"log_{col}"] = math.log1p(val)

    return e

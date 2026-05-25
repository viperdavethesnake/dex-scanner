"""Simulated fill and P&L computation for shadow mode."""
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)
utc = timezone.utc


def compute_entry(signal_price_usd: float, quote_price_usd: float,
                  fill_size_usd: float) -> dict:
    """
    Compute entry fill fields.
    entry_cost_pct measures signal staleness: how much price moved from
    DexScreener's reading to the aggregator quote.
    """
    now = datetime.now(utc)
    entry_cost_pct = 0.0
    if signal_price_usd and signal_price_usd > 0 and quote_price_usd:
        entry_cost_pct = (quote_price_usd - signal_price_usd) / signal_price_usd * 100

    return {
        "fill_ts":        now,
        "fill_size_usd":  fill_size_usd,
        "fill_price_usd": quote_price_usd,
        "entry_cost_pct": round(entry_cost_pct, 4),
    }


def compute_exit(fill_price_usd: float, fill_size_usd: float,
                 exit_quote_price_usd: float, exit_dex_price_usd: float,
                 signal_price_usd: float,
                 gas_usd: float, slippage_bps: int) -> dict:
    """
    Compute exit P&L fields.

    Primary P&L uses aggregator exit quote — same path live mode would use.
    Backtest comparison uses DexScreener price (what backtest.py measured against).

    cost_pct = slippage (bps/100) + gas as % of fill size
    net_pct  = gross_pct - cost_pct
    cost_delta_pct = cost_pct - 1.5 (real cost vs backtest assumption)
    """
    gross_pct = 0.0
    if fill_price_usd and fill_price_usd > 0 and exit_quote_price_usd:
        gross_pct = (exit_quote_price_usd - fill_price_usd) / fill_price_usd * 100

    gas_pct  = (gas_usd / fill_size_usd * 100) if fill_size_usd and fill_size_usd > 0 else 0.0
    cost_pct = (slippage_bps / 100.0) + gas_pct
    net_pct  = gross_pct - cost_pct
    pnl_usd  = fill_size_usd * net_pct / 100.0 if fill_size_usd else 0.0

    # Backtest comparison: DexScreener gross - 1.5% assumed round-trip cost
    backtest_gross = 0.0
    if signal_price_usd and signal_price_usd > 0 and exit_dex_price_usd:
        backtest_gross = (exit_dex_price_usd - signal_price_usd) / signal_price_usd * 100
    backtest_net_pct = backtest_gross - 1.5
    cost_delta_pct   = cost_pct - 1.5

    return {
        "gross_pct":        round(gross_pct, 4),
        "cost_pct":         round(cost_pct, 4),
        "net_pct":          round(net_pct, 4),
        "pnl_usd":          round(pnl_usd, 4),
        "backtest_net_pct": round(backtest_net_pct, 4),
        "cost_delta_pct":   round(cost_delta_pct, 4),
    }

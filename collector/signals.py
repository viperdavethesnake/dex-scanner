"""
Signal computation — exact port of the scanner's Fetch & Process OHLCV
and Build Prompt JavaScript nodes.
"""
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class Token:
    token_address: str
    pair_address: str
    symbol: str
    name: str
    chain: str          # 'base' | 'solana'
    dex: str
    pair_created_at: Optional[float]    # epoch ms
    age_minutes: Optional[float]

    price_usd: Optional[float]
    liquidity_usd: Optional[float]
    market_cap: Optional[float]
    volume_5m: Optional[float]
    volume_1h: Optional[float]
    volume_6h: Optional[float]
    price_ch_5m: Optional[float]
    price_ch_1h: Optional[float]
    price_ch_6h: Optional[float]
    buys_1h: Optional[int]
    sells_1h: Optional[int]
    buys_5m: Optional[int]
    sells_5m: Optional[int]

    # computed
    vl_ratio: Optional[float] = None
    vol_trend: Optional[str] = None
    vol_trend_pct: Optional[float] = None
    micro_trend: Optional[str] = None
    buy_pct_5m: Optional[float] = None
    buy_pct_1h: Optional[float] = None

    # Birdeye enrichment — both chains (Phase 1, 2026-05-30)
    unique_traders_1h:    Optional[int]   = None
    unique_traders_30m:   Optional[int]   = None
    unique_traders_24h:   Optional[int]   = None
    buy_volume_1h_usd:    Optional[float] = None
    sell_volume_1h_usd:   Optional[float] = None
    net_inflow_usd:       Optional[float] = None
    volume_24h_usd:       Optional[float] = None
    buy_volume_24h_usd:   Optional[float] = None
    sell_volume_24h_usd:  Optional[float] = None
    trade_count_1h:       Optional[int]   = None
    trade_count_24h:      Optional[int]   = None
    holder_count_birdeye: Optional[int]   = None
    market_count:         Optional[int]   = None
    last_trade_unix_ts:   Optional[int]   = None
    birdeye_enriched:     bool            = False


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0):
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def from_pair(pair: dict, chain_id: str) -> Token:
    """Build a Token from a DexScreener pair dict."""
    base  = pair.get("baseToken") or {}
    vol   = pair.get("volume") or {}
    pc    = pair.get("priceChange") or {}
    txns  = pair.get("txns") or {}
    h1    = txns.get("h1") or {}
    m5    = txns.get("m5") or {}
    liq   = pair.get("liquidity") or {}

    created_at = pair.get("pairCreatedAt")
    now_ms = time.time() * 1000
    age_min = ((now_ms - created_at) / 60_000) if created_at else None

    chain = "solana" if chain_id == "solana" else "base"

    return Token(
        token_address   = base.get("address", ""),
        pair_address    = pair.get("pairAddress", ""),
        symbol          = base.get("symbol", ""),
        name            = base.get("name", ""),
        chain           = chain,
        dex             = pair.get("dexId", ""),
        pair_created_at = created_at,
        age_minutes     = age_min,
        price_usd       = _safe_float(pair.get("priceUsd")) or None,
        liquidity_usd   = _safe_float(liq.get("usd")) or None,
        market_cap      = _safe_float(pair.get("marketCap")) or None,
        volume_5m       = _safe_float(vol.get("m5")),
        volume_1h       = _safe_float(vol.get("h1")),
        volume_6h       = _safe_float(vol.get("h6")),
        price_ch_5m     = _safe_float(pc.get("m5")),
        price_ch_1h     = _safe_float(pc.get("h1")),
        price_ch_6h     = _safe_float(pc.get("h6")),
        buys_1h         = _safe_int(h1.get("buys")),
        sells_1h        = _safe_int(h1.get("sells")),
        buys_5m         = _safe_int(m5.get("buys")),
        sells_5m        = _safe_int(m5.get("sells")),
    )


def compute_signals(t: Token) -> Token:
    """Compute derived signals in-place, then apply filter logic."""
    v5m = t.volume_5m or 0.0
    v1h = t.volume_1h or 0.0
    liq = t.liquidity_usd or 0.0

    # V/L ratio
    t.vl_ratio = round(v1h / liq, 4) if liq > 0 else None

    # vol trend
    if v5m > 0 and v1h > 0:
        projected = v5m * 12
        pct = ((projected - v1h) / v1h) * 100
        t.vol_trend_pct = round(pct, 1)
        t.vol_trend = "rising" if pct >= 30 else "falling" if pct <= -30 else "flat"

    # micro trend
    p5m = t.price_ch_5m or 0.0
    p1h = t.price_ch_1h or 0.0
    if p5m > 2 and p1h > 0:
        t.micro_trend = "up"
    elif p5m < -2 and p1h < 0:
        t.micro_trend = "down"
    elif p5m > 2 and p1h < 0:
        t.micro_trend = "recovering"
    elif p5m < -2 and p1h > 0:
        t.micro_trend = "fading"
    else:
        t.micro_trend = "flat"

    # buy pressure
    total_5m = (t.buys_5m or 0) + (t.sells_5m or 0)
    total_1h = (t.buys_1h or 0) + (t.sells_1h or 0)
    t.buy_pct_5m = round((t.buys_5m or 0) / total_5m * 100, 1) if total_5m > 0 else None
    t.buy_pct_1h = round((t.buys_1h or 0) / total_1h * 100, 1) if total_1h > 0 else None

    return t

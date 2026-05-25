"""Shared Quote dataclass for all aggregator modules."""
from dataclasses import dataclass, field


@dataclass
class Quote:
    source:        str              # '0x' | 'aerodrome' | 'uniswap_v3' | 'jupiter'
    price_usd:     float            # effective fill price per token in USD
    slippage_bps:  int              # price impact in basis points
    gas_usd:       float            # estimated gas cost in USD
    route_summary: str              # human-readable: "0x (Uniswap_V3)"
    raw_response:  dict = field(default_factory=dict)
    latency_ms:    int = 0

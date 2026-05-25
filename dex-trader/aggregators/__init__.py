"""Aggregator dispatcher — unified Quote dataclass and get_quote() entry point.

Priority order for Base:
  1. 0x Swap API v2 (zerox.py)      — preferred; liquidityAvailable=false → fallthrough
  2. Aerodrome Router (aerodrome.py) — on-chain; volatile pool; single-hop then two-hop
  3. Uniswap V3 QuoterV2 (uniswap.py) — on-chain; tries 1%, 0.3%, 0.05% fee tiers

Solana: raises NotImplementedError (Phase 5+).
Never raises — all exceptions are caught internally; returns None on total failure.
"""
import logging
from typing import Optional

from .types import Quote
from . import zerox, aerodrome, uniswap, jupiter

log = logging.getLogger(__name__)

__all__ = ["Quote", "get_quote"]


def get_quote(
    token_address:  str,
    chain:          str,
    sell_usd:       float,
    w3              = None,
    taker_address:  str   = None,
    direction:      str   = "buy",
    fill_price_usd: float = None,
) -> Optional[Quote]:
    """
    Return first successful Quote from aggregators in priority order, or None.

    Args:
        token_address:  ERC20 token to buy/sell
        chain:          'base' | 'solana'
        sell_usd:       USDC amount to spend (buy) or position size in USD (sell)
        w3:             web3.Web3 instance for on-chain fallbacks (None skips Aerodrome/Uniswap)
        taker_address:  wallet address for 0x taker param (ephemeral in shadow mode)
        direction:      'buy' (USDC→token) or 'sell' (token→USDC)
        fill_price_usd: fill price from entry (used to estimate token amount for exit quotes)
    """
    if chain == "solana":
        raise NotImplementedError("Solana trading deferred to Phase 5+")

    if chain != "base":
        return None

    # 1. 0x
    try:
        q = zerox.quote(token_address, sell_usd, taker_address,
                        direction=direction, fill_price_usd=fill_price_usd)
        if q:
            return q
    except Exception as exc:
        log.warning("0x quote error for %s: %s", token_address[:12], exc)

    # 2. Aerodrome
    try:
        q = aerodrome.quote(token_address, sell_usd, w3,
                            direction=direction, fill_price_usd=fill_price_usd)
        if q:
            return q
    except Exception as exc:
        log.warning("aerodrome error for %s: %s", token_address[:12], exc)

    # 3. Uniswap V3
    try:
        q = uniswap.quote(token_address, sell_usd, w3,
                          direction=direction, fill_price_usd=fill_price_usd)
        if q:
            return q
    except Exception as exc:
        log.warning("uniswap v3 error for %s: %s", token_address[:12], exc)

    log.warning("no quote for %s on %s (all aggregators failed)", token_address[:12], chain)
    return None

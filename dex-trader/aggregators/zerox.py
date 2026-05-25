"""0x Swap API v2 — primary quote source for Base tokens.

Endpoint: https://api.0x.org/swap/permit2/quote
Auth: 0x-api-key header (from ZEROX_API_KEY env var)

Shadow-mode note:
  taker_address is an ephemeral keypair address (Account.create()).
  0x rejects addresses <= 0x000000000000000000000000000000000000ffff with HTTP 400.
  Sentinel addresses (0x000...001) will fail. Always use Account.create() in shadow mode.

liquidityAvailable=false → return None (no error, no log; falls through to Aerodrome).
"""
import logging
import os
import time
from typing import Optional

import requests

from .types import Quote
from eth_price import get_eth_usd
from token_decimals import get_decimals

log = logging.getLogger(__name__)

ZEROX_API_KEY = os.environ.get("ZEROX_API_KEY", "")
ZEROX_URL     = "https://api.0x.org/swap/permit2/quote"
ZEROX_TIMEOUT = 4

USDC_BASE     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6


def quote(token_address: str, sell_usd: float, taker_address: str,
          direction: str = "buy", fill_price_usd: float = None,
          w3=None) -> Optional[Quote]:
    """
    direction='buy':  USDC → token (entry). sell_usd = USDC amount.
    direction='sell': token → USDC (exit).  sell_usd ≈ position USD value.
                      fill_price_usd used to compute approximate token amount.
    Returns None on liquidityAvailable=false, HTTP error, or missing API key.
    """
    if not ZEROX_API_KEY:
        log.warning("ZEROX_API_KEY not set — skipping 0x quote")
        return None

    headers = {
        "0x-api-key": ZEROX_API_KEY,
        "0x-version": "v2",
    }

    if direction == "buy":
        sell_amount = int(sell_usd * 10 ** USDC_DECIMALS)
        params = {
            "sellToken":   USDC_BASE,
            "buyToken":    token_address,
            "sellAmount":  str(sell_amount),
            "taker":       taker_address,
            "chainId":     "8453",
            "slippageBps": "200",
        }
    else:
        # Sell direction: token → USDC
        # Compute approximate token amount using on-chain decimals.
        if fill_price_usd and fill_price_usd > 0:
            tok_dec = get_decimals(token_address, w3)
            token_amount = int(sell_usd / fill_price_usd * 10 ** tok_dec)
        else:
            token_amount = int(sell_usd * 1e12)  # rough fallback for ~$0.001/18-dec tokens
        params = {
            "sellToken":   token_address,
            "buyToken":    USDC_BASE,
            "sellAmount":  str(token_amount),
            "taker":       taker_address,
            "chainId":     "8453",
            "slippageBps": "200",
        }

    t0 = time.monotonic()
    try:
        resp = requests.get(ZEROX_URL, headers=headers, params=params, timeout=ZEROX_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("0x request error: %s", exc)
        return None

    latency_ms = int((time.monotonic() - t0) * 1000)

    if resp.status_code != 200:
        log.warning("0x HTTP %d for %s: %s", resp.status_code, token_address[:12], resp.text[:120])
        return None

    data = resp.json()

    # No liquidity — silent fallthrough to Aerodrome
    if not data.get("liquidityAvailable", True):
        return None

    # Extract price
    price_usd = 0.0
    try:
        price_usd = float(data.get("price", 0))
    except (ValueError, TypeError):
        pass

    # Slippage bps
    slippage_bps = 200
    try:
        p  = float(data.get("price", 0) or 0)
        gp = float(data.get("guaranteedPrice", 0) or 0)
        if p > 0 and gp > 0:
            slippage_bps = round(abs(p - gp) / p * 10000)
    except (ValueError, ZeroDivisionError):
        pass

    # Gas USD
    gas_usd = 0.0
    try:
        fee = (data.get("fees") or {}).get("gasFee") or {}
        if fee.get("amount"):
            gas_usd = int(fee["amount"]) / 1e18 * get_eth_usd()
        elif data.get("estimatedGas") and data.get("gasPrice"):
            gas_usd = int(data["estimatedGas"]) * int(data["gasPrice"]) / 1e18 * get_eth_usd()
    except (ValueError, TypeError):
        pass

    # Route label
    sources = data.get("sources") or []
    route_label = sources[0].get("name", "?") if sources else "?"
    route_summary = f"0x ({route_label})"

    return Quote(
        source       = "0x",
        price_usd    = price_usd,
        slippage_bps = slippage_bps,
        gas_usd      = gas_usd,
        route_summary= route_summary,
        raw_response = data,
        latency_ms   = latency_ms,
    )

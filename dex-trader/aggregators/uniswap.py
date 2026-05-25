"""Uniswap V3 QuoterV2 on-chain quote — second fallback for Base tokens.

QuoterV2: 0x3d4e44Eb1374240CE5F1B136Cf395a8eae0e6953
Fee tiers tried in order: 10000 (1%), 3000 (0.3%), 500 (0.05%).
First tier returning non-zero amountOut is used.

Assumes 18 decimal places for token input/output.
"""
import logging
import time
from typing import Optional

from .types import Quote
from eth_price import get_eth_usd

log = logging.getLogger(__name__)

QUOTER_V2     = "0x3d4e44Eb1374240CE5F1B136Cf395a8eae0e6953"
USDC_BASE     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
FEE_TIERS     = [10000, 3000, 500]

_QUOTER_ABI = [
    {
        "inputs": [
            {
                "name": "params", "type": "tuple",
                "components": [
                    {"name": "tokenIn",           "type": "address"},
                    {"name": "tokenOut",          "type": "address"},
                    {"name": "amountIn",          "type": "uint256"},
                    {"name": "fee",               "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut",               "type": "uint256"},
            {"name": "sqrtPriceX96After",       "type": "uint160"},
            {"name": "initializedTicksCrossed",  "type": "uint32"},
            {"name": "gasEstimate",             "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def quote(token_address: str, sell_usd: float, w3,
          direction: str = "buy", fill_price_usd: float = None) -> Optional[Quote]:
    """
    Tries all fee tiers; returns first Quote with non-zero amountOut.
    Returns None if all fee tiers fail or w3 is None.
    """
    if w3 is None:
        return None

    t0 = time.monotonic()
    try:
        quoter = w3.eth.contract(
            address=w3.to_checksum_address(QUOTER_V2),
            abi=_QUOTER_ABI,
        )
        cs = w3.to_checksum_address

        if direction == "buy":
            token_in  = cs(USDC_BASE)
            token_out = cs(token_address)
            amount_in = int(sell_usd * 10 ** USDC_DECIMALS)
        else:
            token_in  = cs(token_address)
            token_out = cs(USDC_BASE)
            if fill_price_usd and fill_price_usd > 0:
                amount_in = int(sell_usd / fill_price_usd * 1e18)
            else:
                amount_in = int(sell_usd * 1e12)

        for fee in FEE_TIERS:
            try:
                amount_out, _, _, gas_estimate = quoter.functions.quoteExactInputSingle(
                    (token_in, token_out, amount_in, fee, 0)
                ).call()

                if amount_out == 0:
                    continue

                latency_ms = int((time.monotonic() - t0) * 1000)

                if direction == "buy":
                    price_usd = sell_usd / (amount_out / 1e18) if amount_out > 0 else 0.0
                else:
                    price_usd = amount_out / 10 ** USDC_DECIMALS

                gas_usd = 0.0
                try:
                    gas_price = w3.eth.gas_price
                    gas_usd   = gas_estimate * gas_price / 1e18 * get_eth_usd()
                except Exception:
                    pass

                return Quote(
                    source       = "uniswap_v3",
                    price_usd    = price_usd,
                    slippage_bps = 0,
                    gas_usd      = gas_usd,
                    route_summary= f"Uniswap V3 ({fee // 100}bps fee)",
                    latency_ms   = latency_ms,
                )

            except Exception:
                continue   # try next fee tier

        return None   # all fee tiers failed

    except Exception as exc:
        log.warning("uniswap v3 quote failed for %s: %s", token_address[:12], exc)
        return None

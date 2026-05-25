"""Aerodrome Router on-chain quote — first fallback for Base tokens.

Router:  0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
Factory: 0x420DD381b31aEf6683db6B902084cB0FFECe40D
All new meme tokens use volatile pools (stable=False).
Two-hop route (USDC→WETH→token) tried if single-hop returns 0.

Token decimals fetched via get_decimals() (on-chain, cached).
Gas estimate: flat 150,000 gas × current base fee.
"""
import logging
import time
from typing import Optional

from .types import Quote
from eth_price import get_eth_usd
from token_decimals import get_decimals

log = logging.getLogger(__name__)

AERODROME_ROUTER  = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40D"
WETH_BASE      = "0x4200000000000000000000000000000000000006"
USDC_BASE      = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS  = 6
GAS_ESTIMATE   = 150_000   # flat estimate for a new token swap

_ROUTER_ABI = [
    {
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {
                "name": "routes", "type": "tuple[]",
                "components": [
                    {"name": "from",    "type": "address"},
                    {"name": "to",      "type": "address"},
                    {"name": "stable",  "type": "bool"},
                    {"name": "factory", "type": "address"},
                ],
            },
        ],
        "name": "getAmountsOut",
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def quote(token_address: str, sell_usd: float, w3,
          direction: str = "buy", fill_price_usd: float = None,
          signal_price_usd: float = None) -> Optional[Quote]:
    """
    Returns Quote or None (on revert / empty amounts / w3=None).
    direction='buy':  USDC → token
    direction='sell': token → USDC; fill_price_usd used to estimate token amount.
    signal_price_usd: DexScreener price at signal time — used to compute slippage_bps.
    price_usd in the returned Quote is always per-token (not total USDC).
    """
    if w3 is None:
        return None

    t0 = time.monotonic()
    try:
        router = w3.eth.contract(
            address=w3.to_checksum_address(AERODROME_ROUTER),
            abi=_ROUTER_ABI,
        )
        cs = w3.to_checksum_address

        tok_dec = get_decimals(token_address, w3)

        if direction == "buy":
            amount_in = int(sell_usd * 10 ** USDC_DECIMALS)
            route_label, amounts = _try_buy_routes(router, cs, token_address, amount_in)
            if amounts is None or amounts[-1] == 0:
                return None
            token_out_float = amounts[-1] / 10 ** tok_dec
            price_usd = sell_usd / token_out_float if token_out_float > 0 else 0.0

        else:  # sell — token → USDC
            if fill_price_usd and fill_price_usd > 0:
                tokens_float = sell_usd / fill_price_usd         # tokens to sell (float)
                token_amount = int(tokens_float * 10 ** tok_dec)
            else:
                token_amount = int(sell_usd * 1e12)              # rough fallback
                tokens_float = token_amount / 10 ** tok_dec      # back-convert for price calc

            single = [{
                "from":    cs(token_address),
                "to":      cs(USDC_BASE),
                "stable":  False,
                "factory": cs(AERODROME_FACTORY),
            }]
            amounts = _try_route(router, token_amount, single)
            if amounts is None or amounts[-1] == 0:
                return None
            usdc_out    = amounts[-1] / 10 ** USDC_DECIMALS
            price_usd   = usdc_out / tokens_float if tokens_float > 0 else 0.0  # per-token
            route_label = "Aerodrome (volatile, sell)"

        latency_ms = int((time.monotonic() - t0) * 1000)

        # Slippage bps vs signal price (entry) or fill price (exit, via signal_price_usd arg)
        slippage_bps = 0
        if signal_price_usd and signal_price_usd > 0 and price_usd > 0:
            slippage_bps = round(abs(signal_price_usd - price_usd) / signal_price_usd * 10000)

        gas_usd = 0.0
        try:
            gas_price = w3.eth.gas_price
            gas_usd   = GAS_ESTIMATE * gas_price / 1e18 * get_eth_usd()
        except Exception:
            pass

        return Quote(
            source       = "aerodrome",
            price_usd    = price_usd,
            slippage_bps = slippage_bps,
            gas_usd      = gas_usd,
            route_summary= route_label,
            latency_ms   = latency_ms,
        )

    except Exception as exc:
        log.warning("aerodrome quote failed for %s: %s", token_address[:12], exc)
        return None


def _try_buy_routes(router, cs, token_address: str, amount_in: int):
    """Try single-hop then two-hop. Returns (label, amounts) or (None, None)."""
    single = [{
        "from":    cs(USDC_BASE),
        "to":      cs(token_address),
        "stable":  False,
        "factory": cs(AERODROME_FACTORY),
    }]
    amounts = _try_route(router, amount_in, single)
    if amounts is not None and amounts[-1] > 0:
        return "Aerodrome (volatile)", amounts

    two_hop = [
        {
            "from":    cs(USDC_BASE),
            "to":      cs(WETH_BASE),
            "stable":  False,
            "factory": cs(AERODROME_FACTORY),
        },
        {
            "from":    cs(WETH_BASE),
            "to":      cs(token_address),
            "stable":  False,
            "factory": cs(AERODROME_FACTORY),
        },
    ]
    amounts = _try_route(router, amount_in, two_hop)
    return ("Aerodrome (volatile, 2-hop)", amounts)


def _try_route(router, amount_in: int, route: list) -> Optional[list]:
    try:
        return router.functions.getAmountsOut(amount_in, route).call()
    except Exception:
        return None

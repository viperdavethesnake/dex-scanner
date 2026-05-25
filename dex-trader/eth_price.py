"""ETH/USD price feed — Coinbase public API, cached, $3000 fallback.

Used by aggregator modules to convert gas (ETH) to USD.
No API key required. Cache TTL: ETH_USD_CACHE_SECONDS (default 600).
"""
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

_ETH_USD_URL     = os.environ.get("ETH_USD_PRICE_URL",
                                   "https://api.coinbase.com/v2/prices/ETH-USD/spot")
_CACHE_SECONDS   = int(os.environ.get("ETH_USD_CACHE_SECONDS", "600"))
_FALLBACK        = 3000.0

_cached_price: float = _FALLBACK
_cached_at: float    = 0.0


def get_eth_usd() -> float:
    """Return current ETH/USD price. Cached for _CACHE_SECONDS. Never raises."""
    global _cached_price, _cached_at
    now = time.monotonic()
    if now - _cached_at < _CACHE_SECONDS:
        return _cached_price
    try:
        resp = requests.get(_ETH_USD_URL, timeout=5)
        resp.raise_for_status()
        price = float(resp.json()["data"]["amount"])
        _cached_price = price
        _cached_at    = now
        log.debug("eth/usd refreshed: $%.2f", price)
        return price
    except Exception as exc:
        log.warning("eth/usd fetch failed (%s) — using $%.0f fallback", exc, _cached_price)
        _cached_at = now   # back off until next interval even on failure
        return _cached_price

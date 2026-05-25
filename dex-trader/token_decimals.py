"""Token ERC20 decimals cache.

On-chain lookup via decimals() view function with two-level caching:
  1. In-memory dict (_cache) — zero-latency for repeat lookups
  2. Persistent JSON file (/trader_data/token_decimals.json) — survives restarts

Call load_cache() once at startup.
On RPC failure: returns default (18), does NOT cache — retry fires next time.
"""
import json
import logging
import math
import os

log = logging.getLogger(__name__)

_CACHE_FILE = os.environ.get("TOKEN_DECIMALS_CACHE",
                              "/trader_data/token_decimals.json")

_ERC20_DECIMALS_ABI = [
    {
        "inputs":  [],
        "name":    "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type":    "function",
    }
]

_cache: dict[str, int] = {}


def load_cache() -> None:
    """Load persisted decimals from disk at startup. Safe to call even if file absent."""
    global _cache
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE) as f:
                _cache = {k.lower(): int(v) for k, v in json.load(f).items()}
            log.info("token_decimals: loaded %d entries from %s", len(_cache), _CACHE_FILE)
        else:
            log.info("token_decimals: no cache file at %s — starting empty", _CACHE_FILE)
    except Exception as exc:
        log.warning("token_decimals: failed to load %s: %s — starting empty", _CACHE_FILE, exc)
        _cache = {}


def _persist() -> None:
    """Write cache to disk. Never raises."""
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE) or ".", exist_ok=True)
        with open(_CACHE_FILE, "w") as f:
            json.dump(_cache, f)
    except Exception as exc:
        log.warning("token_decimals: persist failed: %s", exc)


def get_decimals(token_address: str, w3, default: int = 18) -> int:
    """
    Return ERC20 decimals for token_address.

    Cache hit  → return cached value (no RPC).
    Cache miss → call decimals() on-chain, cache result, persist to disk.
    RPC error  → log warning, return default, do NOT cache (retry next call).
    w3=None    → return default immediately (on-chain fallback unavailable).
    """
    key = token_address.lower()
    if key in _cache:
        return _cache[key]

    if w3 is None:
        return default

    try:
        contract = w3.eth.contract(
            address=w3.to_checksum_address(token_address),
            abi=_ERC20_DECIMALS_ABI,
        )
        decimals = int(contract.functions.decimals().call())
        _cache[key] = decimals
        _persist()
        log.debug("token_decimals: %s → %d (fetched + cached)", token_address[:12], decimals)
        return decimals
    except Exception as exc:
        log.warning("token_decimals: RPC failed for %s: %s — returning default %d (not cached)",
                    token_address[:12], exc, default)
        return default

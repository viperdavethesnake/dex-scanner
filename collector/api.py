import time
import logging
import requests

log = logging.getLogger(__name__)

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS_URL    = "https://api.dexscreener.com/latest/dex/tokens/{address}"
PRICE_URL    = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"

SUPPORTED_CHAINS = {"base", "solana"}
MIN_LIQUIDITY    = 1_000
REQUEST_TIMEOUT  = 10


def _get(url, retries=3, backoff=2.0):
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                wait = backoff * (2 ** attempt)
                log.warning("rate limited, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                log.error("GET %s failed: %s", url, e)
                return None
            time.sleep(backoff)
    return None


def fetch_profiles():
    """Return list of {chainId, tokenAddress} for base/solana tokens."""
    data = _get(PROFILES_URL)
    if not data:
        return []
    profiles = data if isinstance(data, list) else data.get("profiles", [])
    return [p for p in profiles if p.get("chainId") in SUPPORTED_CHAINS]


def fetch_pair(token_address, chain_id):
    """
    Fetch pair data for a token address. Returns the best pair dict or None.
    Best = highest liquidity, correct chain, within age/liquidity thresholds.
    """
    data = _get(PAIRS_URL.format(address=token_address))
    if not data:
        return None

    pairs = data.get("pairs") or []
    now_ms = time.time() * 1000

    candidates = []
    for p in pairs:
        if p.get("chainId") != chain_id:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq < MIN_LIQUIDITY:
            continue
        candidates.append((liq, p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def fetch_current_price(chain, pair_address):
    """Return current priceUsd for a pair, or None."""
    chain_id = "base" if chain == "base" else "solana"
    data = _get(PRICE_URL.format(chain=chain_id, pair_address=pair_address))
    if not data:
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    price = pairs[0].get("priceUsd")
    return float(price) if price else None

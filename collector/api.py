import time
import logging
import requests

log = logging.getLogger(__name__)

PROFILES_URL          = "https://api.dexscreener.com/token-profiles/latest/v1"
PAIRS_URL             = "https://api.dexscreener.com/latest/dex/tokens/{address}"
PRICE_URL             = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
BIRDEYE_OVERVIEW_URL  = "https://public-api.birdeye.so/defi/token_overview"

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


def fetch_birdeye_overview(address: str, api_key: str, timeout: int = 5) -> dict:
    """
    Call Birdeye /defi/token_overview for a Base token.

    Returns a result dict with all fields needed for log_birdeye_call() and
    Token enrichment. Never raises — all errors are captured in error_message.

    Keys:
        address, http_status, cu_consumed, response_ms,
        unique_traders_1h, net_inflow_usd, error_message
    """
    result = {
        "address":           address,
        "http_status":       None,
        "cu_consumed":       None,
        "response_ms":       None,
        "unique_traders_1h": None,
        "net_inflow_usd":    None,
        "error_message":     None,
    }
    t0 = time.monotonic()
    try:
        r = requests.get(
            BIRDEYE_OVERVIEW_URL,
            params={"address": address},
            headers={"X-API-KEY": api_key, "x-chain": "base"},
            timeout=timeout,
        )
        result["response_ms"] = int((time.monotonic() - t0) * 1000)
        result["http_status"] = r.status_code

        # CU header — not always present on Standard tier
        cu_str = r.headers.get("x-ratelimit-remaining-cu", "")
        if cu_str.isdigit():
            result["cu_consumed"] = int(cu_str)

        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                result["error_message"] = "json_parse_error"
                return result

            if not body.get("success"):
                msg = str(body.get("message", ""))[:120]
                result["error_message"] = f"success=false: {msg}"
                return result

            data = body.get("data") or {}
            raw_traders = data.get("uniqueWallet1h")
            raw_buy     = data.get("vBuy1hUSD")
            raw_sell    = data.get("vSell1hUSD")

            if raw_traders is not None:
                try:
                    result["unique_traders_1h"] = int(raw_traders)
                except (TypeError, ValueError):
                    pass

            if raw_buy is not None and raw_sell is not None:
                try:
                    result["net_inflow_usd"] = round(float(raw_buy) - float(raw_sell), 2)
                except (TypeError, ValueError):
                    pass

        else:
            try:
                body = r.json()
                result["error_message"] = str(body.get("message", r.text))[:120]
            except Exception:
                result["error_message"] = r.text[:120]

    except requests.Timeout:
        result["response_ms"] = int((time.monotonic() - t0) * 1000)
        result["error_message"] = "timeout"
    except Exception as e:
        result["response_ms"] = int((time.monotonic() - t0) * 1000)
        result["error_message"] = str(e)[:120]

    return result

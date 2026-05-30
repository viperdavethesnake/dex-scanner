"""Security checks — GoPlus + Honeypot.is with 1h in-memory cache.

Fail behavior controlled by SECURITY_FAIL_OPEN env var (default true in shadow mode).
When true: API errors allow the trade through with a warning.
When false (live mode): API errors block the trade.
"""
import logging
import os
import time
from typing import Dict, Tuple

import requests

log = logging.getLogger(__name__)

SECURITY_CACHE_TTL = 3600   # seconds
SECURITY_TIMEOUT   = 2      # seconds per API call
SECURITY_FAIL_OPEN = os.environ.get("SECURITY_FAIL_OPEN", "true").lower() == "true"

GOPLUS_URL   = "https://api.gopluslabs.io/api/v1/token_security/8453"
HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"

# (checked_at_monotonic, passed, flags_csv)
_cache: Dict[str, Tuple[float, bool, str]] = {}


def is_safe(address: str, chain: str) -> Tuple[bool, str]:
    """
    Returns (safe, source_tag). Never raises.
    source_tag: 'cache' | 'goplus+honeypot' | 'error_fail_open'
    """
    now = time.monotonic()
    if address in _cache:
        checked_at, passed, flags = _cache[address]
        if now - checked_at < SECURITY_CACHE_TTL:
            return passed, "cache"

    gp_safe, gp_flags = True, ""
    try:
        gp_safe, gp_flags = _check_goplus(address)
    except Exception as exc:
        log.warning("goplus check failed for %s: %s", address[:12], exc)
        gp_safe  = SECURITY_FAIL_OPEN
        gp_flags = "goplus_error"

    hp_safe, hp_flags = True, ""
    if chain == "base":
        try:
            hp_safe, hp_flags = _check_honeypot(address)
        except Exception as exc:
            log.warning("honeypot check failed for %s: %s", address[:12], exc)
            hp_safe  = SECURITY_FAIL_OPEN
            hp_flags = "honeypot_error"

    passed = gp_safe and hp_safe
    flags  = ",".join(f for f in [gp_flags, hp_flags] if f)
    _cache[address] = (now, passed, flags)

    source = "error_fail_open" if ("error" in flags) else "goplus+honeypot"
    return passed, source


def _check_goplus(address: str) -> Tuple[bool, str]:
    resp = requests.get(
        GOPLUS_URL,
        params={"contract_addresses": address},
        timeout=SECURITY_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json().get("result", {}).get(address.lower(), {})
    if not data:
        return True, ""   # not in GoPlus DB — unknown, not unsafe

    flags = []
    if data.get("is_honeypot") == "1":
        flags.append("honeypot")
    if float(data.get("buy_tax") or 0) > 10:
        flags.append(f"buy_tax:{data.get('buy_tax')}")
    if float(data.get("sell_tax") or 0) > 10:
        flags.append(f"sell_tax:{data.get('sell_tax')}")
    if data.get("is_blacklisted") == "1":
        flags.append("blacklisted")
    if data.get("cannot_sell_all") == "1":
        flags.append("cannot_sell_all")

    return len(flags) == 0, ",".join(flags)


def _check_honeypot(address: str) -> Tuple[bool, str]:
    resp = requests.get(
        HONEYPOT_URL,
        params={"address": address},
        timeout=SECURITY_TIMEOUT,
    )
    if resp.status_code == 404:
        return True, ""   # token not yet indexed — not a known honeypot
    resp.raise_for_status()
    if resp.json().get("IsHoneypot"):
        return False, "honeypot"
    return True, ""

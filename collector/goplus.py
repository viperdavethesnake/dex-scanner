"""GoPlus security enrichment for the collector."""
import time
import requests

log = __import__("logging").getLogger(__name__)

GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

_CHAIN_MAP = {
    "base":   "8453",
    "solana": "solana",
}


def fetch_goplus_security(address: str, chain: str, api_key: str = "",
                          timeout: int = 8) -> dict:
    """
    Call GoPlus token_security for the given address and chain.

    Returns a structured result dict. Never raises. If GoPlus doesn't have the
    token in its DB, returns found_in_db=False with all data fields None —
    distinct from an API error.

    Keys: address, chain, http_status, response_ms, found_in_db, error_message,
          top1_pct, top5_pct, top10_pct, holder_count, creator_pct,
          creator_balance, lp_holder_count, lp_locked_pct, buy_tax, sell_tax,
          is_honeypot, is_blacklisted, is_mintable, hidden_owner,
          can_take_back_ownership, owner_change_balance,
          honeypot_with_same_creator, is_proxy, is_open_source,
          transfer_pausable, trading_cooldown, anti_whale_modifiable,
          slippage_modifiable
    """
    chain_id = _CHAIN_MAP.get(chain)
    result = {
        "address":                    address,
        "chain":                      chain,
        "http_status":                None,
        "response_ms":                None,
        "found_in_db":                False,
        "error_message":              None,
        "top1_pct":                   None,
        "top5_pct":                   None,
        "top10_pct":                  None,
        "holder_count":               None,
        "creator_pct":                None,
        "creator_balance":            None,
        "lp_holder_count":            None,
        "lp_locked_pct":              None,
        "buy_tax":                    None,
        "sell_tax":                   None,
        "is_honeypot":                None,
        "is_blacklisted":             None,
        "is_mintable":                None,
        "hidden_owner":               None,
        "can_take_back_ownership":    None,
        "owner_change_balance":       None,
        "honeypot_with_same_creator": None,
        "is_proxy":                   None,
        "is_open_source":             None,
        "transfer_pausable":          None,
        "trading_cooldown":           None,
        "anti_whale_modifiable":      None,
        "slippage_modifiable":        None,
    }

    if chain_id is None:
        result["error_message"] = f"unsupported chain: {chain}"
        return result

    headers = {"X-API-KEY": api_key} if api_key else {}
    url = GOPLUS_URL.format(chain_id=chain_id)
    t0 = time.monotonic()
    try:
        r = requests.get(
            url,
            params={"contract_addresses": address},
            headers=headers,
            timeout=timeout,
        )
        result["response_ms"] = int((time.monotonic() - t0) * 1000)
        result["http_status"] = r.status_code

        if r.status_code != 200:
            try:
                body = r.json()
                result["error_message"] = str(body.get("message", r.text))[:120]
            except Exception:
                result["error_message"] = r.text[:120]
            return result

        try:
            body = r.json()
        except Exception:
            result["error_message"] = "json_parse_error"
            return result

        gp_result = body.get("result") or {}
        keys = list(gp_result.keys())
        if not keys:
            result["found_in_db"] = False
            return result

        gp = gp_result[keys[0]]
        if not gp:
            result["found_in_db"] = False
            return result

        result["found_in_db"] = True

        def _f(key):
            v = gp.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _i(key):
            v = gp.get(key)
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        def _b(key):
            v = gp.get(key)
            if v is None or v == "":
                return None
            try:
                return 1 if str(v) == "1" else 0
            except Exception:
                return None

        result["holder_count"]     = _i("holder_count")
        result["creator_pct"]      = _f("creator_percent")
        result["creator_balance"]  = _f("creator_balance")
        result["lp_holder_count"]  = _i("lp_holder_count")
        result["buy_tax"]          = _f("buy_tax")
        result["sell_tax"]         = _f("sell_tax")

        result["is_honeypot"]                = _b("is_honeypot")
        result["is_blacklisted"]             = _b("is_blacklisted")
        result["is_mintable"]                = _b("is_mintable")
        result["hidden_owner"]               = _b("hidden_owner")
        result["can_take_back_ownership"]    = _b("can_take_back_ownership")
        result["owner_change_balance"]       = _b("owner_change_balance")
        result["honeypot_with_same_creator"] = _b("honeypot_with_same_creator")
        result["is_proxy"]                   = _b("is_proxy")
        result["is_open_source"]             = _b("is_open_source")
        result["transfer_pausable"]          = _b("transfer_pausable")
        result["trading_cooldown"]           = _b("trading_cooldown")
        result["anti_whale_modifiable"]      = _b("anti_whale_modifiable")
        result["slippage_modifiable"]        = _b("slippage_modifiable")

        holders = gp.get("holders") or []
        if holders:
            try:
                pcts = [float(h.get("percent", 0)) * 100 for h in holders]
                if len(pcts) >= 1:
                    result["top1_pct"]  = round(pcts[0], 2)
                if len(pcts) >= 5:
                    result["top5_pct"]  = round(sum(pcts[:5]), 2)
                result["top10_pct"] = round(sum(pcts[:10]), 2)
            except (TypeError, ValueError):
                pass

        lp_holders = gp.get("lp_holders") or []
        if lp_holders:
            try:
                locked_pct = sum(
                    float(h.get("percent", 0)) * 100
                    for h in lp_holders
                    if str(h.get("is_locked", "0")) == "1"
                )
                result["lp_locked_pct"] = round(locked_pct, 2)
            except (TypeError, ValueError):
                pass

    except requests.Timeout:
        result["response_ms"] = int((time.monotonic() - t0) * 1000)
        result["error_message"] = "timeout"
    except Exception as e:
        result["response_ms"] = int((time.monotonic() - t0) * 1000)
        result["error_message"] = str(e)[:120]

    return result

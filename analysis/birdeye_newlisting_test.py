#!/usr/bin/env python3
"""
Test Birdeye new-listing and token-list endpoints for Base and Solana.
Estimates tokens surfaced per hour via each endpoint.
"""
import json
import time
import requests
from datetime import datetime, timezone

import os
BIRDEYE_KEY = os.environ.get("BIRDEYE_API_KEY", "")
if not BIRDEYE_KEY:
    raise SystemExit("BIRDEYE_API_KEY env var not set")

ENDPOINTS_TO_TEST = [
    # (name, url, chain, params)
    # Note: new_listing limit is max 20 per Birdeye docs
    ("new_listing_base",     "https://public-api.birdeye.so/defi/v2/tokens/new_listing",   "base",   {"limit": 20}),
    ("new_listing_solana",   "https://public-api.birdeye.so/defi/v2/tokens/new_listing",   "solana", {"limit": 20}),
    # Token list sorted by listing time
    ("token_list_base",      "https://public-api.birdeye.so/defi/v2/tokens/list",          "base",   {"sort_by": "created_at", "sort_type": "desc", "limit": 50}),
    ("token_list_solana",    "https://public-api.birdeye.so/defi/v2/tokens/list",          "solana", {"sort_by": "created_at", "sort_type": "desc", "limit": 50}),
]

SAMPLE_ROUNDS = 5    # call each endpoint 5 times, 60s apart
ROUND_SLEEP   = 60   # seconds between rounds
INTER_CALL_SLEEP = 3  # seconds between calls within a round (avoid 429)


def call(name, url, chain, params):
    headers = {"X-API-KEY": BIRDEYE_KEY, "x-chain": chain}
    t0 = time.monotonic()
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        ms = int((time.monotonic() - t0) * 1000)
        status = r.status_code
        if status != 200:
            return {"status": status, "error": r.text[:120], "ms": ms, "items": []}
        body = r.json()
        data = body.get("data") or {}
        if isinstance(data, dict):
            items = data.get("items") or data.get("tokens") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        # Show oldest and newest item timestamps
        timestamps = []
        for item in items:
            ts = item.get("lastTradeUnixTime") or item.get("createdAt") or item.get("created_at")
            if ts:
                timestamps.append(int(ts))
        return {
            "status": status,
            "count": len(items),
            "ms": ms,
            "oldest_ts": min(timestamps) if timestamps else None,
            "newest_ts": max(timestamps) if timestamps else None,
            "top_keys": list((items[0] if items else {}).keys())[:10],
            "items": [i.get("address") or i.get("tokenAddress", "") for i in items],
        }
    except Exception as e:
        return {"status": -1, "error": str(e)[:80], "ms": int((time.monotonic() - t0) * 1000), "items": []}


def ts_to_str(ts):
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC")


def main():
    print(f"=== Birdeye New-Listing Endpoint Test ===", flush=True)
    print(f"Start: {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"Rounds: {SAMPLE_ROUNDS} | Interval: {ROUND_SLEEP}s", flush=True)
    print(f"", flush=True)

    seen = {name: set() for name, _, _, _ in ENDPOINTS_TO_TEST}
    round_results = []

    for rnd in range(1, SAMPLE_ROUNDS + 1):
        print(f"--- Round {rnd}/{SAMPLE_ROUNDS} @ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---", flush=True)
        rnd_data = {}

        for i, (name, url, chain, params) in enumerate(ENDPOINTS_TO_TEST):
            if i > 0:
                time.sleep(INTER_CALL_SLEEP)
            result = call(name, url, chain, params)
            rnd_data[name] = result

            new_addrs = [a for a in result.get("items", []) if a and a not in seen[name]]
            for a in result.get("items", []):
                if a:
                    seen[name].add(a)

            if result["status"] == 200:
                print(f"  {name:<30} count={result.get('count', 0):4d}  new={len(new_addrs):4d}  cum={len(seen[name]):5d}  "
                      f"newest={ts_to_str(result.get('newest_ts'))}  oldest={ts_to_str(result.get('oldest_ts'))}  "
                      f"ms={result['ms']:4d}", flush=True)
                if rnd == 1 and result.get("top_keys"):
                    print(f"    keys: {result['top_keys']}", flush=True)
            elif result["status"] == 404:
                print(f"  {name:<30} 404 NOT FOUND", flush=True)
            else:
                print(f"  {name:<30} HTTP{result['status']}  {result.get('error', '')}", flush=True)

        round_results.append({"round": rnd, "ts": datetime.now(timezone.utc).isoformat(), "data": rnd_data})
        print(f"", flush=True)

        if rnd < SAMPLE_ROUNDS:
            time.sleep(ROUND_SLEEP)

    # Summary
    print(f"=== SUMMARY ===", flush=True)
    print(f"{'Endpoint':<30} {'Chain':>6} {'Status':>6} {'Unique':>7} {'Est/hr':>8}", flush=True)
    print("-" * 62, flush=True)

    elapsed_min = SAMPLE_ROUNDS * ROUND_SLEEP / 60
    summary = {}
    for name, url, chain, _ in ENDPOINTS_TO_TEST:
        total_unique = len(seen[name])
        rate_per_hr = round(total_unique / elapsed_min * 60, 0) if elapsed_min > 0 else 0
        # Last known status
        last_status = round_results[-1]["data"].get(name, {}).get("status", "?") if round_results else "?"
        print(f"  {name:<30} {chain:>6} {str(last_status):>6} {total_unique:>7} {rate_per_hr:>8.0f}", flush=True)
        summary[name] = {"chain": chain, "unique_in_window": total_unique, "est_per_hour": rate_per_hr}

    print(f"\n(window = {elapsed_min:.1f} min, unique addrs surfaced in that window extrapolated to hourly)", flush=True)

    # Save
    out = {
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "rounds": SAMPLE_ROUNDS,
        "round_interval_s": ROUND_SLEEP,
        "summary": summary,
        "rounds_data": round_results,
    }
    out_path = "/space/docker/containers/dex-scanner/analysis/birdeye_newlisting_results.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()

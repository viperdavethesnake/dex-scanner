#!/usr/bin/env python3
"""
Endpoint sampler for intake-gap-diagnostic.
Samples DexScreener endpoints once per minute for SAMPLE_DURATION minutes.
All endpoints sampled in the same minute window.
Writes cumulative stats to stdout and a JSON results file.
"""
import json
import time
import sys
from datetime import datetime, timezone
from collections import defaultdict

import requests

ENDPOINTS = {
    "token_profiles":         "https://api.dexscreener.com/token-profiles/latest/v1",
    "token_profiles_updates": "https://api.dexscreener.com/token-profiles/recent-updates/v1",
    "token_boosts_latest":    "https://api.dexscreener.com/token-boosts/latest/v1",
    "token_boosts_top":       "https://api.dexscreener.com/token-boosts/top/v1",
    "search_base":            "https://api.dexscreener.com/latest/dex/search?q=base",
    "search_solana":          "https://api.dexscreener.com/latest/dex/search?q=solana",
    "pairs_base":             "https://api.dexscreener.com/latest/dex/pairs/base",
    "pairs_solana":           "https://api.dexscreener.com/latest/dex/pairs/solana",
    "metas_trending":         "https://api.dexscreener.com/metas/trending/v1",
    "community_takeovers":    "https://api.dexscreener.com/community-takeovers/latest/v1",
}

SUPPORTED_CHAINS = {"base", "solana"}
SAMPLE_DURATION = 60  # minutes
OUT_JSON = "/space/docker/containers/dex-scanner/analysis/endpoint_sample_results.json"


def extract_tokens(key, data):
    """Return list of (chain, addr) tuples from endpoint response."""
    tokens = []
    if data is None:
        return tokens
    if key in ("token_profiles", "token_profiles_updates"):
        items = data if isinstance(data, list) else (data.get("profiles") or [])
        for i in items:
            chain = i.get("chainId", "")
            addr = i.get("tokenAddress", "")
            if chain in SUPPORTED_CHAINS and addr:
                tokens.append((chain, addr))
    elif key in ("token_boosts_latest", "token_boosts_top", "community_takeovers"):
        items = data if isinstance(data, list) else (data or [])
        for i in items:
            chain = i.get("chainId", "")
            addr = i.get("tokenAddress", "")
            if chain in SUPPORTED_CHAINS and addr:
                tokens.append((chain, addr))
    elif key in ("search_base", "search_solana", "pairs_base", "pairs_solana"):
        pairs = (data or {}).get("pairs") or []
        for p in pairs:
            chain = p.get("chainId", "")
            base = (p.get("baseToken") or {}).get("address", "")
            if chain in SUPPORTED_CHAINS and base:
                tokens.append((chain, base))
    elif key == "metas_trending":
        # Response is a top-level list of meta objects, each with optional pairs
        items = data if isinstance(data, list) else (((data or {}).get("data")) or [])
        for meta in items:
            for p in (meta.get("pairs") or []):
                chain = p.get("chainId", "")
                base = (p.get("baseToken") or {}).get("address", "")
                if chain in SUPPORTED_CHAINS and base:
                    tokens.append((chain, base))
    return tokens


def call_endpoint(key, url):
    try:
        r = requests.get(url, timeout=15)
        status = r.status_code
        data = r.json() if status == 200 else None
        return status, data, None
    except requests.RequestException as e:
        return -1, None, str(e)[:80]


def main():
    seen = {key: set() for key in ENDPOINTS}
    seen_by_chain = {key: defaultdict(set) for key in ENDPOINTS}
    http_statuses = {key: [] for key in ENDPOINTS}
    raw_counts = {key: [] for key in ENDPOINTS}

    results_by_minute = []
    start_time = time.time()

    print(f"=== DexScreener Endpoint Sampler ===", flush=True)
    print(f"Start: {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"Duration: {SAMPLE_DURATION} min | Endpoints: {len(ENDPOINTS)}", flush=True)
    print(f"", flush=True)

    # Print header for minute tables
    print(f"{'Min':>4}  {'Endpoint':<35} {'HTTP':>4} {'Ret':>5} {'New':>5} {'Cum':>6} {'Base':>5} {'Sol':>5}", flush=True)
    print("-" * 80, flush=True)

    for minute in range(1, SAMPLE_DURATION + 1):
        tick_start = time.time()
        ts = datetime.now(timezone.utc).isoformat()
        minute_entry = {"minute": minute, "ts": ts, "endpoints": {}}

        for key, url in ENDPOINTS.items():
            status, data, err = call_endpoint(key, url)
            tokens = extract_tokens(key, data)

            http_statuses[key].append(status)
            raw_counts[key].append(len(tokens))

            new_this = 0
            chain_new = defaultdict(int)
            for chain, addr in tokens:
                if addr not in seen[key]:
                    seen[key].add(addr)
                    seen_by_chain[key][chain].add(addr)
                    new_this += 1
                    chain_new[chain] += 1

            cum = len(seen[key])
            base_n = len(seen_by_chain[key]["base"])
            sol_n = len(seen_by_chain[key]["solana"])

            entry = {
                "http": status,
                "returned": len(tokens),
                "new": new_this,
                "cum": cum,
                "base": base_n,
                "sol": sol_n,
            }
            if err:
                entry["err"] = err
            minute_entry["endpoints"][key] = entry

            if status == 200:
                print(f"[{minute:02d}/{SAMPLE_DURATION}]  {key:<35} {status:4d} {len(tokens):5d} {new_this:5d} {cum:6d} {base_n:5d} {sol_n:5d}", flush=True)
            elif status == 404:
                print(f"[{minute:02d}/{SAMPLE_DURATION}]  {key:<35}  404  NOT FOUND", flush=True)
            else:
                print(f"[{minute:02d}/{SAMPLE_DURATION}]  {key:<35} {status:4d}  {err or ''}", flush=True)

        print(f"", flush=True)
        results_by_minute.append(minute_entry)

        # Sleep to next minute mark
        elapsed = time.time() - tick_start
        sleep_for = max(0, 60 - elapsed)
        if minute < SAMPLE_DURATION:
            time.sleep(sleep_for)

    # --- Final summary ---
    total_elapsed = time.time() - start_time
    print(f"\n=== FINAL SUMMARY ({total_elapsed/60:.1f} min) ===", flush=True)
    print(f"\n{'Endpoint':<35} {'Status':>6} {'Uniq':>6} {'Base':>6} {'Sol':>6} {'Avg/call':>9}", flush=True)
    print("-" * 75, flush=True)

    summary = {}
    for key in ENDPOINTS:
        statuses = http_statuses[key]
        ok = [s for s in statuses if s == 200]
        most_common = max(set(statuses), key=statuses.count) if statuses else "?"
        avg_ret = sum(raw_counts[key]) / len(raw_counts[key]) if raw_counts[key] else 0
        uniq = len(seen[key])
        base_u = len(seen_by_chain[key]["base"])
        sol_u = len(seen_by_chain[key]["solana"])
        success_rate = len(ok) / len(statuses) if statuses else 0

        print(f"  {key:<35} {most_common:>6} {uniq:>6} {base_u:>6} {sol_u:>6} {avg_ret:>9.1f}", flush=True)

        summary[key] = {
            "most_common_status": most_common,
            "success_rate_pct": round(success_rate * 100, 1),
            "unique_total": uniq,
            "unique_base": base_u,
            "unique_solana": sol_u,
            "avg_per_call": round(avg_ret, 1),
            "hourly_rate": round(uniq / (total_elapsed / 3600), 0),
        }

    # Per-endpoint 30-min vs 60-min breakdown
    print(f"\n--- 30-min vs 60-min unique token accumulation ---", flush=True)
    print(f"{'Endpoint':<35} {'@30min':>8} {'@60min':>8} {'Growth':>8}", flush=True)
    print("-" * 65, flush=True)

    for key in ENDPOINTS:
        seen_30 = set()
        seen_60 = set()
        for minute_entry in results_by_minute:
            ep = minute_entry["endpoints"].get(key, {})
            # Rebuild cumulative at 30 and 60 from per-minute data
        # Use the cum field at minute 30 and 60
        cum_30 = results_by_minute[29]["endpoints"].get(key, {}).get("cum", 0) if len(results_by_minute) >= 30 else 0
        cum_60 = results_by_minute[-1]["endpoints"].get(key, {}).get("cum", 0) if results_by_minute else 0
        growth = cum_60 - cum_30
        print(f"  {key:<35} {cum_30:>8} {cum_60:>8} {growth:>+8}", flush=True)

    # Save JSON
    output = {
        "started_at":       results_by_minute[0]["ts"] if results_by_minute else None,
        "ended_at":         datetime.now(timezone.utc).isoformat(),
        "duration_min":     SAMPLE_DURATION,
        "elapsed_min":      round(total_elapsed / 60, 1),
        "summary":          summary,
        "by_minute":        results_by_minute,
    }
    with open(OUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved: {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()

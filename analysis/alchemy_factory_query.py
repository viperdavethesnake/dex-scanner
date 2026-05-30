#!/usr/bin/env python3
"""
Query Alchemy for new pair/pool creation events on Base in the last 1 hour.
Reports per-factory event counts to establish ground-truth launch volume.
"""
import json
import requests
from datetime import datetime, timezone

import os as _os
_alchemy_base = _os.environ.get("ALCHEMY_BASE_URL", "")
if not _alchemy_base:
    raise SystemExit("ALCHEMY_BASE_URL env var not set")
ALCHEMY_URL = _alchemy_base

# Base mainnet factory addresses (user-specified)
FACTORIES = {
    "uniswap_v2": {
        "address": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        "event":   "PairCreated",
        # keccak256("PairCreated(address,address,address,uint256)")
        "topic0":  "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9",
    },
    "uniswap_v3": {
        "address": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "event":   "PoolCreated",
        # keccak256("PoolCreated(address,address,uint24,int24,address)")
        "topic0":  "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118",
    },
    "aerodrome": {
        "address": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
        "event":   "PoolCreated",
        # keccak256("PoolCreated(address,address,bool,address,uint256)") — Aerodrome V2 style
        "topic0":  "0x2a53ef25d0b959e4b4a54b4e73e0af40e50c5f27c7e59be4c2b99d8ba6fdedf8",
    },
}

# Additional high-volume factories on Base worth checking
EXTRA_FACTORIES = {
    "pancakeswap_v3": {
        "address": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
        "event":   "PoolCreated",
        "topic0":  "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118",
    },
    "baseswap_v2": {
        "address": "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB",
        "event":   "PairCreated",
        "topic0":  "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9",
    },
}


def rpc(method, params):
    r = requests.post(ALCHEMY_URL, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=30)
    r.raise_for_status()
    result = r.json()
    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error']}")
    return result["result"]


def get_logs_chunked(address, topic0, from_block, to_block, chunk_size=500):
    """Fetch logs in chunks to avoid Alchemy response size limits."""
    all_logs = []
    current = from_block
    while current <= to_block:
        end = min(current + chunk_size - 1, to_block)
        params = [{
            "fromBlock": hex(current),
            "toBlock":   hex(end),
            "address":   address,
            "topics":    [topic0],
        }]
        try:
            logs = rpc("eth_getLogs", params)
            all_logs.extend(logs)
            print(f"    blocks {current:,}–{end:,}: {len(logs)} events (running total: {len(all_logs)})", flush=True)
        except Exception as e:
            print(f"    ERROR blocks {current:,}–{end:,}: {e}", flush=True)
        current = end + 1
    return all_logs


def main():
    print(f"=== Alchemy Base Factory Event Query ===", flush=True)
    print(f"Time: {datetime.now(timezone.utc).isoformat()}", flush=True)

    # Get current block
    current_hex = rpc("eth_blockNumber", [])
    current_block = int(current_hex, 16)

    # Base: ~1 block per 2 seconds, 1 hour = 1800 blocks
    # Use a slightly larger margin: 2000 blocks (~67 min) to avoid missing edge
    blocks_per_hour = 1800
    from_block = current_block - blocks_per_hour

    # Verify block times
    current_ts = int(rpc("eth_getBlockByNumber", [hex(current_block), False])["timestamp"], 16)
    from_ts = int(rpc("eth_getBlockByNumber", [hex(from_block), False])["timestamp"], 16)
    actual_seconds = current_ts - from_ts

    print(f"\nCurrent block:  {current_block:,} (hex: {hex(current_block)})", flush=True)
    print(f"From block:     {from_block:,} (hex: {hex(from_block)})", flush=True)
    print(f"Block window:   {blocks_per_hour:,} blocks = {actual_seconds/3600:.2f} hours", flush=True)
    print(f"From:  {datetime.fromtimestamp(from_ts, tz=timezone.utc).isoformat()}", flush=True)
    print(f"To:    {datetime.fromtimestamp(current_ts, tz=timezone.utc).isoformat()}", flush=True)

    results = {}

    all_factories = {**FACTORIES, **EXTRA_FACTORIES}

    for name, info in all_factories.items():
        print(f"\n--- {name} ({info['address']}) ---", flush=True)
        print(f"    Event: {info['event']}", flush=True)
        try:
            logs = get_logs_chunked(info["address"], info["topic0"], from_block, current_block)
            print(f"  TOTAL: {len(logs)} {info['event']} events in last {actual_seconds/3600:.2f} hours", flush=True)
            print(f"  Rate: {len(logs) / (actual_seconds/3600):.1f} events/hour", flush=True)
            results[name] = {
                "address": info["address"],
                "event": info["event"],
                "count_1h": len(logs),
                "rate_per_hour": round(len(logs) / (actual_seconds / 3600), 1),
            }
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            results[name] = {"error": str(e)}

    # Summary
    total_pairs = sum(r.get("count_1h", 0) for r in results.values() if "count_1h" in r)
    print(f"\n=== SUMMARY ===", flush=True)
    print(f"{'Factory':<25} {'Events/hour':>12} {'Count (1h)':>12}", flush=True)
    print("-" * 52, flush=True)
    for name, r in results.items():
        if "count_1h" in r:
            print(f"  {name:<23} {r['rate_per_hour']:>12.1f} {r['count_1h']:>12}", flush=True)
        else:
            print(f"  {name:<23} {'ERROR':>12}", flush=True)
    print(f"  {'TOTAL (3 specified)':<23} {sum(results.get(k, {}).get('rate_per_hour', 0) for k in FACTORIES):>12.1f} {sum(results.get(k, {}).get('count_1h', 0) for k in FACTORIES):>12}", flush=True)
    print(f"  {'TOTAL (all 5)':<23} {sum(r.get('rate_per_hour', 0) for r in results.values() if 'count_1h' in r):>12.1f} {total_pairs:>12}", flush=True)

    print(f"\nNote: This covers {len(all_factories)} factories. Base also has", flush=True)
    print(f"      Alien Base, SwapBased, SushiSwap, and others not counted.", flush=True)
    print(f"      These numbers are a lower bound on actual Base pair launches.", flush=True)

    # Save results
    output = {
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "from_block": from_block,
        "to_block": current_block,
        "window_hours": round(actual_seconds / 3600, 3),
        "results": results,
        "total_known_factories_1h": total_pairs,
    }
    out_path = "/space/docker/containers/dex-scanner/analysis/alchemy_factory_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved: {out_path}", flush=True)


if __name__ == "__main__":
    main()

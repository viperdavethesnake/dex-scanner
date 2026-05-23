#!/usr/bin/env python3
"""
Birdeye Solana reset test — run on billing cycle reset date to determine
whether Solana token_overview access is tier-gated (permanent 400) or
was CU-exhaustion (resets monthly).

Exits 0 if both tokens return HTTP 200 (Solana access is available).
Exits 1 if either returns 4xx (still blocked — upgrade needed).

Result is written to stdout; redirect to analysis/SOLANA-RESET-TEST-<date>.md
"""
import os
import sys
import time
import json
import requests
from datetime import datetime, timezone
from pathlib import Path

# Load .env from repo root
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get("BIRDEYE_API_KEY", "")
if not API_KEY:
    print("ERROR: BIRDEYE_API_KEY not set in environment or .env")
    sys.exit(1)

BASE_URL = "https://public-api.birdeye.so/defi/token_overview"
HEADERS  = {"X-API-KEY": API_KEY, "x-chain": "solana"}
TIMEOUT  = 10

TOKENS = {
    "SOL":  "So11111111111111111111111111111111111111112",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
}

print(f"# Birdeye Solana Reset Test")
print(f"**Run at:** {datetime.now(timezone.utc).isoformat()}")
print(f"**Key prefix:** {API_KEY[:8]}...")
print()

all_ok = True
for name, address in TOKENS.items():
    try:
        t0 = time.monotonic()
        r = requests.get(BASE_URL, params={"address": address},
                         headers=HEADERS, timeout=TIMEOUT)
        ms = int((time.monotonic() - t0) * 1000)

        cu_header = r.headers.get("x-ratelimit-remaining", "n/a")
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}

        print(f"## {name} (`{address[:8]}...`)")
        print(f"- HTTP status: **{r.status_code}**")
        print(f"- Response time: {ms}ms")
        print(f"- x-ratelimit-remaining: {cu_header}")
        if r.status_code == 200:
            data = body.get("data", {})
            print(f"- success: {body.get('success')}")
            print(f"- uniqueWallet1h: {data.get('uniqueWallet1h', 'MISSING')}")
            print(f"- vBuy1hUSD: {data.get('vBuy1hUSD', 'MISSING')}")
            print(f"- vSell1hUSD: {data.get('vSell1hUSD', 'MISSING')}")
        else:
            all_ok = False
            print(f"- message: {body.get('message', body)}")
        print()

        time.sleep(1.1)  # respect 1 rps rate limit

    except requests.Timeout:
        print(f"## {name} — TIMEOUT after {TIMEOUT}s")
        all_ok = False
        print()
    except Exception as e:
        print(f"## {name} — ERROR: {e}")
        all_ok = False
        print()

print("## Result")
if all_ok:
    print("✅ **PASS** — Both Solana tokens returned HTTP 200.")
    print()
    print("**Interpretation:** Solana token_overview is accessible on the Standard free tier.")
    print("The previous 400 errors were CU-exhaustion, not tier-gating.")
    print()
    print("**Next action:** Enable Solana in collector enrichment at SAMPLE_RATE=0.02")
    print("without upgrading Birdeye plan. Update BIRDEYE-SOLANA-TIER-RESEARCH.md.")
    sys.exit(0)
else:
    print("❌ **FAIL** — One or both Solana tokens returned non-200.")
    print()
    print("**Interpretation:** Solana token_overview is tier-gated behind a paid plan.")
    print("Upgrade to Lite ($39/month) when Base auto-trading is profitable.")
    print("Do not enable Solana enrichment in the collector until then.")
    sys.exit(1)

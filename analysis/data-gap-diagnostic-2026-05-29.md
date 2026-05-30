# Data Gap Diagnostic — 2026-05-29

Scope: Birdeye enrichment coverage failure, holder/LP data availability,
API tier headroom. Pure diagnostic — no code or schema changes.

---

## Section 1 — Birdeye Enrichment: Why 0.8% Instead of 2%?

### 1.1 Configuration

All environment variables are set correctly in the running container:

```
COLLECTOR_BIRDEYE_ENRICHMENT=true
COLLECTOR_BIRDEYE_SAMPLE_RATE=0.02
COLLECTOR_BIRDEYE_MAX_PER_CYCLE=20
BIRDEYE_API_KEY=<present, 32 chars>
```

The configuration is not the problem.

### 1.2 Code Path

The sampling logic in `collector/main.py`:

```python
def _should_sample(token_address: str, scanned_at: datetime, rate: float) -> bool:
    """Deterministic per-token sampling within a 5-minute window."""
    window = scanned_at.replace(second=0, microsecond=0)
    window = window.replace(minute=(window.minute // 5) * 5)
    key = f"{token_address}:{window.isoformat()}"
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return (h % 10000) < int(rate * 10000)

def birdeye_enrich(t, conn, cycle_state: dict, scanned_at: datetime) -> None:
    if not BIRDEYE_ENRICHMENT:
        return

    if t.chain != "base":          # ← Solana ALWAYS skipped
        cycle_state["skipped_solana"] += 1
        return

    if not _should_sample(t.token_address, scanned_at, BIRDEYE_SAMPLE_RATE):
        cycle_state["skipped_sample"] += 1
        return

    # ... API call, parse, write-back
```

The `fetch_birdeye_overview()` function in `collector/api.py`:
- URL: `https://public-api.birdeye.so/defi/token_overview`
- Headers: `X-API-KEY`, `x-chain: base`
- Timeout: **5 seconds** (relevant — see birdeye_calls table)
- Parses `uniqueWallet1h` → `unique_traders_1h` and `vBuy1hUSD - vSell1hUSD` → `net_inflow_usd`
- Sets `t.birdeye_enriched = True` regardless of success or failure
- On non-200 or parse error: logs at DEBUG (suppressed in production), increments `fail` counter

The code is correct. The sample gate math (`h % 10000 < int(0.02 * 10000)` = `< 200`) correctly
selects 2% of the (token, 5-min-window) space.

### 1.3 Log Evidence

The logs are dominated by lines like:
```
birdeye: cycle done | sampled=0 skipped_solana=17 skipped_sample=1 cap_hit=0 success=0 fail=0
```

`sampled=0` almost every cycle. One success every ~50 cycles.

The grep for `birdeye.*(fail|error)` returned 487 matches — these are **false positives**
matching `fail=0` in the summary line, not actual error messages. Zero real error lines at
INFO level in the 48-hour window. Errors (429, timeouts) are emitted at DEBUG and suppressed.

### 1.4 Coverage Data

**Per chain:**

| chain  | total  | enriched | pct    |
|--------|--------|----------|--------|
| base   | 10,362 | 82       | 0.791% |
| solana | 37,285 | 0        | 0.000% |

**Time window:**

| period     | total  | enriched | pct    |
|------------|--------|----------|--------|
| last 24h   | 3,531  | 12       | 0.340% |
| last 7d    | 24,753 | 70       | 0.283% |
| last 30d   | 19,373 | 0        | 0.000% |

Enrichment started **2026-05-23** (8 of 13 collection days). Pre-May-23 rows have zero
coverage — no backfill possible without re-running the API.

**birdeye_calls audit table (all-time):**

| http_status | n  | avg_ms |
|-------------|-----|--------|
| 200         | 82  | 804    |
| NULL        | 5   | 5,059  | ← timeouts (5s limit exceeded)
| 429         | 4   | 287    | ← rate-limit hits |

Total API calls ever made: 91. Of those: 82 success, 5 timeout, 4 rate-limited.

### 1.5 Root Cause

**The 2% sample rate is applied per-token per-5-minute-window, but only to Base tokens.
Solana tokens — which make up ~78% of collector intake — are unconditionally excluded.
With ~1–2 Base tokens per cycle (vs ~8–9 Solana), expected enrichments are 0.02–0.04
per cycle. Over 8 days of enrichment operation (~2,300 cycles), that yields 46–92
expected enrichments, matching the 82 observed.**

This is not a bug. The code does exactly what it says. The gap between "2% sample rate"
and "0.8% Base coverage" is explained entirely by:

1. Solana exclusion reducing eligible tokens from 10/cycle to ~1-2/cycle.
2. Enrichment not active for the first 5 of 13 collection days (pre-2026-05-23).
3. 9 failed/timed-out calls that attempted but did not capture data.

The design assumption was "2% of all tokens get enriched." The implementation delivers
"2% of Base tokens get enriched, Solana never." With a 1:4 Base:Solana ratio, the
effective all-token rate is ~0.4%.

---

## Section 2 — Holder / Security Data: What Exists Where?

### 2.1 Collector Schema

`raw_signals` has 34 columns. The following columns that would be useful for holder
and security analysis are **absent**:

```
top1_pct, top5_pct, creator_pct, lp_locked, lp_burned,
holder_count, buy_tax, sell_tax, insider_count
```

The only Birdeye-enriched columns that exist:
- `unique_traders_1h` — populated in 82 Base rows (0.791%)
- `net_inflow_usd` — populated in same 82 rows
- `birdeye_enriched` — boolean flag

### 2.2 Scanner token_signals Schema

The scanner DB (`dex-timescale`) stores one row per LLM-rated token. Schema:

```
scanned_at, address, pair_address, symbol, chain, dex, rating,
age_minutes, price_usd, liquidity_usd, volume_1h, volume_5m,
price_ch_5m, price_ch_1h, price_ch_6h, buy_pct_5m, buy_pct_1h,
vl_ratio, vol_trend, micro_trend,
flags,              ← raw text CSV of GoPlus/RugCheck flag names only
reasoning,          ← LLM free-text
entry_price, target_price, stop_price,
price_at_5m, price_at_15m, price_at_30m, price_peak_30m,
target_hit, stop_hit,
unique_traders_1h,  ← populated if Birdeye was called by n8n workflow
net_inflow_usd
```

**Also absent** from scanner: `top1_pct`, `top5_pct`, `creator_pct`, `lp_locked`,
`lp_burned`, `holder_count`, `buy_tax`, `sell_tax`. The `flags` column holds raw
flag names (e.g., `"honeypot,high_sell_tax"`) but no structured numeric values.

The scanner has `unique_traders_1h` and `net_inflow_usd` — the same two Birdeye fields
— and outcome columns (`target_hit`, `stop_hit`, `price_at_5m/15m/30m`, `price_peak_30m`)
not present in the collector.

### 2.3 Address Overlap

| metric           | count |
|------------------|-------|
| collector unique | 2,192 |
| scanner unique   | 568   |
| overlap          | **5** |

**5 tokens appear in both datasets.** The scanner and collector are tracking nearly
disjoint token universes. The collector fetches all Base/Solana tokens from DexScreener's
`/token-profiles/latest/v1` endpoint on every 5-minute cycle. The scanner processes only
tokens surfaced by the n8n DEX scanner workflow — which requires llamacpp to be running
and the webhook to be called. Since llamacpp was down for ~20+ hours recently, and
scan batches are small (~10 tokens per run), scanner coverage is narrow.

Backfill from scanner → collector is not feasible given 5-token overlap.

### 2.4 Survivorship Bias

All 2,232 scanner records carry `rating IN ('WATCH', 'INTERESTING')`:

| rating      | n     |
|-------------|-------|
| WATCH       | 1,639 |
| INTERESTING | 593   |

SKIP-rated tokens are never written to `token_signals`. This is 100% survivorship bias:
every scanner record passed the LLM's quality filter. The scanner dataset cannot be used
for neutral analysis of what makes a token succeed or fail — it only contains tokens the
model already thought were promising.

### 2.5 Synthesis

- **Backfill from scanner is not viable**: 5 tokens in common out of 2,192 collector
  tokens. Scanner data cannot enrich collector rows at scale.
- **Is scanner data alone useful for audits?** Only for survivorship-biased questions
  (e.g., "among tokens the LLM liked, which features predicted better outcomes?"). For
  neutral filter calibration it is not usable.
- **Holder/LP structured data exists nowhere** in the current pipeline. GoPlus flags are
  stored as raw text in `token_signals.flags` but numeric values (top5_pct, creator_pct,
  lp_burned) are discarded at parse time.

---

## Section 3 — API Tier Reality Check

### 3.1 Keys Present

```
BIRDEYE_API_KEY=<present>
```

GoPlus, RugCheck, and Honeypot.is do not require API keys on their free tiers and are
not in `.env`. The trader's `security.py` calls them keylessly.

### 3.2 Live Rate-Limit Probes

| Provider     | Status | Size   | Time   | Rate-Limit Header |
|--------------|--------|--------|--------|--------------------|
| GoPlus       | 200    | 2,153B | 0.248s | None visible (undocumented free tier) |
| RugCheck     | 200    | 135B   | 0.474s | `x-rate-limit-limit: 15` (req/min) |
| Honeypot.is  | 200    | 1,344B | 0.277s | `x-ratelimit-limit: 50` (req/min) |
| Birdeye      | 200    | 8,609B | 0.840s | `x-ratelimit-limit: 300` (req/min) |

Birdeye's 300 req/min limit is the Standard tier level (paid). The API key in `.env` has
elevated quota relative to the free tier (~30–60 req/min).

### 3.3 Feasibility of 100% Collector-Side Enrichment

**Current collector load:**
- Base tokens per day: ~288 (10 tokens/cycle × 20% Base × 288 cycles/day)
- Current Birdeye calls: ~0.04/min (82 total over 8 days)

**At 100% Base enrichment (sample_rate=1.0):**
- 288 Base tokens/day = 12/hour = **0.2 req/min** against a 300 req/min limit
- Headroom: 299.8 req/min unused

**At 100% all-token enrichment (Base + Solana — Birdeye supports Solana):**
- 2,880 tokens/day = 120/hour = **2.0 req/min**
- Still <1% of available rate limit

**Scanner load at peak (12 scans/hour × 10 tokens/scan = 120 calls/hour = 2 req/min):**
- Combined scanner + collector at 100%: ~4 req/min total
- Birdeye capacity: 300 req/min
- **No rate-limit risk at any realistic collector volume.**

RugCheck (15/min) and Honeypot.is (50/min) are the tighter constraints if we add
holder/security enrichment to the collector — but current scanner usage is only ~2/min,
well below both limits.

---

## Decision-Ready Summary

### Q1: Why is Birdeye coverage 12× below design?

**The 2% sample rate applies only to Base tokens; Solana (78% of intake) is
unconditionally excluded. With ~1–2 Base tokens per cycle, the expected enrichment
rate is 2% × 1.5 = 0.03 per cycle — not 2% × 10. The 82 enriched rows over 8 days
exactly match this math. No bug; the code does what it says. To achieve 2% Base
coverage requires keeping the current rate. To achieve 2% of ALL tokens requires
either enabling Solana or raising the Base rate to ~20%.**

### Q2: Can we backfill holder data from scanner-side token_signals?

**No.** Only 5 tokens overlap between the 2,192-token collector universe and the
568-token scanner universe. Scanner data is also 100% survivorship-biased (SKIP
tokens never written). Neither structured holder data (top5_pct, creator_pct,
lp_locked) nor numeric GoPlus values exist anywhere in the current pipeline —
they are parsed to raw flag text and the numbers discarded. Backfill is not
feasible at any useful scale.

### Q3: Can we afford 100% collector-side Birdeye enrichment?

**Yes.** 100% Base enrichment = 288 calls/day = 0.2 req/min against a 300 req/min
limit. Zero rate-limit risk. API capacity is not a constraint at any collector
volume we are likely to reach. The only cost is the Birdeye Standard plan (already
active based on the 300/min limit header) and ~800ms added latency per enriched token
per cycle (non-blocking since it runs inside the poll loop per-token, not in sequence
for the entire batch).

---

## Future Audits Worth Running (Not This Diagnostic)

- **Time of day / day of week** — requires distinct aggregation approach; meaningful
  after 90+ days of data
- **DEX-by-DEX outcome breakdown** — deserves its own audit; was 1-category in the
  trained model, possibly due to training-data sparsity
- **6h price shape patterns** — requires sequence methodology (not bucket aggregation)
- **Holder concentration / LP state** (top5_pct, creator_pct, lp_locked) — requires
  adding structured GoPlus/RugCheck fields to the collector schema and calling those
  APIs per-token; the data does not currently exist anywhere in the pipeline

# Collector Birdeye Enrichment — Design Proposal
**Status:** Implemented and validated ✅  
**Date:** 2026-05-23  
**Scope:** Add `unique_traders_1h` and `net_inflow_usd` to `raw_signals` at insert time, Base chain only.

### Validation results (2026-05-23, ~13:00–17:00 UTC)

- 5 calls, 100% HTTP 200, 0 failures
- avg response time: **826ms** (target was <500ms — Standard tier is slower; 5s timeout has headroom, not a problem)
- CU header (`x-ratelimit-remaining-cu`) not returned on Standard tier — CU tracking is dashboard-only
- `birdeye_enriched=true` rows confirmed in `raw_signals` with real `unique_traders_1h` + `net_inflow_usd` values
- Feature currently **ENABLED** in `.env` (`COLLECTOR_BIRDEYE_ENRICHMENT=true`)

---

## 1. Investigation Summary

### Poll loop structure (`main.py`)

```
main() → while True:
  poll(conn)
    fetch_profiles()                          # DexScreener /token-profiles/latest/v1
    for each profile:
      fetch_pair(token_address, chain_id)     # DexScreener /latest/dex/tokens/{addr}
      signals.from_pair(pair, chain_id)       # build Token dataclass
      signals.compute_signals(t)              # compute vl_ratio, vol_trend, micro_trend, etc.
    db.bulk_insert(conn, tokens, scanned_at)  # INSERT ... VALUES %s (all tokens at once)
  backfill_outcomes(conn)                     # fill price_at_5m for pending rows
  sleep(POLL_INTERVAL - elapsed)
```

Birdeye enrichment will fire **per token, inline, after `compute_signals()` and before `bulk_insert()`**. This satisfies the "at insert time" requirement. At 0.2 sample rate with ~2.7 Base tokens/cycle, expected latency addition is ≤1.5 seconds/cycle (well within the 300-second window).

### Schema — current `raw_signals` (missing fields)

`unique_traders_1h` and `net_inflow_usd` do **not** exist. Migration required.

### Birdeye endpoint (reused verbatim from n8n enricher)

```
GET https://public-api.birdeye.so/defi/token_overview?address={token_address}
Headers:
  X-API-KEY: {BIRDEYE_API_KEY}
  x-chain: base
```

Field extraction (from n8n `Prep Birdeye Update` node):
```js
uniqueTraders1h = parseInt(data.uniqueWallet1h) || 0
netInflowUsd    = Math.round(vBuy1hUSD - vSell1hUSD)
```

### compose.yaml — `BIRDEYE_API_KEY`

Already injected into the collector container:
```yaml
env_file:
  - .env
environment:
  - BIRDEYE_API_KEY=${BIRDEYE_API_KEY}
```
No compose change required.

---

## 2. Schema Migration

### `raw_signals` — two new columns

```sql
ALTER TABLE raw_signals
  ADD COLUMN IF NOT EXISTS unique_traders_1h  INT,
  ADD COLUMN IF NOT EXISTS net_inflow_usd     NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS birdeye_enriched   BOOLEAN DEFAULT FALSE;
```

`birdeye_enriched = TRUE` means a Birdeye call was attempted for this row (success or handled failure). `FALSE` (default) means the row was not sampled / is Solana / feature is off. NULL columns = call failed or token not found.

### New table `birdeye_calls`

```sql
CREATE TABLE IF NOT EXISTS birdeye_calls (
  called_at      TIMESTAMPTZ NOT NULL,
  endpoint       TEXT NOT NULL,
  chain          TEXT NOT NULL,
  address        TEXT,
  http_status    INT,
  cu_consumed    INT,      -- from response header x-ratelimit-remaining-cu if present, else NULL
  response_ms    INT,
  error_message  TEXT
);
SELECT create_hypertable('birdeye_calls', 'called_at', if_not_exists => TRUE);
```

Migration runs at container startup via a new `db.migrate()` call in `main()` (before the poll loop). Safe to re-run (`IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`).

---

## 3. Enrichment placement in poll loop

```
poll(conn):
  fetch_profiles()
  for each profile:
    fetch_pair()
    signals.from_pair() + compute_signals()
    → birdeye_enrich(t, conn)   ← NEW: fires here if eligible
  db.bulk_insert(conn, tokens, scanned_at)
```

`birdeye_enrich(t, conn)` mutates the Token in-place:
- If feature flag off → return immediately (no-op)
- If `t.chain != 'base'` → log skip, return
- If random sample check fails → return
- If cycle cap already hit → return
- Call Birdeye, parse response, set `t.unique_traders_1h`, `t.net_inflow_usd`
- Log to `birdeye_calls` table
- Set `t.birdeye_enriched = True`

The Token dataclass gains three new optional fields with `None` defaults so existing code paths are unaffected.

---

## 4. Sampling controls

Three env vars, all respected only when enrichment is enabled:

| Env var | Default | Effect |
|---|---|---|
| `COLLECTOR_BIRDEYE_ENRICHMENT` | `false` | Master switch. `false` = entire code path is a no-op. |
| `COLLECTOR_BIRDEYE_SAMPLE_RATE` | `0.2` | Fraction of eligible Base tokens enriched per cycle. |
| `COLLECTOR_BIRDEYE_MAX_PER_CYCLE` | `20` | Hard cap on Birdeye calls per 5-min cycle. |

**Deterministic sampling per token:**
```python
import hashlib
def _should_sample(token_address: str, scanned_at: datetime, rate: float) -> bool:
    # Consistent within a 5-minute window — avoids double-enriching with n8n enricher
    window = scanned_at.replace(second=0, microsecond=0)
    window = window.replace(minute=(window.minute // 5) * 5)
    key = f"{token_address}:{window.isoformat()}"
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return (h % 10000) < int(rate * 10000)
```

A per-cycle counter resets at the start of each `poll()` call and increments on every Birdeye call attempted. Once it reaches `MAX_PER_CYCLE`, remaining tokens are skipped (logged as `cap_hit` in a cycle summary log line).

---

## 5. Error handling

All errors are caught within `birdeye_enrich()`. Nothing propagates to the poll loop.

| Condition | Behaviour |
|---|---|
| Feature flag off | Immediate return, no log |
| Solana chain | `log.debug("birdeye: skipping solana %s", addr)`, return |
| Not sampled | Silent return |
| Cycle cap hit | `log.debug("birdeye: cycle cap hit, skipping %s", addr)`, return |
| Timeout (>5s) | Token fields stay None; row inserted with `birdeye_enriched=True`; log to `birdeye_calls` with `http_status=None, error_message="timeout"` |
| HTTP 4xx | Same; log with actual `http_status` and response body snippet |
| HTTP 5xx / network error | Same; log with error |
| `success: false` in response | Same; log |
| Missing fields in response | Fields default to `None`; `birdeye_enriched=True` |

**Invariant:** `birdeye_enrich()` never raises. The collector's reliability is paramount over Birdeye coverage.

---

## 6. Instrumentation

**Per-call:** every Birdeye call (success or failure) writes one row to `birdeye_calls`.

**Per-cycle summary log line** (INFO level):
```
birdeye: cycle done | base_seen=3 sampled=1 skipped_solana=7 skipped_sample=2 cap_hit=0 success=1 fail=0 elapsed_ms=312
```

**First-hour validation query** (from NEXT-STEPS):
```sql
SELECT chain, http_status, COUNT(*), AVG(cu_consumed), AVG(response_ms)
FROM birdeye_calls
WHERE called_at > NOW() - INTERVAL '1 hour'
GROUP BY 1, 2;
```

Expected: 100% `base`, >95% HTTP 200, `cu_consumed` single digits (if header is present), `response_ms` < 500.

---

## 7. CU budget — measured and recomputed (Addition 1)

**Standard free tier: 30,000 CU/month. Budget is SHARED between the scanner n8n enricher and the new collector enrichment.**

### Scanner enricher CU consumption (measured 2026-05-23)

Queried n8n `execution_entity` table (SQLite) directly — n8n was down, GPU occupied.

| Duration bucket | Executions (this month) | Interpretation |
|---|---|---|
| 0–50ms | 1,486 | No Birdeye call (DB query, no pending tokens) |
| 50–150ms | 116 | No Birdeye call (slightly slower DB query) |
| 150–600ms | 1,956 | Mostly slow DB queries on growing TimescaleDB |
| 600ms–1s | 280 | ~30% are real Birdeye calls (~84 calls) |
| 1s–5s | 34 | Real Birdeye calls, avg ~2 per execution (~68 calls) |
| >5s | 43 | Real Birdeye calls, avg ~5 per execution (~215 calls) |

**Estimated scanner enricher calls this month: ~367**  
**Estimated scanner enricher CU consumption: ~367 × 25 = ~9,175 CU**

Note: This is an estimate from execution duration. Birdeye API does not expose monthly CU consumed in response headers. Exact figure requires checking bds.birdeye.so dashboard. **Verify before adjusting sample rate upward.**

### Available collector budget

| Item | CU |
|---|---|
| Standard monthly allotment | 30,000 |
| Scanner enricher (estimated) | −9,175 |
| **Available for collector** | **~20,825** |

**~20,825 CU available < 25,000 threshold → sample rate reduced from 0.05 to 0.02.**

### Projected collector CU at SAMPLE_RATE=0.02

- Base rows/day: ~779, × 0.02 = ~15.6 calls/day
- Monthly: ~468 calls × 25 CU = **~11,700 CU/month**
- Total (scanner + collector): ~9,175 + ~11,700 = **~20,875 CU/month** (well within 30,000) ✅
- Headroom: ~9,125 CU/month (~30% buffer)

At SAMPLE_RATE=0.03: ~17,550 CU collector + ~9,175 scanner = ~26,725 CU — fits but leaves only ~3,275 CU margin. Too thin given estimate uncertainty.

**Final default: `COLLECTOR_BIRDEYE_SAMPLE_RATE=0.02`**. Increase to 0.03 after the actual dashboard CU figure is confirmed below 8,000.

---

## 8. Files to change (after approval)

| File | Change |
|---|---|
| `collector/init.sql` | Add `ALTER TABLE` migration + `birdeye_calls` DDL |
| `collector/db.py` | Add `migrate()`, `log_birdeye_call()`, update `INSERT_SQL` + `bulk_insert()` for new columns |
| `collector/api.py` | Add `fetch_birdeye_overview(address, api_key, timeout=5)` |
| `collector/signals.py` | Add 3 optional fields to `Token` dataclass |
| `collector/main.py` | Add `birdeye_enrich()`, env var parsing, cycle counter; call `db.migrate()` at startup |
| `.env.example` | Add `COLLECTOR_BIRDEYE_ENRICHMENT`, `COLLECTOR_BIRDEYE_SAMPLE_RATE`, `COLLECTOR_BIRDEYE_MAX_PER_CYCLE` |
| `docs/RESUME.md` | Add session note |

No changes to compose.yaml (`BIRDEYE_API_KEY` already injected). No changes to n8n stack.

---

## 9. Rollback plan

If enrichment causes problems after deploy:

1. Set `COLLECTOR_BIRDEYE_ENRICHMENT=false` in `.env`
2. `docker compose restart dex-collector` — enrichment is immediately a no-op
3. No data loss: existing rows keep their `NULL` Birdeye fields; new rows insert as before
4. `birdeye_calls` table retains the call history for post-mortem analysis
5. If DB migration caused issues: the new columns are nullable with no constraints, so the existing INSERT (without the new columns) continues to work via the ON CONFLICT DO NOTHING logic

The migration is **purely additive**. Rollback of the code is safe without rolling back the schema.

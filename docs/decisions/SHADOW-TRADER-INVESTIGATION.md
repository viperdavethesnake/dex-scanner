# Shadow Trader — Phase 1 Investigation

**Issued:** 2026-05-25  
**Status:** Complete — awaiting user feedback before Phase 2 (design)  
**Scope:** Code archaeology, aggregator API surface, model artifact state, trade log schema

---

## 1. Existing Code State

### 1.1 Collector service pattern (`collector/`)

The collector is the template for any new Python service in this stack. Key patterns to reuse verbatim:

| Pattern | Implementation |
|---|---|
| Entry point | `main.py` — flat `main()` with `while True` loop, `time.sleep()` at the end |
| DB connection | `db.connect()` — env-var driven DSN, retry loop (10 attempts, 5s backoff), `autocommit=False` |
| Schema init | `db.migrate()` called once at startup — all `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (idempotent) |
| Bulk insert | `psycopg2.extras.execute_values()` with `ON CONFLICT DO NOTHING` |
| Error handling | `try/except` around each major step, `conn.rollback()` on exception, log and continue — never crash the loop |
| Env vars | Read at module top level with `os.environ.get("VAR", "default")`. Feature flags cast inline (`== "true"`). |
| API layer | Separate `api.py` — plain `requests.get()` with retry+backoff wrapper, per-endpoint functions |
| Signal logic | Separate `signals.py` — `@dataclass Token`, pure `from_pair()` + `compute_signals()` functions |
| Logging | `logging.basicConfig()` in `main.py`, `log = logging.getLogger(__name__)` per module |

**Poll loop structure (exact):**
```
main()
  └── while True:
        ├── poll(conn)          # fetch DexScreener → compute signals → bulk insert
        ├── backfill_outcomes(conn)  # fill price_at_5m for rows >5min old
        └── sleep(POLL_INTERVAL - elapsed)
```

Each step is wrapped independently — an error in `poll()` doesn't prevent `backfill_outcomes()` from running.

### 1.2 `analysis/ml.py` — model training

**Critical finding: the model is never persisted to disk.** `ml.py` trains a `LGBMClassifier`, evaluates it, generates figures, and exits. There is no `model.save_model()`, `joblib.dump()`, or `pickle.dump()` call anywhere in the file.

**Implications for `dex-trader`:**
- There is no model file to load today. The trader cannot score tokens without first producing a saved model artifact.
- A model serialization step must be added to `ml.py` (or a separate `export_model.py`) before the trader is built.
- Recommended: `model.booster_.save_model("models/lgbm_base.txt")` (LightGBM native text format) + a companion `models/feature_list.json` (feature column names in order) + `models/metadata.json` (threshold, train date, AUC). These three files are the full artifact the trader needs.

**Training details relevant to trader design:**
- Binary target: `outcome_pct > 0` (win/loss, not magnitude)
- Feature set: 30+ columns including categoricals (`chain`, `dex`, `micro_trend`, `vol_trend`) encoded as LightGBM native categories
- Recommended operating threshold from backtest: **≥0.65** (49–55% post-cost win rate, 2.5x profit factor)
- Model is retrained from scratch each run on all data before `TRAIN_CUTOFF`; no incremental update

### 1.3 `analysis/backtest.py` — fill simulation and cost assumptions

**Explicit constants at top of file:**
```python
ROUND_TRIP_COST_PCT = 1.5   # % — gas + swap fee + slippage (combined)
BET_SIZE = 10.0              # $ per trade (flat)
STARTING_CAPITAL = 100.0
```

**Fill model (simplified):**
```python
df['net_pct'] = df['outcome_pct'] - ROUND_TRIP_COST_PCT  # 1.5% flat deduction
df['pnl']     = bet_size * df['net_pct'] / 100.0
df['win']     = df['net_pct'] > 0
```

**What the 1.5% covers (assumed, not measured):**
- Gas cost on Base (≈$0.05–0.15 per tx at current prices → ~0.5–1.5% of $10 trade)
- Swap fee (Aerodrome/Uniswap V3: 0.05–0.30% per leg → 0.1–0.6% round-trip)
- Slippage (assumed ~0.4–0.9% for thin new tokens)

**What the shadow trader must measure:** whether the real aggregator quote prices + actual gas costs align with this 1.5% assumption. This is the primary research question of Phase 4.

**Exit model:** `outcome_pct` is the percentage change from `price_usd` at scan time to `price_at_5m` (collected by the outcome backfiller). This is a DexScreener price poll at T+5min — not an aggregator quote. The shadow trader must decide how to model exit (see §4, open question #3).

### 1.4 Schema patterns (`init.sql`, `collector/init.sql`)

**Scanner DB** (`dex-timescale`, `dex_signals`):
- `scan_summary` — one row per scan run (metadata)
- `token_signals` — one row per WATCH/INTERESTING token, includes LLM output, entry/target/stop prices, Birdeye enrichment, and outcome backfill columns

**Collector DB** (`dex-collector-db`, `collector_signals`):
- `raw_signals` — hypertable, partitioned on `scanned_at`, UNIQUE on `(token_address, pair_address, scanned_at)`, includes Birdeye enrichment columns
- `birdeye_calls` — audit log hypertable for every API call

**Schema conventions:**
- All timestamp columns: `TIMESTAMPTZ NOT NULL`
- All money columns: `NUMERIC(20,4)` for large values, `NUMERIC(14,2)` for P&L-scale values, `NUMERIC(30,12)` for price
- Hypertables via `SELECT create_hypertable(..., if_not_exists => TRUE)`
- All indexes use `IF NOT EXISTS`
- JSONB not used anywhere yet — first use would be in the trader's feature snapshot

### 1.5 `compose.yaml` — networking and service patterns

**Two independent network groups exist already:**

| Group | Network | DB Port |
|---|---|---|
| Scanner (GPU) | `macvlan_net` (external, 192.168.33.231) | internal only |
| Collector (no GPU) | `dex-collector-net` (bridge) | 5434 (host-exposed for analysis scripts) |

**Trader group fits the collector pattern exactly:**
- Bridge network, isolated from scanner macvlan
- `env_file: .env` for secrets
- `environment:` block for non-secret config (DB host, port, feature flags)
- `depends_on:` the DB service (no condition needed — DB comes up fast)
- `build: context: ./dex-trader`
- No `devices:` (GPU-free)

**Healthcheck pattern (collector DB example):**
```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U collector || exit 1"]
  interval: 30s
  timeout: 10s
  retries: 5
```

**`BIRDEYE_API_KEY` is already injected into the collector via `${BIRDEYE_API_KEY}` from `.env`.** The trader will need `ZEROX_API_KEY` and potentially `WEB3_PROVIDER_URL` added the same way.

### 1.6 `docs/ROADMAP.md` Phase 2/3/5 — pre-decided architecture

Key decisions already locked (do not re-litigate):
- **dex-trader** service: independent Python service, no n8n dependency, no LLM
- **Decision loop:** pull signals → score with LightGBM → for qualifying tokens: check position limits → buy → record. At T+5m: sell → record outcome.
- **Exit rule:** hard 5-minute timer, no exceptions, no re-entry within 30min
- **Circuit breakers:** max 3 simultaneous positions, $10–15/trade, $50/day loss limit
- **Chain order:** Base first, Solana after Base is proven
- **Target architecture:** `dex-trader` + `dex-trader-db` as a third independent group in compose.yaml
- **Optional:** `dex-model-server` (FastAPI scoring endpoint) — not required for shadow mode

---

## 2. Aggregator API Surface

### 2.1 Jupiter (Solana) — v1 Quote API

**Quote endpoint:** `GET https://api.jup.ag/swap/v1/quote`

**Required parameters:**
| Param | Type | Notes |
|---|---|---|
| `inputMint` | string | Token mint address (from) |
| `outputMint` | string | Token mint address (to) |
| `amount` | uint64 | Raw amount in base units (before decimals) |

**Key optional parameters:**
| Param | Default | Notes |
|---|---|---|
| `slippageBps` | 50 | Slippage tolerance in bps |
| `onlyDirectRoutes` | false | Single-hop only (faster, less optimal) |
| `swapMode` | ExactIn | ExactIn or ExactOut |

**Key response fields:**
| Field | Notes |
|---|---|
| `outAmount` | Best output after AMM fees — use for effective price |
| `otherAmountThreshold` | Minimum output after slippage — use for worst-case |
| `priceImpactPct` | True on-chain price impact — best slippage proxy |
| `slippageBps` | Echo of the requested tolerance |
| `routePlan` | Array of hop objects with DEX label, amounts, fee |

**Effective price:** `outAmount / inAmount` (both in raw units; divide by decimal ratio)  
**Expected slippage:** `priceImpactPct` field directly

**Auth:** API key required via `x-api-key` header. Get free key at `https://developers.jup.ag/portal`.

**Rate limits (free tier):**
| Plan | RPS | RPM |
|---|---|---|
| Keyless (no key) | 0.5 | 30 |
| **Free (key required)** | **1** | **60** |
| Developer | 10 | 600 |

**Assessment for shadow trader:** Free tier (1 RPS) is adequate for a 5-minute loop. At most 3 simultaneous position checks + a handful of entry quotes = well under 60 RPM. No cost. Solana is deferred anyway — Jupiter is Phase 2+ infrastructure.

### 2.2 0x Swap API v2 (Base)

**Quote endpoint:** `GET https://api.0x.org/swap/permit2/quote`  
**Required header:** `0x-version: v2`  
**API key header:** `0x-api-key: <key>`

**Required parameters for Base:**
| Param | Notes |
|---|---|
| `sellToken` | ERC20 address or symbol |
| `buyToken` | ERC20 address or symbol |
| `sellAmount` | In base units (wei/smallest denomination) |
| `taker` | Wallet address that will execute |
| `chainId` | `8453` for Base |

**Key optional parameters:**
| Param | Default | Notes |
|---|---|---|
| `slippageBps` | 100 | Max acceptable slippage |

**Key response fields:**
| Field | Notes |
|---|---|
| `liquidityAvailable` | Boolean — if false, no quote; new token has no liquidity yet |
| `price` | Quoted price at request time |
| `guaranteedPrice` | Minimum price guaranteed (accounts for slippageBps) |
| `buyAmount` | Output tokens received |
| `minBuyAmount` | Minimum with slippage applied |
| `estimatedGas` | Gas units required |
| `fees.gasFee` | Gas cost in USD |
| `route.fills` | Array of `{source, proportionBps}` — which DEXes used |
| `issues` | Validation failures (allowance, balance, simulation) |

**Effective price:** `buyAmount / sellAmount` adjusted for decimals  
**Expected slippage:** `(price - guaranteedPrice) / price × 10000` bps

**Auth:** API key required. Free at `https://dashboard.0x.org/apps`.

**Free tier limits:**
- 1 RPS
- 1M calls/month
- No payment required

**Critical limitation for new tokens:** If `liquidityAvailable: false`, no quote is returned. Very new tokens with thin on-chain liquidity will fail here. This is the primary reason a fallback is needed.

**Assessment:** 0x is the right primary aggregator for Base — good routing, free tier adequate for shadow mode. The `liquidityAvailable` false case is expected and must be handled.

### 2.3 Base Fallback — Direct AMM Quotes

For tokens too new/thin for 0x to quote, two direct on-chain options:

**Aerodrome Finance (largest Base DEX by TVL):**
- Router: `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`
- Quote via `router.getAmountsOut(amountIn, routes)` — `routes` is array of `{from, to, stable, factory}`
- Factory for volatile pools: `0x420DD381b31aEf6683db6B902084cB0FFECe40D` (Aerodrome V2)
- Requires knowing whether pool is stable or volatile; new meme coins are always volatile

**Uniswap V3 Quoter (Base):**
- QuoterV2: `0x3d4e44Eb1374240CE5F1B136Cf395a8eae0e6953`
- `quoteExactInputSingle((tokenIn, tokenOut, amountIn, fee, sqrtPriceLimitX96))` → `(amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)`
- Fee tiers on Base: 100 (0.01%), 500 (0.05%), 3000 (0.3%), 10000 (1%)
- New tokens usually pool at 1% (10000 bps) fee tier on Uniswap V3

**Assessment:** Aerodrome is the better fallback — it's where most new Base meme coins actually launch. Direct `getAmountsOut` call via web3.py is straightforward. Uniswap V3 QuoterV2 is the secondary fallback. Both require a Web3 RPC endpoint (e.g., Base public RPC: `https://mainnet.base.org`, or Alchemy/Infura for reliability).

**Recommended fallback chain for new Base tokens:**
```
0x quote → if liquidityAvailable=false → Aerodrome getAmountsOut → if no pool → Uniswap V3 QuoterV2 → if no pool → log "no quote available", skip trade
```

---

## 3. Model File Artifact

**Current state:** No saved model file exists anywhere in the repo.

`ml.py` trains entirely in memory. The last line before `main()` returns is `print(f"\n✓ Done. Figures in analysis/figures/")` — no serialization.

**What needs to be built before the trader can score:**

A `save_model()` step must be added to `ml.py` (or extracted to `export_model.py`). The three-file artifact format:

```
analysis/models/
  lgbm_base.txt           # LightGBM native booster format (model.booster_.save_model())
  feature_list.json       # ["chain","dex","age_minutes",...] — ordered list
  metadata.json           # {"threshold":0.65,"train_cutoff":"2026-05-21","val_auc":0.61,"trained_at":"..."}
```

**Loading in the trader:**
```python
import lightgbm as lgb, json
booster = lgb.Booster(model_file="models/lgbm_base.txt")
features = json.load(open("models/feature_list.json"))
meta     = json.load(open("models/metadata.json"))
threshold = meta["threshold"]

# Score a token:
X = pd.DataFrame([token_features])[features]
score = booster.predict(X)[0]   # probability of win
```

**Hot-reload:** Watch `lgbm_base.txt` mtime every N minutes; if changed, reload booster. This allows weekly retrain without restarting the trader container.

**This is a prerequisite blocker.** The model save step must be done before Phase 3 (implementation) begins.

---

## 4. Trade Log Schema

The central question of shadow mode: **do real aggregator quotes match the backtest's 1.5% round-trip cost assumption?**

The `trades` table must capture every step of the trade lifecycle with enough detail to answer this question and compute real P&L.

```sql
CREATE TABLE IF NOT EXISTS trades (
    id               BIGSERIAL        NOT NULL,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    -- Identity
    chain            TEXT             NOT NULL,   -- 'base' | 'solana'
    token_address    TEXT             NOT NULL,
    pair_address     TEXT,
    symbol           TEXT,

    -- Signal at decision time
    signal_ts        TIMESTAMPTZ      NOT NULL,   -- when raw_signals row was written
    signal_price_usd NUMERIC(30,12),              -- DexScreener price at signal time
    conviction_score REAL             NOT NULL,   -- LightGBM p(win)
    model_version    TEXT,                        -- metadata.json trained_at stamp
    signal_features  JSONB,                       -- full feature dict at decision time

    -- Quote (entry)
    quote_ts         TIMESTAMPTZ,                 -- when aggregator quote was requested
    quote_source     TEXT,                        -- '0x' | 'aerodrome' | 'uniswap_v3' | 'jupiter' | 'none'
    quote_price_usd  NUMERIC(30,12),              -- effective price from aggregator
    quote_slippage_bps INT,                       -- priceImpactPct → bps, or estimated
    quote_gas_usd    NUMERIC(10,4),               -- gas cost estimate from aggregator
    quote_latency_ms INT,                         -- ms from signal to quote response

    -- Simulated fill (entry)
    fill_ts          TIMESTAMPTZ,                 -- when we would have submitted
    fill_size_usd    NUMERIC(10,4),               -- trade size in USD (e.g. 10.00)
    fill_price_usd   NUMERIC(30,12),              -- quote_price_usd (shadow mode)
    entry_cost_pct   REAL,                        -- (quote_price - signal_price) / signal_price * 100

    -- Simulated exit (T+5min)
    exit_ts          TIMESTAMPTZ,                 -- when exit was triggered
    exit_trigger     TEXT,                        -- 'timer' | 'stop_loss' | 'take_profit' | 'kill_switch'
    exit_price_usd   NUMERIC(30,12),              -- DexScreener price at T+5min (from outcome backfiller)
    exit_quote_usd   NUMERIC(30,12),              -- aggregator quote at exit time (if requested)

    -- P&L
    gross_pct        REAL,                        -- (exit_price - fill_price) / fill_price * 100
    cost_pct         REAL,                        -- actual total cost (gas + fee + slippage estimate)
    net_pct          REAL,                        -- gross_pct - cost_pct
    pnl_usd          NUMERIC(10,4),               -- fill_size_usd * net_pct / 100

    -- Backtest comparison
    backtest_cost_pct REAL DEFAULT 1.5,           -- constant from backtest assumptions
    backtest_net_pct  REAL,                       -- gross_pct - 1.5 (what backtest assumed)
    cost_delta_pct    REAL,                       -- cost_pct - backtest_cost_pct (the gap we're measuring)

    -- State machine
    status           TEXT             NOT NULL    -- see below
        CHECK (status IN ('intent','quoted','simulated','managed','exited','failed','skipped')),
    failure_reason   TEXT,                        -- populated if status = 'failed' | 'skipped'

    PRIMARY KEY (id, created_at)
);

SELECT create_hypertable('trades', 'created_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_trades_token   ON trades (token_address, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status  ON trades (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_open    ON trades (created_at DESC) WHERE status IN ('intent','quoted','simulated','managed');
```

**Status state machine:**
```
intent → quoted → simulated → managed → exited
                                       ↗
                            (timer fires at T+5)
Any state → failed    (unrecoverable error)
Any state → skipped   (risk check blocked entry: position limit, daily loss cap, kill switch)
```

**Key diagnostic queries for Phase 4:**

```sql
-- Edge survival: does shadow match backtest?
SELECT
  COUNT(*)                          AS n_trades,
  AVG(gross_pct)                    AS avg_gross_pct,
  AVG(cost_pct)                     AS avg_real_cost,
  AVG(backtest_cost_pct)            AS avg_assumed_cost,
  AVG(cost_delta_pct)               AS avg_cost_gap,   -- real - assumed
  AVG(net_pct)                      AS avg_net_pct,
  COUNT(*) FILTER (WHERE net_pct>0) AS wins,
  AVG(pnl_usd) * COUNT(*)           AS total_pnl_usd
FROM trades
WHERE status = 'exited'
  AND created_at > NOW() - INTERVAL '7 days';

-- Scan latency cost (signal price vs quote price)
SELECT
  quote_source,
  AVG(quote_latency_ms)             AS avg_latency_ms,
  AVG(entry_cost_pct)               AS avg_entry_slippage_pct,
  AVG(quote_slippage_bps) / 100.0   AS avg_price_impact_pct
FROM trades
WHERE status != 'skipped'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY quote_source;

-- Token coverage: how often do we get quotes?
SELECT
  quote_source,
  COUNT(*) AS n,
  COUNT(*) FILTER (WHERE quote_source = 'none') AS no_quote
FROM trades
GROUP BY quote_source;
```

---

## 5. Open Questions for Phase 2 Design

These require design decisions — listed here so Phase 2 addresses them explicitly:

1. **Signal ingestion method:** How does `dex-trader` get scanner signals? Three options: (a) poll `raw_signals` directly on `dex-collector-db`, (b) poll `token_signals` on `dex-timescale` (scanner DB — signals that passed scanner filters), (c) compute signals independently from DexScreener (duplicate of collector work). Option (a) is the simplest but couples the trader to the collector's DB. Option (b) only gets INTERESTING/WATCH tokens (scanner must be running). Option (c) is fully independent but duplicates work. This choice drives the service architecture significantly.

2. **Model save step timing:** Who runs `export_model.py`? Manual trigger before trader launch, or a cron inside the trader that retriggers when `ml.py` is rerun externally? Needs a concrete answer before implementation.

3. **Exit price source:** `outcome_pct` in `raw_signals` is a DexScreener price poll at T+5min collected by the outcome backfiller. The shadow trader can reuse this (easiest, avoids a second API call) or poll DexScreener itself at exit time (independent, real-time). The collector's `backfill_outcomes()` loop fills within 5–10 minutes of the target time — close enough for shadow mode but potentially stale for live mode.

4. **Web3 RPC for on-chain quotes:** A Base RPC endpoint is needed for Aerodrome/Uniswap V3 fallback quotes. Public Base RPC (`https://mainnet.base.org`) works but is rate-limited. Alchemy/Infura free tiers are more reliable. Is there a preference? This is also the endpoint needed for live execution.

5. **`dex-trader-db` vs reusing `dex-collector-db`:** Separate DB is cleaner isolation but another TimescaleDB instance. Collector DB at port 5434 is already running and has room. The prompt asks the design to recommend one — this investigation flags it as a meaningful tradeoff.

6. **Kill switch mechanism:** Env var (`KILL_SWITCH=true` → restart container), file-based (touch `/tmp/kill` → trader stops entering), or a DB flag the trader polls. Design must pick one.

7. **Port exposure:** Collector DB is exposed on `5434` for analysis scripts. Trader DB should similarly be exposed on a new port (e.g., `5435`) so Python analysis scripts can query it directly from the host. Confirm this is desired.

---

## Summary

| Area | Finding |
|---|---|
| Service pattern | Collector is the template — reuse db.py/api.py/signals.py structure verbatim |
| Model artifact | **Does not exist** — `ml.py` has no serialization; must be built before Phase 3 |
| Backtest cost assumption | 1.5% flat round-trip (gas + fee + slippage); shadow mode measures whether this holds |
| 0x API (Base) | Free, key required, 1 RPS, `liquidityAvailable=false` for new tokens → need fallback |
| Jupiter (Solana) | Free key, 1 RPS free tier, adequate for 5-min loop; Solana deferred anyway |
| On-chain fallback (Base) | Aerodrome Router `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43` → Uniswap V3 QuoterV2 `0x3d4e44Eb1374240CE5F1B136Cf395a8eae0e6953` |
| Trade log schema | Designed above — captures every step, enables direct backtest-vs-shadow comparison |
| Compose pattern | Third bridge network group, mirrors collector group exactly |
| Blocking prereq | Model save step must be added to `ml.py` before implementation |

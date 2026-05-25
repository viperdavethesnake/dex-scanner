# Shadow Trader — Phase 2 Design

**Issued:** 2026-05-25  
**Status:** Approved — Phase 3 implementation ready to begin  
**Decisions incorporated from user review of investigation open questions: Q1–Q7 + A–E + prereq Q&A**

---

## 0. Prerequisites for Phase 3

Before a single line of trader code is written, these must exist:

| # | Prereq | Owner | Blocker for |
|---|---|---|---|
| P1 | `analysis/export_model.py` written and run — produces `analysis/models/lgbm_base.txt`, `feature_list.json`, `metadata.json` | Manual (user triggers) | Trader startup |
| P2 | `ZEROX_API_KEY` obtained from `dashboard.0x.org` (free) | User | Quote module |
| P3 | `ALCHEMY_BASE_URL` obtained from `dashboard.alchemy.com` (free) | User | Aerodrome/Uniswap fallback |
| P4 | `JUPITER_API_KEY` obtained from `developers.jup.ag` (free, Solana-deferred but wire it now) | User | Jupiter module (stubbed) |
| P5 | `./trader_data/` directory created on host (TimescaleDB volume mount) | Phase 3 commit (auto-created by implementation step) | DB init |

P1 is complete (export_model.py written and run; artifacts confirmed). P2–P4 API keys are obtained and in `.env`. P5 is created in the Phase 3 implementation commit. All five are satisfied before `docker compose up` is attempted on the trader group.

---

## 1. Service Architecture

### 1.1 Process model

Single Python process, single thread, single `while True` loop. Same pattern as the collector — proven, simple, no async complexity.

```
main()
  └── startup:
        ├── db.connect(trader_db)       # own DB for writes
        ├── db.connect(collector_db)    # collector DB for reads (raw_signals)
        ├── db.migrate(trader_db)       # idempotent schema init
        ├── scorer.load()               # LightGBM booster + feature list
        ├── security.init_cache()       # empty in-memory cache
        └── risk.load_state(trader_db)  # reload open positions + watermark from DB

  └── while True:
        ├── check_kill_switch(trader_db)    # abort loop if flagged
        ├── ingest_signals(collector_db)    # pull new raw_signals past watermark
        ├── for each new signal:
        │     ├── apply_hard_filters()      # age, micro_trend, V/L — same as scanner
        │     ├── score(signal)             # LightGBM → p(win)
        │     ├── if score >= threshold:
        │     │     ├── check_risk_gates()  # position limit, daily loss, re-entry, kill switch
        │     │     ├── check_security()    # GoPlus + Honeypot.is (cached 1h)
        │     │     ├── get_quote()         # 0x → Aerodrome → Uniswap V3
        │     │     ├── validate_quote()    # drift check, slippage check
        │     │     └── record_intent()     # write trades row, status=simulated
        ├── manage_open_positions()     # check exit timer (fill_ts + 5min)
        │     └── for each expired position:
        │           ├── get_exit_quote()    # real aggregator quote at exit time
        │           ├── poll_dexscreener()  # DexScreener price at exit (parallel truth)
        │           └── close_position()    # compute P&L, write final trades row
        ├── scorer.maybe_reload()       # check model mtime, hot-reload if changed
        └── sleep(max(0, POLL_INTERVAL - elapsed))   # POLL_INTERVAL=5s
```

### 1.2 Separation of concerns — module layout

```
dex-trader/
  main.py          Entry point, main loop, wires modules together
  db.py            DB connections, migrate(), all SQL — never leaks into other modules
  signals.py       hard_filter() — scanner filter replica (see §3)
  scorer.py        LightGBM load, hot-reload, score()
  security.py      GoPlus + Honeypot.is checks, 1h in-memory cache
  aggregators/
    __init__.py    Quote dataclass, get_quote() dispatcher
    zerox.py       0x Swap API v2 wrapper
    aerodrome.py   Aerodrome Router on-chain wrapper (web3.py)
    uniswap.py     Uniswap V3 QuoterV2 on-chain wrapper (web3.py)
    jupiter.py     Jupiter v1 stub (raises NotImplementedError; Solana deferred)
  simulator.py     fill simulation, exit simulation, P&L computation
  risk.py          position limit, daily loss cap, re-entry lock, kill switch poll
  Dockerfile
  init.sql         trader_db schema
  requirements.txt
```

### 1.3 Restart behavior and crash recovery

On startup, `risk.load_state()` queries:
```sql
SELECT * FROM trades WHERE status IN ('intent','quoted','simulated','managed');
```
Any open trades are rebuilt into in-memory position state. The watermark is read from `signal_watermark`. This means a clean restart loses no state — the DB is the source of truth.

**Edge case:** a position was in `managed` state when the process crashed, and 5 minutes have passed. On restart, `manage_open_positions()` will immediately fire the exit for it. This is correct behavior.

### 1.4 Healthcheck

Expose a minimal HTTP endpoint via `http.server` in a daemon thread:

```
GET http://localhost:8090/health
→ 200 {"status":"ok","open_positions":N,"last_signal_ts":"...","model_version":"..."}
```

Used by Docker healthcheck. Compose definition:
```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -fs http://localhost:8090/health || exit 1"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 30s
```

---

## 2. Database

### 2.1 Decision: separate `dex-trader-db` ✅

**Rationale:** Clean blast radius. If the trader DB is corrupted or needs a schema wipe, the collector continues running unaffected. The two services have different backup cadences and different retention needs. The marginal resource cost of a second TimescaleDB is negligible (idle Postgres uses ~50MB RAM).

**Connection summary:**

| From | To | Purpose |
|---|---|---|
| `dex-trader` | `dex-trader-db` (port 5432 internal) | All writes; trader_state; watermark |
| `dex-trader` | `dex-collector-db` (port 5432 internal) | Read-only: `SELECT` from `raw_signals` |
| Host analysis scripts | `dex-trader-db` (port **5435** host) | Post-hoc queries on shadow trades |

The trader service joins both networks: `dex-trader-net` (own DB) and `dex-collector-net` (read raw_signals).

### 2.2 Schema — `dex-trader-db`

**`init.sql`:**

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── trades ────────────────────────────────────────────────────────────────────
-- One row per trade candidate. Created at 'intent'; updated through lifecycle.
-- The hypertable partition key is created_at (immutable), so all UPDATEs
-- must include created_at in the WHERE clause.

CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL        NOT NULL,
    created_at          TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    -- Identity
    chain               TEXT             NOT NULL,   -- 'base' | 'solana'
    token_address       TEXT             NOT NULL,
    pair_address        TEXT,
    symbol              TEXT,

    -- Signal at decision time (from raw_signals)
    signal_ts           TIMESTAMPTZ      NOT NULL,   -- raw_signals.scanned_at
    signal_price_usd    NUMERIC(30,12),              -- DexScreener price at signal time
    conviction_score    REAL             NOT NULL,   -- LightGBM p(win)
    conviction_band     TEXT,                        -- 'shadow_only'(0.65–0.70) | 'live_eligible'(>=0.70); enables post-hoc threshold analysis
    model_version       TEXT,                        -- metadata.json trained_at
    signal_features     JSONB,                       -- full feature dict at decision time
    collector_signal_id BIGINT,                      -- raw_signals.id for traceability

    -- Security check
    security_checked    BOOLEAN          NOT NULL DEFAULT FALSE,
    security_passed     BOOLEAN,                     -- null = not checked
    security_source     TEXT,                        -- 'goplus+honeypot' | 'cache' | 'skipped'
    security_flags      TEXT,                        -- comma-sep list of flags if failed

    -- Quote (entry)
    quote_ts            TIMESTAMPTZ,
    quote_source        TEXT,                        -- '0x'|'aerodrome'|'uniswap_v3'|'none'
    quote_price_usd     NUMERIC(30,12),
    quote_slippage_bps  INT,
    quote_gas_usd       NUMERIC(10,4),
    quote_latency_ms    INT,

    -- Simulated fill (entry) — shadow mode: no real tx
    fill_ts             TIMESTAMPTZ,                 -- T₀ for exit timer
    fill_size_usd       NUMERIC(10,4),
    fill_price_usd      NUMERIC(30,12),              -- = quote_price_usd in shadow mode
    entry_cost_pct      REAL,                        -- (quote_price - signal_price) / signal_price * 100

    -- Exit
    -- fill_ts + POSITION_HOLD_SECONDS = expected_exit_ts
    exit_ts             TIMESTAMPTZ,                 -- actual exit time (fill_ts + ~5min)
    exit_trigger        TEXT,                        -- 'timer' | 'kill_switch'
    exit_price_usd      NUMERIC(30,12),              -- DexScreener poll at exit time (parallel truth)
    exit_quote_usd      NUMERIC(30,12),              -- aggregator quote at exit time (primary)
    exit_quote_source   TEXT,
    exit_quote_latency_ms INT,

    -- P&L — computed using exit_quote_usd as exit price (not DexScreener)
    gross_pct           REAL,                        -- (exit_quote_usd - fill_price_usd) / fill_price_usd * 100
    cost_pct            REAL,                        -- entry_cost_pct + exit_slippage_bps/100 + gas_pct
    net_pct             REAL,                        -- gross_pct - cost_pct
    pnl_usd             NUMERIC(10,4),               -- fill_size_usd * net_pct / 100

    -- Backtest comparison
    backtest_cost_pct   REAL NOT NULL DEFAULT 1.5,
    backtest_net_pct    REAL,                        -- (exit_price_usd-based gross) - 1.5
    cost_delta_pct      REAL,                        -- cost_pct - 1.5

    -- State
    status              TEXT             NOT NULL
        CHECK (status IN ('intent','quoted','simulated','managed','exited','failed','skipped')),
    failure_reason      TEXT,

    PRIMARY KEY (id, created_at)
);

SELECT create_hypertable('trades', 'created_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_trades_token  ON trades (token_address, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_open   ON trades (created_at DESC)
    WHERE status IN ('intent','quoted','simulated','managed');


-- ── trader_state ──────────────────────────────────────────────────────────────
-- Key-value config table. Kill switch lives here.
-- Toggle kill switch: UPDATE trader_state SET value='true' WHERE key='kill_switch';

CREATE TABLE IF NOT EXISTS trader_state (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO trader_state (key, value) VALUES
    ('kill_switch', 'false'),
    ('shadow_mode',  'true')
ON CONFLICT DO NOTHING;


-- ── signal_watermark ──────────────────────────────────────────────────────────
-- Tracks how far we've read into raw_signals so we never double-process.
-- Updated after each ingest cycle.

CREATE TABLE IF NOT EXISTS signal_watermark (
    id         INT         PRIMARY KEY DEFAULT 1,
    last_id    BIGINT      NOT NULL DEFAULT 0,
    last_ts    TIMESTAMPTZ
);

INSERT INTO signal_watermark (id, last_id) VALUES (1, 0) ON CONFLICT DO NOTHING;
```

### 2.3 Migration strategy

Same as collector: `db.migrate()` called once at startup, all statements idempotent (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`). `init.sql` is the canonical schema for fresh installs; `migrate()` handles running instances. Both must stay in sync.

---

## 3. Signal Ingestion

### 3.1 Decision: poll `raw_signals` on `dex-collector-db` every 5 seconds ✅

**Rationale:** Simple. No collector modification needed. 5s lag in a 5-minute hold window is 1.7% of the position lifetime — immaterial.

**Trade-off vs Postgres LISTEN/NOTIFY:** LISTEN/NOTIFY would give <100ms pickup but requires the collector to call `NOTIFY` after each `bulk_insert()` — a cross-service coupling change. The latency savings don't justify the coupling. Revisit if sub-second pickup becomes a trading requirement in live mode.

**The real latency is the collector's poll cycle, not the trader's pickup.**

The collector polls DexScreener every 5 minutes. A signal generated at T=0 may not land in `raw_signals` until T=300. The trader then picks it up within 5 seconds. So end-to-end signal-to-intent latency is dominated by the collector cycle:

```
DexScreener data age    → 0s (by definition; it's the current price)
Collector poll lag      → 0–300s (up to 5 minutes; avg ~150s)
Trader pickup lag       → 0–5s
Quote request           → 200–500ms
Security check          → 300ms (cached) to 1s (cold)
─────────────────────────────────────
Intent recorded         → signal_price_usd may be 0–5 minutes stale
```

This staleness is the primary source of entry_cost_pct. Shadow mode measures it explicitly. If it's consistently >1%, the collector cycle should be tightened before live mode (or the trader should fetch its own DexScreener price at quote time and compare).

### 3.2 Ingestion query

```sql
-- Run every 5 seconds. Returns new rows since last watermark, Base only, no duplicates.
SELECT
    id, scanned_at, token_address, pair_address, symbol, chain,
    age_minutes, price_usd, liquidity_usd, market_cap,
    volume_5m, volume_1h, volume_6h,
    price_ch_5m, price_ch_1h, price_ch_6h,
    buys_1h, sells_1h, buys_5m, sells_5m,
    vl_ratio, vol_trend, vol_trend_pct, micro_trend,
    buy_pct_5m, buy_pct_1h
FROM raw_signals
WHERE id > %s              -- watermark
  AND chain = 'base'       -- Base-first; Solana deferred
  AND scanned_at > NOW() - INTERVAL '10 minutes'  -- safety: ignore very stale rows
ORDER BY id ASC
LIMIT 500;                 -- cap per cycle
```

After processing, update watermark to `MAX(id)` of returned rows.

### 3.3 Hard filters — scanner replica in `signals.py`

The trader must replicate the scanner's hard pre-filters exactly. This is the source of truth for which tokens are in-scope. The filter logic lives in `signals.py` and is documented inline with a reference to the scanner source so it's easy to sync:

```python
# KEEP IN SYNC WITH: n8n workflow "Safety Filter" node (dex-scanner-workflow.json)
# Filter version: Phase 9 (2026-05-23)

BASE_VL_CEILING    = 8.0    # Base V/L ratio ceiling
SOLANA_VL_CEILING  = 4.0    # Solana V/L ratio ceiling (not used in shadow; kept for Solana phase)
AGE_MIN_MINUTES    = 15
AGE_MAX_MINUTES    = 90

# micro_trend values that cause exclusion, by chain
EXCLUDED_MICRO = {
    "base":   {"down", "recovering"},          # Phase 9
    "solana": {"down", "recovering", "flat"},  # Phase 9
}

def hard_filter(signal: dict) -> tuple[bool, str]:
    """
    Returns (passes, reason). reason is empty string if passes=True.
    """
    age  = signal.get("age_minutes") or 0
    vl   = signal.get("vl_ratio")    or 0
    micro= signal.get("micro_trend") or ""
    chain= signal.get("chain")       or ""

    if not (AGE_MIN_MINUTES <= age <= AGE_MAX_MINUTES):
        return False, f"age_out_of_window:{age:.0f}m"

    vl_ceil = BASE_VL_CEILING if chain == "base" else SOLANA_VL_CEILING
    if vl > vl_ceil:
        return False, f"vl_too_high:{vl:.1f}>{vl_ceil}"

    excluded = EXCLUDED_MICRO.get(chain, set())
    if micro in excluded:
        return False, f"micro_excluded:{micro}"

    return True, ""
```

**When scanner filters evolve:** update the constants/sets here and commit. The inline comment names the workflow node and phase so there's an audit trail.

---

## 4. Model Serving

### 4.1 Artifact location

The model files live in `analysis/models/` on the host and are bind-mounted read-only into the trader container at `/models/`:

```
analysis/models/           (host, gitignored)
  lgbm_base.txt            LightGBM native booster (model.booster_.save_model())
  feature_list.json        ["chain","dex","age_minutes",...] — ordered
  metadata.json            {"threshold":0.70,"train_cutoff":"2026-05-23","val_auc":0.6208,"trained_at":"..."}
                           -- threshold is the ML-FINDINGS.md canonical (0.70).
                           -- Scorer uses env vars (CONVICTION_THRESHOLD_SHADOW / LIVE) to override at runtime.
```

The gitignore entry for `/analysis/models/` prevents model blobs from being committed. `analysis/models/.gitkeep` keeps the directory tracked.

### 4.2 Loading in `scorer.py`

```python
MODEL_PATH    = os.environ.get("MODEL_PATH", "/models/lgbm_base.txt")
FEATURES_PATH = os.environ.get("FEATURES_PATH", "/models/feature_list.json")
METADATA_PATH = os.environ.get("METADATA_PATH", "/models/metadata.json")
RELOAD_CHECK_INTERVAL = 300  # seconds between mtime checks

class Scorer:
    def load(self):
        self.booster   = lgb.Booster(model_file=MODEL_PATH)
        self.features  = json.load(open(FEATURES_PATH))
        self.meta      = json.load(open(METADATA_PATH))
        self.threshold = self.meta["threshold"]
        self.mtime     = os.path.getmtime(MODEL_PATH)
        self.last_checked = time.monotonic()
        log.info("model loaded: version=%s auc=%.3f threshold=%.2f",
                 self.meta["trained_at"], self.meta["val_auc"], self.threshold)

    def maybe_reload(self):
        if time.monotonic() - self.last_checked < RELOAD_CHECK_INTERVAL:
            return
        self.last_checked = time.monotonic()
        try:
            new_mtime = os.path.getmtime(MODEL_PATH)
            if new_mtime != self.mtime:
                log.info("model file changed, reloading…")
                self.load()
        except FileNotFoundError:
            log.warning("model file not found at %s — keeping current", MODEL_PATH)

    def score(self, signal: dict) -> float:
        X = pd.DataFrame([signal])[self.features]
        for col in ["chain", "dex", "micro_trend", "vol_trend"]:
            if col in X.columns:
                X[col] = X[col].astype("category")
        return float(self.booster.predict(X)[0])
```

**Hot-reload:** `maybe_reload()` checks mtime every 5 minutes. When `export_model.py` is run on the host (writing to `analysis/models/`), the bind-mounted file is immediately visible in the container. Within 5 minutes, the trader picks up the new booster. No restart needed.

### 4.3 Note on model sizing and Kelly fractions (do not implement now)

The current model outputs `p(win)` from a binary classifier trained on `outcome_pct > 0`. This is suitable for the uniform $10 bet size in shadow mode. It is **not** suitable for Kelly-fraction position sizing, which requires both `p(win)` and `E[return | win]` (expected magnitude of winning trades, not just direction). The current model does not predict magnitude.

The migration path to Kelly-optimal sizing is: (1) train a second model as a magnitude regression on `outcome_pct | outcome_pct > 0`, (2) combine with the classifier's `p(win)` to compute fractional Kelly `f = (bp - q) / b` where `b = E[return | win]` and `q = 1 - p(win)`, (3) cap at 25% full Kelly as standard practice. This is a Phase 5+ improvement. For shadow mode and initial live mode, uniform $10 is correct.

---

## 5. Aggregator Integration

### 5.1 Unified `Quote` dataclass

```python
@dataclass
class Quote:
    source:          str              # '0x' | 'aerodrome' | 'uniswap_v3' | 'jupiter'
    price_usd:       float            # effective fill price
    slippage_bps:    int              # price impact in basis points
    gas_usd:         float            # estimated gas cost in USD
    route_summary:   str              # human-readable: "Aerodrome (volatile)"
    raw_response:    dict             # full API response for debugging
    latency_ms:      int
```

### 5.2 Dispatcher — `aggregators/__init__.py`

```python
def get_quote(token_address: str, chain: str, sell_usd: float,
              w3=None, taker_address=None) -> Optional[Quote]:
    """
    Try aggregators in priority order. Return first successful Quote or None.
    Never raises.
    """
    if chain == "base":
        quote = zerox.quote(token_address, sell_usd, taker_address)
        if quote: return quote
        quote = aerodrome.quote(token_address, sell_usd, w3)
        if quote: return quote
        quote = uniswap.quote(token_address, sell_usd, w3)
        if quote: return quote
        log.warning("no quote available for %s on %s", token_address[:8], chain)
        return None
    elif chain == "solana":
        raise NotImplementedError("Solana trading deferred")
    return None
```

### 5.3 `zerox.py`

- Endpoint: `GET https://api.0x.org/swap/permit2/quote`
- Headers: `0x-api-key: {ZEROX_API_KEY}`, `0x-version: v2`
- Params: `sellToken=USDC`, `buyToken={address}`, `sellAmount={amount_in_wei}`, `taker={taker}`, `chainId=8453`, `slippageBps=200`
- USDC on Base: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (6 decimals)
- Sell amount: `int(sell_usd * 1e6)` USDC
- On `liquidityAvailable: false` → return `None` immediately (no error, no log spam)
- Slippage bps: extracted as `round((price - guaranteedPrice) / price * 10000)` or `slippageBps` param echo
- Gas USD: `fees.gasFee.amount` if present, else `estimatedGas * gasPrice / 1e18 * eth_usd_price` (stale estimate)
- Timeout: 3s

### 5.4 `aerodrome.py`

- Contract: `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43` (Aerodrome Router)
- WETH on Base: `0x4200000000000000000000000000000000000006`
- USDC on Base: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- Two-hop route: USDC → WETH → token (for tokens without direct USDC pool)
- Single-hop route: USDC → token (try first)
- Call: `router.functions.getAmountsOut(amount_in, routes).call()`
- Pool is always volatile (`stable=False`) for new meme tokens
- Factory: `0x420DD381b31aEf6683db6B902084cB0FFECe40D`
- Slippage: compute as `(ideal_price - quoted_price) / ideal_price * 10000` using DexScreener price as ideal
- Gas: flat estimate 150,000 gas × current base fee (fetched from `w3.eth.gas_price`)
- On revert or empty amounts → return `None`
- Timeout: 5s (on-chain call, slower than HTTP)

### 5.5 `uniswap.py`

- Contract: `0x3d4e44Eb1374240CE5F1B136Cf395a8eae0e6953` (QuoterV2)
- Try fee tiers in order: `10000` (1%), `3000` (0.3%), `500` (0.05%)
- Call: `quoter.functions.quoteExactInputSingle((tokenIn, tokenOut, amountIn, fee, 0)).call()`
- Returns `(amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)`
- Use first fee tier that returns non-zero `amountOut`
- Gas: `gasEstimate` field from QuoterV2 response
- On all fee tiers returning 0 → return `None`

### 5.6 Quote validation before recording intent

After getting a quote, apply two drift checks:

```python
QUOTE_DRIFT_MAX_PCT  = 3.0   # reject if aggregator price > 3% above DexScreener signal price
SLIPPAGE_REJECT_BPS  = 500   # reject if expected slippage > 5% (thin book)

entry_cost = (quote.price_usd - signal_price) / signal_price * 100
if entry_cost > QUOTE_DRIFT_MAX_PCT:
    # signal is too stale; price has moved too much
    record_skipped(reason=f"quote_drift:{entry_cost:.1f}%")
    return

if quote.slippage_bps > SLIPPAGE_REJECT_BPS:
    record_skipped(reason=f"slippage_too_high:{quote.slippage_bps}bps")
    return
```

Both thresholds are env-var configurable (`QUOTE_DRIFT_MAX_PCT`, `SLIPPAGE_REJECT_BPS`).

---

## 6. Wallet / Key Management

### 6.1 Decision: `eth-account` + `web3.py` with `PRIVATE_KEY` env var ✅

**Rationale:**
- **CDP SDK:** Adds a hard dependency on Coinbase's infrastructure. If Coinbase has an outage, the trader stops. Also adds significant API surface complexity (managed MPC wallet involves key shards, multiple round trips). Not worth it for a $200 wallet.
- **Privy/Turnkey:** SaaS key custody. Good for consumer apps, adds recurring cost and external auth dependency. Overkill for a personal bot.
- **`eth-account` + `web3.py`:** Standard library, no third-party key custody, full local control, straightforward rotation (update `.env`, restart container), works with any RPC. Used by 95% of DeFi bots. The right choice at this scale.

**Key in `.env`:**
```
TRADER_WALLET_PRIVATE_KEY=0x...   # Base wallet — keep this out of git
ALCHEMY_BASE_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
ZEROX_API_KEY=...
```

**In shadow mode, `TRADER_WALLET_PRIVATE_KEY` is never read.** The code path signs transactions only when `SHADOW_MODE=false`. The wallet address is still derived (for 0x `taker` param and for logging) but no signing occurs.

```python
SHADOW_MODE = os.environ.get("SHADOW_MODE", "true").lower() == "true"

if not SHADOW_MODE:
    from eth_account import Account
    wallet = Account.from_key(os.environ["TRADER_WALLET_PRIVATE_KEY"])
    taker_address = wallet.address
else:
    taker_address = "0x0000000000000000000000000000000000000001"  # sentinel
```

**For Solana (deferred to Phase 5+):** `solders` keypair from a JSON file (`SOLANA_KEYPAIR_PATH`). Same principle — keypair file is loaded in live mode only; shadow mode uses a sentinel pubkey.

### 6.2 RPC configuration

```python
ALCHEMY_BASE_URL = os.environ.get("ALCHEMY_BASE_URL", "")
BASE_PUBLIC_RPC  = "https://mainnet.base.org"

# Use Alchemy if configured, fall back to public
rpc_url = ALCHEMY_BASE_URL or BASE_PUBLIC_RPC
w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
```

Public Base RPC is the documented fallback. Log a warning at startup if Alchemy URL is not set.

---

## 7. Simulated Fill Model

### 7.1 Entry fill

In shadow mode, the entry fill price equals the aggregator quote price. No AMM slippage formula is applied beyond what the aggregator already reports — the aggregator's `priceImpactPct` / `slippage_bps` is the realistic slippage estimate.

```python
fill_price_usd = quote.price_usd      # shadow mode: quote is the fill
fill_size_usd  = TRADE_SIZE_USD       # flat $10 default
fill_ts        = datetime.now(utc)    # T₀ — exit timer starts here
entry_cost_pct = (fill_price_usd - signal_price_usd) / signal_price_usd * 100
```

### 7.2 Exit fill — decision: real aggregator quote at exit time ✅

**At T = fill_ts + POSITION_HOLD_SECONDS (default 300):**

1. Request an aggregator exit quote (sell token → USDC). Same fallback chain: 0x → Aerodrome → Uniswap V3.
2. Simultaneously poll DexScreener for the current price (`fetch_current_price(chain, pair_address)`).
3. Record both. Compute P&L using `exit_quote_usd` as the canonical exit price.

```python
exit_quote = get_quote(token_address, chain, sell_token_amount, direction="sell")
exit_dexscreen = api.fetch_current_price(chain, pair_address)

# P&L uses aggregator quote — most realistic simulation of live mode
gross_pct = (exit_quote.price_usd - fill_price_usd) / fill_price_usd * 100

# Backtest comparison uses DexScreener (what backtest.py measured)
backtest_net = (exit_dexscreen - signal_price_usd) / signal_price_usd * 100 - 1.5
```

**Rationale:** This decision ensures the shadow P&L is computed using actual aggregator quotes on both legs — the same path that live mode would use. DexScreener price is stored as a parallel ground truth, enabling direct comparison with the collector's `outcome_pct` and backtest assumptions.

### 7.3 Exit timer

**The exit timer starts at `fill_ts`, not `signal_ts`.**

```python
expected_exit = fill_ts + timedelta(seconds=POSITION_HOLD_SECONDS)  # default 300

# In manage_open_positions():
if datetime.now(utc) >= expected_exit:
    trigger_exit(trade_id, trigger="timer")
```

This is correct because the 5-minute hold represents the time the position is "open," not the age of the signal. A signal could be 4 minutes old when we pick it up; we still hold for 5 minutes from our simulated entry.

---

## 8. Position Lifecycle State Machine

### 8.1 States

```
┌──────────┐
│  intent  │  Row created; signal passed filters + score threshold + risk gates
└────┬─────┘
     │ security check + quote request
     ▼
┌──────────┐
│  quoted  │  Quote obtained; drift/slippage validation passed
└────┬─────┘
     │ write fill (shadow: immediate)
     ▼
┌────────────┐
│ simulated  │  Fill recorded; exit timer running (fill_ts + 300s)
└────┬───────┘
     │ (future: live mode would add "managed" between here and exit;
     │  in shadow, simulated → exited directly when timer fires)
     ▼
┌─────────┐
│ exited  │  Exit quote + DexScreener price obtained; P&L computed; row finalized
└─────────┘

From any state:
  → failed   Unrecoverable error (quote module crashed, DB write failed repeatedly)
  → skipped  Risk gate blocked entry before fill (position limit, daily loss, kill switch,
              re-entry lock, security fail, drift rejection, slippage rejection)
```

**`managed` state** is reserved for live mode, where a real position is open and being monitored. In shadow mode, `simulated` and `managed` are collapsed — there is no real position to manage.

### 8.2 Transitions in code

| Event | From | To | DB action |
|---|---|---|---|
| Signal passes filters + score | — | `intent` | INSERT trades row |
| Security + quote success | `intent` | `quoted` | UPDATE quote_* columns |
| Fill recorded | `quoted` | `simulated` | UPDATE fill_* columns, fill_ts |
| Exit timer fires | `simulated` | `exited` | UPDATE exit_* + P&L columns |
| Any error | any | `failed` | UPDATE status, failure_reason |
| Risk/drift/slippage rejection | `intent` | `skipped` | UPDATE status, failure_reason |

### 8.3 Crash recovery

On startup, `risk.load_state()`:
1. Query all open trades (status IN ('intent','quoted','simulated','managed'))
2. For each, reconstruct in-memory position state
3. Check if any have `fill_ts` + 300s in the past → immediately trigger exit
4. Read `signal_watermark` table → set current `last_id`

---

## 9. Risk Controls

All limits are env-var configurable. Conservative defaults for shadow mode.

| Control | Default | Env var | Rationale |
|---|---|---|---|
| Max simultaneous positions | 3 | `MAX_POSITIONS` | $30 peak exposure on $200–300 wallet |
| Trade size | $10 | `TRADE_SIZE_USD` | Matches backtest assumption |
| Daily loss cap | $50 | `DAILY_LOSS_CAP_USD` | 25% of min wallet size |
| Hourly trade rate limit | 20 | `MAX_TRADES_PER_HOUR` | Prevent runaway on data anomaly |
| Re-entry lockout | 30min | `REENTRY_LOCKOUT_MINUTES` | Matches ROADMAP constraint |
| Conviction threshold — shadow floor | 0.65 | `CONVICTION_THRESHOLD_SHADOW` | Shadow mode entry; accumulates data faster for ≥200-trade gate |
| Conviction threshold — live canonical | 0.70 | `CONVICTION_THRESHOLD_LIVE` | ML-FINDINGS.md optimum (55% win, 2.50x PF); used in live mode only |
| Quote drift rejection | 3% | `QUOTE_DRIFT_MAX_PCT` | Signal too stale |
| Slippage rejection | 500 bps | `SLIPPAGE_REJECT_BPS` | Book too thin |
| Kill switch | false | DB (`trader_state`) | SQL toggle, polled each cycle |
| Position hold | 300s | `POSITION_HOLD_SECONDS` | 5-min window |

**Two-threshold logic:** In shadow mode, `SHADOW_MODE=true` activates using `CONVICTION_THRESHOLD_SHADOW` as the entry floor (0.65). All trades in the 0.65–0.70 range are tagged `conviction_band='shadow_only'`; trades at ≥0.70 are tagged `conviction_band='live_eligible'`. This lets post-hoc queries separate the two cohorts and confirm whether the live threshold (0.70) still meets the ML-FINDINGS targets before the shadow→live transition. The `conviction_band` column is set on every inserted `trades` row.

```python
threshold = float(os.environ.get(
    "CONVICTION_THRESHOLD_SHADOW" if SHADOW_MODE else "CONVICTION_THRESHOLD_LIVE",
    "0.65" if SHADOW_MODE else "0.70"
))
live_threshold = float(os.environ.get("CONVICTION_THRESHOLD_LIVE", "0.70"))

# At INSERT time:
conviction_band = "live_eligible" if score >= live_threshold else "shadow_only"
```

### 9.1 Kill switch — DB flag, polled each cycle

```sql
-- Check (every cycle):
SELECT value FROM trader_state WHERE key = 'kill_switch';

-- Arm (from host, any psql client):
UPDATE trader_state SET value='true', updated_at=NOW() WHERE key='kill_switch';

-- Disarm:
UPDATE trader_state SET value='false', updated_at=NOW() WHERE key='kill_switch';
```

When `kill_switch = 'true'`:
- No new intent rows are created
- Open positions in `simulated` state are NOT force-closed (they expire naturally via the timer)
- The main loop continues running and logs "kill switch armed, skipping entry" each cycle

This is deliberately non-destructive: arming the kill switch stops new entries but lets open positions exit cleanly on their timer.

### 9.2 Re-entry lockout

In-memory: `Dict[str, datetime]` mapping `token_address → last_fill_ts`. On startup, seed from:
```sql
SELECT token_address, MAX(fill_ts) FROM trades
WHERE fill_ts > NOW() - INTERVAL '30 minutes'
  AND status NOT IN ('skipped','failed')
GROUP BY token_address;
```

Before entering a trade, check `last_fill_ts[token_address] + 30min > now()`.

### 9.3 Daily loss cap

Computed at start of each cycle:
```sql
SELECT COALESCE(SUM(pnl_usd), 0) AS daily_pnl
FROM trades
WHERE status = 'exited'
  AND created_at >= NOW()::date;
```
If `daily_pnl <= -DAILY_LOSS_CAP_USD`, skip all new entries until tomorrow UTC.

### 9.4 Controls exercisable in shadow mode

All controls above are enforced identically in shadow and live mode. The kill switch test, daily loss cap, position limit, conviction threshold, slippage rejection — all fire in shadow mode. This is intentional: we validate the control logic has no bugs before going live.

---

## 10. Security Re-Checks

### 10.1 Decision: trader re-runs GoPlus + Honeypot.is with 1h cache ✅

**Rationale:**
- Option (b) — join against `token_signals` — only covers tokens the scanner has seen in INTERESTING/WATCH runs. The scanner isn't always running; the collector covers 24/7. This approach would miss the majority of trader candidates.
- Option (c) — trust `raw_signals` blindly — `raw_signals` has no security data at all. New tokens in the 15-90min window include honeypots. Unacceptable even in shadow mode (builds bad habits).
- Option (a) — trader re-runs checks — correct. The trader is independently running 24/7, so it cannot rely on scanner sessions. Small latency cost (300ms cached, 1s cold) is acceptable.

**GoPlus endpoint (Base):**
```
GET https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={address}
```

**Honeypot.is endpoint (Base):**
```
GET https://api.honeypot.is/v2/IsHoneypot?address={address}
```

Both are free, no API key required.

### 10.2 `security.py` design

```python
SECURITY_CACHE_TTL = 3600  # 1 hour in seconds
SECURITY_TIMEOUT   = 2     # seconds per API call
_cache: Dict[str, tuple[float, bool, str]] = {}
# cache value: (checked_at_monotonic, passed, flags_csv)

def is_safe(address: str, chain: str) -> tuple[bool, str]:
    """Returns (safe, source_tag). Never raises."""
    now = time.monotonic()
    if address in _cache:
        checked_at, passed, flags = _cache[address]
        if now - checked_at < SECURITY_CACHE_TTL:
            return passed, "cache"

    gp_safe, gp_flags = _check_goplus(address, chain)
    hp_safe, hp_flags = _check_honeypot(address) if chain == "base" else (True, "")

    passed = gp_safe and hp_safe
    flags  = ",".join(f for f in [gp_flags, hp_flags] if f)
    _cache[address] = (now, passed, flags)
    return passed, "goplus+honeypot"
```

**GoPlus failure flags checked:**
- `is_honeypot == "1"`
- `buy_tax > 10` (%) or `sell_tax > 10`
- `is_blacklisted == "1"`
- `cannot_sell_all == "1"`

**Honeypot.is failure flag:**
- `IsHoneypot == true`

**Fail behavior in shadow mode:** Fail open (log warning, allow trade). The security API is a best-effort check; if GoPlus is down, shadow mode proceeds. In live mode, flip to fail safe (skip trade if API unreachable). The behavior is controlled by `SECURITY_FAIL_OPEN=true` (default in shadow mode).

---

## 11. Observability

### 11.1 Structured log format

Same as collector: `%(asctime)s %(levelname)s %(message)s`. Key log lines:

```
INFO  ingest: 12 new signals | watermark=188432
INFO  signal PEPE base | score=0.71 age=34m liq=$45k → entry check
INFO  security PEPE: goplus+honeypot → safe
INFO  quote PEPE: 0x | price=$0.000142 slippage=89bps gas=$0.08 latency=312ms
INFO  intent PEPE: trade_id=42 fill_price=$0.000142 entry_cost=+0.4%
INFO  exit PEPE: trade_id=42 timer | exit_quote=$0.000148 exit_dex=$0.000149 gross=+4.2% cost=0.7% net=+3.5% pnl=+$0.35
WARN  signal RUGTOKEN base | score=0.68 → security FAIL: is_honeypot
WARN  signal THINTOKEN base | score=0.70 → quote NONE (no liquidity anywhere)
INFO  risk: kill_switch=false positions=1/3 daily_pnl=-$2.50 trades_1h=4
```

### 11.2 Key metrics for Phase 4 reviews

**T+1 hour smoke test:**
```sql
SELECT status, COUNT(*), MIN(created_at), MAX(created_at)
FROM trades GROUP BY status ORDER BY COUNT(*) DESC;
```

**T+24 hours — first read:**
```sql
-- Signal pipeline health
SELECT
  COUNT(*)                        AS signals_processed,
  COUNT(*) FILTER (WHERE conviction_score >= 0.65) AS above_shadow_threshold,
  COUNT(*) FILTER (WHERE conviction_score >= 0.70) AS above_live_threshold,
  COUNT(*) FILTER (WHERE status = 'simulated')     AS simulated_fills,
  COUNT(*) FILTER (WHERE status = 'skipped')       AS skipped,
  COUNT(*) FILTER (WHERE status = 'exited')        AS completed
FROM trades WHERE created_at > NOW() - INTERVAL '24 hours';

-- Conviction band breakdown (shadow_only vs live_eligible)
SELECT conviction_band, COUNT(*), ROUND(AVG(net_pct)::numeric,2) AS avg_net_pct
FROM trades WHERE status = 'exited'
GROUP BY conviction_band ORDER BY conviction_band;

-- Quote coverage
SELECT quote_source, COUNT(*), AVG(quote_latency_ms), AVG(quote_slippage_bps)
FROM trades WHERE status NOT IN ('intent','skipped','failed')
GROUP BY quote_source ORDER BY COUNT(*) DESC;

-- Entry latency cost
SELECT
  AVG(entry_cost_pct)  AS avg_entry_cost,
  MAX(entry_cost_pct)  AS max_entry_cost,
  AVG(quote_latency_ms) AS avg_quote_ms
FROM trades WHERE fill_ts IS NOT NULL;

-- Security check outcomes
SELECT security_passed, security_source, COUNT(*)
FROM trades GROUP BY 1, 2;
```

**T+7 days — edge survival:**
```sql
-- Primary edge check: does shadow match backtest?
SELECT
  COUNT(*)                                AS n_trades,
  ROUND(AVG(gross_pct)::numeric, 2)       AS avg_gross_pct,
  ROUND(AVG(cost_pct)::numeric, 2)        AS avg_real_cost_pct,
  ROUND(AVG(cost_delta_pct)::numeric, 2)  AS avg_cost_delta,   -- real - 1.5%
  ROUND(AVG(net_pct)::numeric, 2)         AS avg_net_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE net_pct > 0) / COUNT(*), 1) AS win_pct,
  ROUND(
    SUM(pnl_usd) FILTER (WHERE pnl_usd > 0) /
    NULLIF(ABS(SUM(pnl_usd) FILTER (WHERE pnl_usd < 0)), 0), 2
  ) AS profit_factor
FROM trades
WHERE status = 'exited'
  AND created_at > NOW() - INTERVAL '7 days';

-- Backtest comparison
SELECT
  ROUND(AVG(backtest_net_pct)::numeric, 2)  AS backtest_assumed_avg,
  ROUND(AVG(net_pct)::numeric, 2)           AS shadow_actual_avg,
  ROUND(AVG(net_pct - backtest_net_pct)::numeric, 2) AS gap
FROM trades WHERE status = 'exited';
```

---

## 12. 0x Coverage Estimate — Base Tokens (Appendix C)

The following is empirical data from 7 days of collector data answering: *how often will 0x return `liquidityAvailable=false`, and how often will the Aerodrome fallback fire?*

**Note:** 0x's `liquidityAvailable` threshold is not public, but is estimated ~$5k–25k USD liquidity depending on token age and DEX routing. This analysis uses liquidity buckets as a proxy.

**Base filter pass rates (last 7 days):**
- Total Base rows: **6,971**
- In age window (15–90 min): **3,085** (44%)
- Pass all hard filters: **2,567** (83% of in-window)
- Unique tokens in window: **244**

**Liquidity distribution of trader candidates (tokens passing all filters):**

| Liquidity | Count | % of Candidates | 0x Likely? | Est. Aerodrome need |
|---|---|---|---|---|
| < $5k | 18 | 0.7% | ❌ Very unlikely | Yes |
| $5k–$10k | 95 | 3.7% | ❌ Unlikely | Yes |
| $10k–$25k | 433 | 16.9% | ⚠️ Uncertain (50/50) | ~Half |
| $25k–$50k | 515 | 20.1% | ✅ Likely | Occasionally |
| $50k–$100k | 865 | 33.7% | ✅ Probable | Rarely |
| > $100k | 641 | 25.0% | ✅ Reliable | Almost never |

**Estimated Aerodrome fallback fire rate:** ~10–25% of trade candidates, concentrated in the $5k–$25k bucket. This is frequent enough that the fallback is not a niche case — it will fire multiple times per day.

**Recommendation:** After 48 hours of shadow running, query `SELECT quote_source, COUNT(*) FROM trades GROUP BY 1` to get the real empirical rate. This will replace the estimate above and inform whether Aerodrome reliability needs attention before live mode.

**One-off measurement script** (run from host, referenced by design — not yet built):
```bash
# analysis/check_0x_coverage.py — to be written in Phase 3
# Queries last 7 days of collector Base candidates, calls 0x for each unique token,
# buckets results by liquidity_usd, prints liquidityAvailable true/false rate per bucket.
```

---

## 13. Compose Changes

```yaml
# Add to compose.yaml — Trader group

  dex-trader-db:
    image: timescale/timescaledb:latest-pg16
    container_name: dex-trader-db
    restart: unless-stopped
    networks:
      - dex-trader-net
    ports:
      - "5435:5432"
    environment:
      - POSTGRES_DB=trader
      - POSTGRES_USER=trader
      - POSTGRES_PASSWORD=trader
    volumes:
      - source: ./trader_data
        target: /var/lib/postgresql/data
        type: bind
      - source: ./dex-trader/init.sql
        target: /docker-entrypoint-initdb.d/init.sql
        type: bind
        read_only: true

  dex-trader:
    build:
      context: ./dex-trader
    container_name: dex-trader
    restart: unless-stopped
    networks:
      - dex-trader-net       # own DB
      - dex-collector-net    # read raw_signals from collector DB
    depends_on:
      - dex-trader-db
      - dex-collector-db
    env_file:
      - .env
    environment:
      - SHADOW_MODE=true
      - TRADER_DB_HOST=dex-trader-db
      - TRADER_DB_PORT=5432
      - TRADER_DB_NAME=trader
      - TRADER_DB_USER=trader
      - TRADER_DB_PASS=trader
      - COLLECTOR_DB_HOST=dex-collector-db
      - COLLECTOR_DB_PORT=5432
      - COLLECTOR_DB_NAME=collector_signals
      - COLLECTOR_DB_USER=collector
      - COLLECTOR_DB_PASS=collector
      - MODEL_PATH=/models/lgbm_base.txt
      - FEATURES_PATH=/models/feature_list.json
      - METADATA_PATH=/models/metadata.json
      - POLL_INTERVAL=5
      - TRADE_SIZE_USD=10.0
      - MAX_POSITIONS=3
      - DAILY_LOSS_CAP_USD=50.0
      - CONVICTION_THRESHOLD_SHADOW=0.65
      - CONVICTION_THRESHOLD_LIVE=0.70
      - POSITION_HOLD_SECONDS=300
      - ETH_USD_PRICE_URL=https://api.coinbase.com/v2/prices/ETH-USD/spot
      - ETH_USD_CACHE_SECONDS=600
      - QUOTE_DRIFT_MAX_PCT=3.0
      - SLIPPAGE_REJECT_BPS=500
      - SECURITY_FAIL_OPEN=true
    volumes:
      - source: ./analysis/models
        target: /models
        type: bind
        read_only: true
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:8090/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

# Add to networks section:
  dex-trader-net:
    driver: bridge
```

**New env vars for `.env.example`:**
```bash
# Trader — Base aggregators
ZEROX_API_KEY=your_0x_api_key_here
ALCHEMY_BASE_URL=https://base-mainnet.g.alchemy.com/v2/your_key_here
JUPITER_API_KEY=your_jupiter_key_here

# Trader — wallet (shadow mode: not used for signing)
TRADER_WALLET_PRIVATE_KEY=0x_your_key_here_never_commit_this
```

---

## 14. Rollback Plan

The trader group is fully independent. Rolling it back does not touch the collector or scanner.

**Immediate stop (keep DB):**
```bash
docker compose stop dex-trader dex-trader-db
```
Collector and scanner continue unaffected.

**Arm kill switch before stopping (graceful):**
```bash
docker exec dex-trader-db psql -U trader -d trader \
  -c "UPDATE trader_state SET value='true' WHERE key='kill_switch';"
sleep 30  # allow open positions to expire naturally
docker compose stop dex-trader dex-trader-db
```

**Full removal (destroy DB):**
```bash
docker compose stop dex-trader dex-trader-db
docker compose rm -f dex-trader dex-trader-db
rm -rf ./trader_data
# Remove dex-trader-db and dex-trader from compose.yaml
```

**No compose.yaml changes are needed to stop the trader.** `docker compose stop dex-trader` is sufficient. The other groups (scanner, collector) have no `depends_on` the trader and are unaffected.

---

## 15. Open Questions

No blocking open questions remain for Phase 3. The following are logged for awareness:

| # | Topic | Status |
|---|---|---|
| OQ1 | GoPlus API rate limits not confirmed | Non-blocking. Free tier is documented as "no limit" for token security. If throttled, the 1h cache means cold calls are rare. Revisit if throttle errors appear. |
| OQ2 | 0x `taker` address requirement in shadow mode | **Confirmed.** API key verified — 0x returns HTTP 400 with `"User address must be greater than 0x000000000000000000000000000000000000ffff"` when sentinel `0x000...001` is used. Auth is valid; this is a param validation rejection. In shadow mode: sentinel is intentional, `issues.balance` warnings are expected and should be silently ignored. Empirically confirmed in prereq verification pass (2026-05-25). |
| OQ3 | Aerodrome pool discovery | Design assumes volatile pool for all new tokens. Some tokens may have stable pools or no pool at all. The `getAmountsOut` revert on no pool is handled by catching the exception and returning `None`. |
| OQ4 | Gas price for cost_pct calculation | **Resolved.** Use Coinbase public API: `https://api.coinbase.com/v2/prices/ETH-USD/spot` (free, no key, 600s cache, $3000 hardcoded fallback). Implemented as `eth_price.py` module. Env vars: `ETH_USD_PRICE_URL`, `ETH_USD_CACHE_SECONDS=600`. |
| OQ5 | `analysis/check_0x_coverage.py` script | **Deferred to Phase 4.** Run manually after 48h of shadow data accumulates. §12 estimate (10–25% Aerodrome fallback rate) is sufficient for Phase 3; real empirical rate from `SELECT quote_source, COUNT(*) FROM trades GROUP BY 1` replaces it in Phase 4 pre-work. |

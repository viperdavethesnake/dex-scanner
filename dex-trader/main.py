"""DEX Trader — shadow mode main loop.

Single-process, single-thread, single while-True loop (same pattern as collector).
Polls raw_signals from dex-collector-db every POLL_INTERVAL seconds.
Writes shadow trade records to dex-trader-db.
Health endpoint: http://0.0.0.0:8090/health

Shadow mode (SHADOW_MODE=true, default):
  - No real transactions submitted
  - taker_address = ephemeral Account.create() address (new per process start)
  - TRADER_WALLET_PRIVATE_KEY never read
  - Security checks run (fail-open on API error)
  - All risk controls enforced (validates control logic before go-live)
"""
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import psycopg2
import psycopg2.extras
from eth_account import Account
from web3 import Web3

import db
import risk
import signals as sig
from aggregators import get_quote
from features import engineer_features
from scorer import Scorer
from security import is_safe
from simulator import compute_entry, compute_exit

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

utc = timezone.utc

SHADOW_MODE           = os.environ.get("SHADOW_MODE", "true").lower() == "true"
POLL_INTERVAL         = int(os.environ.get("POLL_INTERVAL", "5"))
TRADE_SIZE_USD        = float(os.environ.get("TRADE_SIZE_USD", "10.0"))
POSITION_HOLD_SECONDS = int(os.environ.get("POSITION_HOLD_SECONDS", "300"))
# Asymmetric drift gates — strategy is momentum scalping, not value entry.
# See docs/decisions/DRIFT-GATE-V2.md
QUOTE_DRIFT_DOWN_MAX_PCT = float(os.environ.get("QUOTE_DRIFT_DOWN_MAX_PCT", "1.5"))
QUOTE_DRIFT_UP_MAX_PCT   = float(os.environ.get("QUOTE_DRIFT_UP_MAX_PCT",   "15.0"))

# Exit cost estimates used when aggregator returns None (no liquidity).
# Without these, gross_pct ≈ net_pct and P&L is overstated.
FALLBACK_EXIT_GAS_USD       = float(os.environ.get("FALLBACK_EXIT_GAS_USD",       "0.20"))
FALLBACK_EXIT_SLIPPAGE_BPS  = int  (os.environ.get("FALLBACK_EXIT_SLIPPAGE_BPS",  "300"))

SLIPPAGE_REJECT_BPS   = int(os.environ.get("SLIPPAGE_REJECT_BPS", "500"))
HEALTH_PORT           = int(os.environ.get("HEALTH_PORT", "8090"))

CONVICTION_THRESHOLD_SHADOW = float(os.environ.get("CONVICTION_THRESHOLD_SHADOW", "0.65"))
CONVICTION_THRESHOLD_LIVE   = float(os.environ.get("CONVICTION_THRESHOLD_LIVE",   "0.70"))
THRESHOLD = CONVICTION_THRESHOLD_SHADOW if SHADOW_MODE else CONVICTION_THRESHOLD_LIVE

ALCHEMY_BASE_URL = os.environ.get("ALCHEMY_BASE_URL", "")
BASE_PUBLIC_RPC  = "https://mainnet.base.org"

# ── Taker address ──────────────────────────────────────────────────────────────

if not SHADOW_MODE:
    _wallet       = Account.from_key(os.environ["TRADER_WALLET_PRIVATE_KEY"])
    taker_address = _wallet.address
    log.info("live mode: taker_address=%s", taker_address)
else:
    # Ephemeral keypair — address only, key never persisted or used for signing.
    # 0x rejects addresses <= 0x000000000000000000000000000000000000ffff (HTTP 400).
    _ephemeral    = Account.create()
    taker_address = _ephemeral.address
    log.info("shadow mode: ephemeral taker_address=%s", taker_address)

# ── Health endpoint ─────────────────────────────────────────────────────────────

_health = {
    "open_positions": 0,
    "last_signal_ts": None,
    "model_version":  "not_loaded",
}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({
            "status":         "ok",
            "open_positions": _health["open_positions"],
            "last_signal_ts": _health["last_signal_ts"],
            "model_version":  _health["model_version"],
            "shadow_mode":    SHADOW_MODE,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence HTTP access logs


def _start_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("health: http://0.0.0.0:%d/health", HEALTH_PORT)


# ── DexScreener price fetch ────────────────────────────────────────────────────

import requests as _req


def _fetch_dexscreener_price(chain: str, pair_address: str) -> Optional[float]:
    """Best-effort DexScreener price at exit time (parallel truth)."""
    chain_id = "base" if chain == "base" else "solana"
    try:
        url  = f"https://api.dexscreener.com/latest/dex/pairs/{chain_id}/{pair_address}"
        resp = _req.get(url, timeout=5)
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if pairs:
            return float(pairs[0]["priceUsd"])
    except Exception as exc:
        log.warning("dexscreener fetch failed for %s: %s", pair_address, exc)
    return None


# ── DB write helpers ───────────────────────────────────────────────────────────

def _insert_intent(conn, signal: dict, score: float, band: str, model_ver: str,
                   signal_features: dict = None) -> int:
    sf = psycopg2.extras.Json(signal_features) if signal_features else None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (
                chain, token_address, pair_address, symbol,
                signal_ts, signal_price_usd,
                conviction_score, conviction_band, model_version,
                signal_features,
                collector_signal_id, status
            ) VALUES (%s,%s,%s,%s, %s,%s, %s,%s,%s, %s, %s, 'intent')
            RETURNING id
        """, (
            signal.get("chain"),
            signal.get("token_address"),
            signal.get("pair_address"),
            signal.get("symbol"),
            signal.get("scanned_at"),
            signal.get("price_usd"),
            score, band, model_ver,
            sf,
            signal.get("id"),
        ))
        trade_id = cur.fetchone()[0]
    conn.commit()
    return trade_id


def _update_security(conn, trade_id: int, passed: bool, source: str, flags: str):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE trades SET
                security_checked=TRUE,
                security_passed=%s,
                security_source=%s,
                security_flags=%s
            WHERE id=%s
        """, (passed, source, flags or None, trade_id))
    conn.commit()


def _update_skipped(conn, trade_id: int, reason: str):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trades SET status='skipped', failure_reason=%s WHERE id=%s",
            (reason, trade_id),
        )
    conn.commit()


def _update_simulated(conn, trade_id: int, q, fill: dict):
    """Advance trade to 'simulated' — quote + fill recorded in one write."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE trades SET
                status='simulated',
                quote_ts=NOW(),
                quote_source=%s, quote_price_usd=%s,
                quote_slippage_bps=%s, quote_gas_usd=%s, quote_latency_ms=%s,
                fill_ts=%s, fill_size_usd=%s, fill_price_usd=%s, entry_cost_pct=%s
            WHERE id=%s
        """, (
            q.source, q.price_usd,
            q.slippage_bps, q.gas_usd, q.latency_ms,
            fill["fill_ts"], fill["fill_size_usd"],
            fill["fill_price_usd"], fill["entry_cost_pct"],
            trade_id,
        ))
    conn.commit()


def _update_exited(conn, trade_id: int, exit_q_price: float, exit_q_source: str,
                   exit_q_latency: int, exit_dex_price: float, pnl: dict, trigger: str):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE trades SET
                status='exited',
                exit_ts=NOW(), exit_trigger=%s,
                exit_quote_usd=%s, exit_quote_source=%s, exit_quote_latency_ms=%s,
                exit_price_usd=%s,
                gross_pct=%s, cost_pct=%s, net_pct=%s, pnl_usd=%s,
                backtest_net_pct=%s, cost_delta_pct=%s
            WHERE id=%s
        """, (
            trigger,
            exit_q_price, exit_q_source, exit_q_latency,
            exit_dex_price,
            pnl.get("gross_pct"), pnl.get("cost_pct"),
            pnl.get("net_pct"),   pnl.get("pnl_usd"),
            pnl.get("backtest_net_pct"), pnl.get("cost_delta_pct"),
            trade_id,
        ))
    conn.commit()


def _update_failed(conn, trade_id: int, reason: str):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trades SET status='failed', failure_reason=%s WHERE id=%s",
            (reason, trade_id),
        )
    conn.commit()


# ── Signal ingestion ──────────────────────────────────────────────────────────

def _ingest_signals(collector_conn, trader_conn) -> list[dict]:
    """Pull new raw_signals past watermark. Returns list of signal dicts."""
    with trader_conn.cursor() as cur:
        cur.execute("SELECT last_id FROM signal_watermark WHERE id=1")
        last_id = cur.fetchone()[0]

    with collector_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                id, scanned_at, token_address, pair_address, symbol, chain, dex,
                age_minutes, price_usd, liquidity_usd, market_cap,
                volume_5m, volume_1h, volume_6h,
                price_ch_5m, price_ch_1h, price_ch_6h,
                buys_1h, sells_1h, buys_5m, sells_5m,
                vl_ratio, vol_trend, vol_trend_pct, micro_trend,
                buy_pct_5m, buy_pct_1h
            FROM raw_signals
            WHERE id > %s
              AND chain = 'base'
              AND scanned_at > NOW() - INTERVAL '10 minutes'
            ORDER BY id ASC
            LIMIT 500
        """, (last_id,))
        rows = [dict(r) for r in cur.fetchall()]

    if rows:
        new_watermark = max(r["id"] for r in rows)
        with trader_conn.cursor() as cur:
            cur.execute(
                "UPDATE signal_watermark SET last_id=%s, last_ts=NOW() WHERE id=1",
                (new_watermark,),
            )
        trader_conn.commit()
        log.info("ingest: %d new signals | watermark=%d", len(rows), new_watermark)
        last_ts = rows[-1].get("scanned_at")
        _health["last_signal_ts"] = last_ts.isoformat() if last_ts else None

    return rows


# ── Signal processing ─────────────────────────────────────────────────────────

def _process_signal(signal: dict, scorer: Scorer, trader_conn, w3,
                    kill_switch_armed: bool = False) -> None:
    token = signal.get("token_address", "?")
    sym   = signal.get("symbol", "?")
    chain = signal.get("chain", "base")

    # 1. Hard filter (silent — most signals filtered here)
    passes, reason = sig.hard_filter(signal)
    if not passes:
        return

    # 2. Score
    enriched = engineer_features(signal)
    score    = scorer.score(enriched)
    if score < THRESHOLD:
        return

    # Build JSON-safe feature snapshot for JSONB storage.
    # Coerce: float NaN/Inf → None, Decimal → float, datetime → ISO str.
    # (psycopg2 returns NUMERIC as Decimal and timestamps as datetime objects.)
    signal_features: dict = {}
    for _k, _v in enriched.items():
        if isinstance(_v, (dict, list)):
            continue
        if isinstance(_v, float) and not math.isfinite(_v):
            _v = None
        elif isinstance(_v, Decimal):
            _v = float(_v)
        elif isinstance(_v, datetime):
            _v = _v.isoformat()
        signal_features[_k] = _v

    band = Scorer.conviction_band(score)
    liq  = (signal.get("liquidity_usd") or 0) / 1000
    log.info("signal %s %s | score=%.3f band=%s age=%sm liq=$%.0fk → entry check",
             sym, chain, score, band, signal.get("age_minutes", 0), liq)

    # 3. Risk gates — pass pre-read kill_switch_armed to avoid redundant DB query
    allowed, reason = risk.check_entry_allowed(token, trader_conn,
                                               kill_switch_armed=kill_switch_armed)
    if not allowed:
        log.info("risk block %s: %s", sym, reason)
        return

    # 4. Insert intent row
    trade_id = _insert_intent(
        trader_conn, signal, score, band,
        scorer.meta.get("trained_at", "unknown"),
        signal_features=signal_features,
    )

    # 5. Security check
    safe, source = is_safe(token, chain)
    flags = "" if safe else "failed"
    _update_security(trader_conn, trade_id, safe, source, flags)
    if not safe:
        log.warning("security FAIL %s trade_id=%d flags=%s", sym, trade_id, flags)
        _update_skipped(trader_conn, trade_id, f"security_fail:{flags}")
        return
    log.info("security %s: %s → safe", sym, source)

    # 6. Get quote
    signal_price = float(signal.get("price_usd") or 0)
    q = get_quote(token, chain, TRADE_SIZE_USD,
                  w3=w3, taker_address=taker_address, direction="buy",
                  signal_price_usd=signal_price)
    if q is None:
        log.warning("quote NONE %s trade_id=%d (no liquidity)", sym, trade_id)
        _update_skipped(trader_conn, trade_id, "no_quote")
        return

    log.info("quote %s: %s | price=$%.8f slippage=%dbps gas=$%.3f latency=%dms",
             sym, q.source, q.price_usd, q.slippage_bps, q.gas_usd, q.latency_ms)

    # 7. Quote validation — asymmetric drift gate for momentum strategy
    #
    # Continued positive drift between signal and quote IS the signal
    # confirming in real time. Reject only when:
    #   (a) the token has reversed >X% (momentum failed before we could enter), or
    #   (b) the token has already extended >Y% (move likely near-complete; 5-min
    #       scalp on an already-pumped token has less remaining edge).
    #
    # The previous one-sided gate (reject if drift > +3%) actively selected
    # against the model's strongest signals and accepted reversals. See
    # docs/decisions/DRIFT-GATE-V2.md for the analysis.
    if signal_price > 0 and q.price_usd > 0:
        drift_pct = (q.price_usd - signal_price) / signal_price * 100
        if drift_pct < -QUOTE_DRIFT_DOWN_MAX_PCT:
            log.warning("momentum_failed %s: drift=%+.2f%% < -%.2f%% (signal reversing)",
                        sym, drift_pct, QUOTE_DRIFT_DOWN_MAX_PCT)
            _update_skipped(trader_conn, trade_id, f"momentum_failed:{drift_pct:+.2f}%")
            return
        if drift_pct > QUOTE_DRIFT_UP_MAX_PCT:
            log.warning("drift_too_high %s: drift=%+.2f%% > +%.2f%% (over-extended)",
                        sym, drift_pct, QUOTE_DRIFT_UP_MAX_PCT)
            _update_skipped(trader_conn, trade_id, f"drift_too_high:{drift_pct:+.2f}%")
            return
        log.info("drift OK %s: %+.2f%%", sym, drift_pct)

    if q.slippage_bps > SLIPPAGE_REJECT_BPS:
        log.warning("slippage_reject %s: %dbps > %d max", sym, q.slippage_bps, SLIPPAGE_REJECT_BPS)
        _update_skipped(trader_conn, trade_id, f"slippage_too_high:{q.slippage_bps}bps")
        return

    # 8. Simulate fill
    fill = compute_entry(signal_price, q.price_usd, TRADE_SIZE_USD)
    _update_simulated(trader_conn, trade_id, q, fill)
    risk.record_entry()   # increment hourly counter only after confirmed fill

    trade = {
        "id":               trade_id,
        "token_address":    token,
        "pair_address":     signal.get("pair_address"),
        "fill_ts":          fill["fill_ts"],
        "fill_price_usd":   fill["fill_price_usd"],
        "fill_size_usd":    fill["fill_size_usd"],
        "conviction_score": score,
        "conviction_band":  band,
        "created_at":       datetime.now(utc),
        "chain":            chain,
        "signal_price_usd": signal_price,
    }
    risk.add_position(trade)
    _health["open_positions"] = len(risk.get_open_positions())

    log.info("FILL %s trade_id=%d fill=$%.8f entry_cost=%.2f%%",
             sym, trade_id, fill["fill_price_usd"], fill["entry_cost_pct"])


# ── Position management (exits) ───────────────────────────────────────────────

def _manage_open_positions(trader_conn, w3) -> None:
    now   = datetime.now(utc)
    open_ = risk.get_open_positions()

    for trade_id, trade in list(open_.items()):
        fill_ts = trade.get("fill_ts")
        if fill_ts is None:
            continue  # intent/quoted not yet filled — skip

        if fill_ts.tzinfo is None:
            fill_ts = fill_ts.replace(tzinfo=utc)
        expected_exit = fill_ts + timedelta(seconds=POSITION_HOLD_SECONDS)
        if now < expected_exit:
            continue

        token      = trade["token_address"]
        chain      = trade.get("chain", "base")
        pair_addr  = trade.get("pair_address")
        fill_price = trade.get("fill_price_usd") or 0.0
        fill_size  = trade.get("fill_size_usd") or TRADE_SIZE_USD
        sig_price  = trade.get("signal_price_usd") or fill_price

        log.info("exit timer trade_id=%d %s", trade_id, token[:12])

        # Exit aggregator quote
        exit_q = get_quote(token, chain, fill_size, w3=w3,
                           taker_address=taker_address,
                           direction="sell", fill_price_usd=fill_price)

        # DexScreener parallel truth
        exit_dex = _fetch_dexscreener_price(chain, pair_addr) or fill_price

        # Unpack exit quote (or use DexScreener fallback)
        if exit_q is not None:
            eq_price   = exit_q.price_usd
            eq_source  = exit_q.source
            eq_latency = exit_q.latency_ms
            eq_gas     = exit_q.gas_usd
            eq_slip    = exit_q.slippage_bps
        else:
            log.warning(
                "exit_quote NONE trade_id=%d — dexscreener fallback + estimated costs "
                "(gas=$%.2f, slip=%dbps)", trade_id,
                FALLBACK_EXIT_GAS_USD, FALLBACK_EXIT_SLIPPAGE_BPS,
            )
            eq_price   = exit_dex
            eq_source  = "dexscreener_fallback"
            eq_latency = 0
            # Conservative estimates so P&L isn't artificially inflated when no
            # real aggregator quote is available. Identify these trades in
            # analysis via exit_quote_source='dexscreener_fallback'.
            eq_gas     = FALLBACK_EXIT_GAS_USD
            eq_slip    = FALLBACK_EXIT_SLIPPAGE_BPS

        pnl = compute_exit(
            fill_price_usd       = fill_price,
            fill_size_usd        = fill_size,
            exit_quote_price_usd = eq_price,
            exit_dex_price_usd   = exit_dex,
            signal_price_usd     = sig_price,
            gas_usd              = eq_gas,
            slippage_bps         = eq_slip,
        )

        _update_exited(trader_conn, trade_id,
                       eq_price, eq_source, eq_latency, exit_dex, pnl, "timer")
        risk.remove_position(trade_id)
        _health["open_positions"] = len(risk.get_open_positions())

        log.info(
            "EXIT trade_id=%d %s | gross=%.2f%% cost=%.2f%% net=%.2f%% pnl=$%.3f",
            trade_id, token[:12],
            pnl.get("gross_pct", 0), pnl.get("cost_pct", 0),
            pnl.get("net_pct", 0),   pnl.get("pnl_usd", 0),
        )


# ── Web3 reconnect ────────────────────────────────────────────────────────────

_w3_fail_count      = 0
_w3_last_reconnect  = 0.0
W3_RECONNECT_AFTER_FAILS = int(os.environ.get("W3_RECONNECT_AFTER_FAILS", "3"))
W3_RECONNECT_BACKOFF_SEC = int(os.environ.get("W3_RECONNECT_BACKOFF_SEC",  "30"))


def _maybe_reconnect_w3(w3: Web3, rpc_url: str) -> Web3:
    """
    Test w3 connectivity each loop. On failure: increment counter.
    If failures >= W3_RECONNECT_AFTER_FAILS AND >= W3_RECONNECT_BACKOFF_SEC
    since last reconnect attempt: rebuild w3 and reset counter.
    Backoff prevents flap loops when Alchemy is down for extended periods.
    Returns the (possibly new) w3 instance.
    """
    global _w3_fail_count, _w3_last_reconnect
    try:
        if w3.is_connected():
            _w3_fail_count = 0
            return w3
    except Exception as exc:
        # Log the underlying error on first failure of each cycle, then suppress
        # repeats until we either recover or trigger a reconnect.
        if _w3_fail_count == 0:
            log.warning("web3 is_connected raised: %s", exc)

    _w3_fail_count += 1
    now = time.monotonic()
    if (_w3_fail_count >= W3_RECONNECT_AFTER_FAILS
            and (now - _w3_last_reconnect) >= W3_RECONNECT_BACKOFF_SEC):
        log.warning(
            "web3 disconnected (%d consecutive failures) — rebuilding w3 (rpc=%s)",
            _w3_fail_count, rpc_url.split("/v2/")[0],
        )
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
            _w3_fail_count    = 0
            _w3_last_reconnect = now
            try:
                _connected = w3.is_connected()
                if _connected:
                    log.info("web3 reconnected: connected=True block=%d",
                             w3.eth.block_number)
                else:
                    log.warning("web3 reconnected but still not responding "
                                "(provider returned False on is_connected)")
            except Exception as exc:
                log.warning("web3 reconnected but is_connected raised: %s", exc)
        except Exception as exc:
            log.error("web3 reconnect failed: %s", exc)
            _w3_last_reconnect = now   # reset timer so we don't retry every cycle
    else:
        log.debug("web3 not connected (fail_count=%d)", _w3_fail_count)
    return w3


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("DEX Trader — shadow_mode=%s threshold=%.2f", SHADOW_MODE, THRESHOLD)
    log.info("=" * 60)

    _start_health_server()

    # Trader DB (critical — with retry)
    log.info("connecting to trader DB…")
    trader_conn = db.connect_trader()
    db.migrate(trader_conn)

    # Collector DB (read-only; with retry)
    log.info("connecting to collector DB…")
    collector_conn = db.connect_collector()

    # web3 (RPC for on-chain fallbacks)
    rpc_url = ALCHEMY_BASE_URL or BASE_PUBLIC_RPC
    if not ALCHEMY_BASE_URL:
        log.warning("ALCHEMY_BASE_URL not set — using public Base RPC (rate-limited for on-chain quotes)")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
    log.info("web3: connected=%s rpc=%s", w3.is_connected(), rpc_url.split("/v2/")[0])

    # Model
    scorer = Scorer()
    scorer.load()
    _health["model_version"] = scorer.meta.get("trained_at", "unknown")

    # Restore position state from DB
    risk.load_state(trader_conn)

    log.info("startup complete — entering loop (poll_interval=%ds)", POLL_INTERVAL)

    while True:
        t0 = time.monotonic()

        try:
            # Exits fire first — timer-based positions expire regardless of kill switch
            _manage_open_positions(trader_conn, w3)

            # Web3 connectivity check (with backoff reconnect)
            w3 = _maybe_reconnect_w3(w3, rpc_url)

            # Single kill-switch DB read per cycle — passed through to avoid re-query
            kill = risk.check_kill_switch(trader_conn)

            if not kill:
                # Ingest new signals
                new_signals = _ingest_signals(collector_conn, trader_conn)
                for signal in new_signals:
                    try:
                        _process_signal(signal, scorer, trader_conn, w3,
                                        kill_switch_armed=False)
                    except Exception as exc:
                        addr = (signal.get("token_address") or "?")[:12]
                        log.error("signal error %s: %s", addr, exc, exc_info=True)
            else:
                log.info("kill switch armed — skipping entries, managing exits")

            # Hot-reload model if export_model.py was re-run
            scorer.maybe_reload()
            _health["model_version"] = scorer.meta.get("trained_at", "unknown")

        except psycopg2.OperationalError as exc:
            log.error("DB connection lost: %s — attempting reconnect", exc)
            try:
                if trader_conn.closed:
                    trader_conn = db.connect_trader()
                    log.info("trader DB reconnected")
            except Exception as e2:
                log.error("trader reconnect failed: %s", e2)
            try:
                if collector_conn.closed:
                    collector_conn = db.connect_collector()
                    log.info("collector DB reconnected")
            except Exception as e2:
                log.error("collector reconnect failed: %s", e2)
        except Exception as exc:
            log.error("loop error: %s", exc, exc_info=True)

        elapsed = time.monotonic() - t0
        sleep_s = max(0.0, POLL_INTERVAL - elapsed)
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()

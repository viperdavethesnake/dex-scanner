"""Risk controls: position limit, daily loss cap, re-entry lockout, kill switch.

All limits are env-var configurable with conservative shadow-mode defaults.
In-memory state is rebuilt from DB on every startup (crash-safe).
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

log = logging.getLogger(__name__)
utc = timezone.utc

MAX_POSITIONS           = int(os.environ.get("MAX_POSITIONS",          "3"))
TRADE_SIZE_USD          = float(os.environ.get("TRADE_SIZE_USD",       "10.0"))
DAILY_LOSS_CAP_USD      = float(os.environ.get("DAILY_LOSS_CAP_USD",   "50.0"))
MAX_TRADES_PER_HOUR     = int(os.environ.get("MAX_TRADES_PER_HOUR",    "20"))
REENTRY_LOCKOUT_MINUTES = int(os.environ.get("REENTRY_LOCKOUT_MINUTES","30"))

# In-memory state — populated by load_state() on startup
_open_positions: Dict[int, dict] = {}    # trade_id → trade dict
_reentry_lock:   Dict[str, datetime] = {}  # token_address → last_fill_ts (tz-aware)
_trades_this_hour: int = 0
_hour_window_start: datetime = datetime.now(utc)


def load_state(conn) -> None:
    """Restore in-memory position state and re-entry locks from DB."""
    global _open_positions, _reentry_lock, _trades_this_hour, _hour_window_start

    with conn.cursor() as cur:
        # Rebuild open positions
        cur.execute("""
            SELECT id, token_address, fill_ts, fill_price_usd, fill_size_usd,
                   conviction_score, conviction_band, created_at, chain,
                   pair_address, signal_price_usd
            FROM trades
            WHERE status IN ('intent', 'quoted', 'simulated', 'managed')
        """)
        rows = cur.fetchall()
        for (tid, token, fill_ts, fill_price, fill_size,
             score, band, created_at, chain, pair_addr, sig_price) in rows:
            _open_positions[tid] = {
                "id":               tid,
                "token_address":    token,
                "fill_ts":          fill_ts,
                "fill_price_usd":   float(fill_price) if fill_price else None,
                "fill_size_usd":    float(fill_size) if fill_size else TRADE_SIZE_USD,
                "conviction_score": score,
                "conviction_band":  band,
                "created_at":       created_at,
                "chain":            chain,
                "pair_address":     pair_addr,
                "signal_price_usd": float(sig_price) if sig_price else None,
            }

        # Rebuild re-entry locks (last 30 min)
        cur.execute("""
            SELECT token_address, MAX(fill_ts) AS last_fill
            FROM trades
            WHERE fill_ts > NOW() - INTERVAL '30 minutes'
              AND status NOT IN ('skipped', 'failed')
            GROUP BY token_address
        """)
        for token, last_fill in cur.fetchall():
            if last_fill:
                ts = last_fill if last_fill.tzinfo else last_fill.replace(tzinfo=utc)
                _reentry_lock[token] = ts

    log.info("risk: restored %d open positions, %d re-entry locks",
             len(_open_positions), len(_reentry_lock))


def get_open_positions() -> Dict[int, dict]:
    return dict(_open_positions)


def add_position(trade: dict) -> None:
    _open_positions[trade["id"]] = trade
    if trade.get("fill_ts"):
        ts = trade["fill_ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=utc)
        _reentry_lock[trade["token_address"]] = ts


def remove_position(trade_id: int) -> None:
    _open_positions.pop(trade_id, None)


def check_kill_switch(conn) -> bool:
    """Returns True if kill switch is armed. Reads DB each cycle."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM trader_state WHERE key='kill_switch'")
        row = cur.fetchone()
        return bool(row and row[0].lower() == "true")


def check_entry_allowed(token_address: str, conn) -> Tuple[bool, str]:
    """
    Returns (allowed, reason). reason='' when allowed=True.
    Increments the hourly counter only on success (allowed=True).
    """
    global _trades_this_hour, _hour_window_start

    # Kill switch
    if check_kill_switch(conn):
        return False, "kill_switch_armed"

    # Position limit
    if len(_open_positions) >= MAX_POSITIONS:
        return False, f"position_limit:{len(_open_positions)}/{MAX_POSITIONS}"

    # Re-entry lockout
    last_fill = _reentry_lock.get(token_address)
    if last_fill:
        if last_fill.tzinfo is None:
            last_fill = last_fill.replace(tzinfo=utc)
        lockout_end = last_fill + timedelta(minutes=REENTRY_LOCKOUT_MINUTES)
        if datetime.now(utc) < lockout_end:
            remaining = int((lockout_end - datetime.now(utc)).total_seconds() / 60)
            return False, f"reentry_lock:{remaining}m remaining"

    # Hourly rate limit
    now = datetime.now(utc)
    if (now - _hour_window_start).total_seconds() >= 3600:
        _trades_this_hour   = 0
        _hour_window_start  = now
    if _trades_this_hour >= MAX_TRADES_PER_HOUR:
        return False, f"rate_limit:{_trades_this_hour}/{MAX_TRADES_PER_HOUR}/hr"

    # Daily loss cap
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(pnl_usd), 0)
            FROM trades
            WHERE status = 'exited'
              AND created_at >= CURRENT_DATE
        """)
        daily_pnl = float(cur.fetchone()[0])
    if daily_pnl <= -DAILY_LOSS_CAP_USD:
        return False, f"daily_loss_cap:${daily_pnl:.2f}<=-${DAILY_LOSS_CAP_USD:.0f}"

    _trades_this_hour += 1
    return True, ""

"""Conviction score calibration check.

One-shot diagnostic. Run after 33+ exits to gauge whether the model's
conviction_score correlates with realized net_pct. The answer determines
whether the next feature-importance analysis is "refine a working model"
or "diagnose a broken one."

Usage from host (dex-trader-db port 5435 exposed):
  cd /space/docker/containers/dex-scanner
  set -a; source .env; set +a   # load TRADER_DB_PASSWORD
  python3 analysis/calibration_check.py

Or inside the trader container (if scipy etc. matter — they don't here):
  docker cp analysis/calibration_check.py dex-trader:/tmp/
  docker exec -e PYTHONPATH=/app dex-trader python /tmp/calibration_check.py

Dependencies: psycopg2, pandas — both already in the trader image and on
the host if Code Claude has used the analysis dir before. No scipy:
Spearman is computed via pandas rank correlation (mathematically equivalent).
"""
import os
import sys
import psycopg2
import pandas as pd


# ── Connection ────────────────────────────────────────────────────────
host = os.environ.get("TRADER_DB_HOST", "localhost")
port = int(os.environ.get("TRADER_DB_PORT", "5435"))    # 5435 from host; 5432 inside container network
user = os.environ.get("TRADER_DB_USER",     "trader")
pw   = os.environ.get("TRADER_DB_PASSWORD")
db   = os.environ.get("TRADER_DB_NAME",     "trader")

if not pw:
    print("ERROR: TRADER_DB_PASSWORD not set", file=sys.stderr)
    sys.exit(1)


def _connect():
    """Try host:port first; fall back to container-internal hostname."""
    try:
        return psycopg2.connect(host=host, port=port, user=user, password=pw, dbname=db)
    except psycopg2.OperationalError:
        return psycopg2.connect(host="dex-trader-db", port=5432, user=user, password=pw, dbname=db)


conn = _connect()
print(f"Connected to {conn.info.host}:{conn.info.port}/{conn.info.dbname}\n")


# ── Pull data ─────────────────────────────────────────────────────────
df = pd.read_sql("""
    SELECT id, conviction_score, net_pct, gross_pct,
           entry_cost_pct, low_drawdown_pct, backtest_net_pct,
           exit_trigger, exit_quote_source,
           CASE WHEN id <= 62 THEN 'pre_v2' ELSE 'post_v2' END AS cohort
      FROM trades
     WHERE status = 'exited'
       AND conviction_score IS NOT NULL
       AND net_pct IS NOT NULL
     ORDER BY id
""", conn)

# AURAi-class contamination: exit fallback wrote cost_pct=0 pre-fix, inflating net_pct.
clean = df[df['exit_quote_source'] != 'dexscreener_fallback'].copy()

print("=" * 60)
print("Conviction calibration check")
print("=" * 60)
print(f"Total exited:        {len(df)}")
print(f"  with dexscreener:  {len(df) - len(clean)}  (excluded — AURAi-class P&L inflation)")
print(f"Clean for analysis:  {len(clean)}")
print("\nCohort breakdown (clean):")
print(clean.groupby('cohort').size().to_string())


# ── Spearman correlation (pandas-only) ────────────────────────────────
def _spearman(x, y):
    """Pandas rank correlation. Returns None if too few samples."""
    if len(x) < 3:
        return None
    return x.rank().corr(y.rank())


print("\n" + "-" * 60)
print("Spearman rank correlation (conviction_score x net_pct)")
print("-" * 60)

rho_all = _spearman(clean['conviction_score'], clean['net_pct'])
if rho_all is not None:
    print(f"All clean    (n={len(clean):2d}): rho = {rho_all:+.3f}")
else:
    print("All clean: n too small")

post = clean[clean['cohort'] == 'post_v2']
rho_post = _spearman(post['conviction_score'], post['net_pct'])
if rho_post is not None:
    print(f"Post-v2 only (n={len(post):2d}): rho = {rho_post:+.3f}")


# ── Conviction band breakdown ─────────────────────────────────────────
print("\n" + "-" * 60)
print("Win rate and avg net by conviction band (all clean)")
print("-" * 60)

bands = pd.cut(clean['conviction_score'],
               bins=[0.65, 0.70, 0.75, 0.80, 1.00],
               labels=['0.65-0.70', '0.70-0.75', '0.75-0.80', '0.80+'],
               include_lowest=True)

summary = clean.assign(band=bands).groupby('band', observed=True).agg(
    n=('net_pct', 'size'),
    avg_net=('net_pct', 'mean'),
    median_net=('net_pct', 'median'),
    win_rate=('net_pct', lambda x: (x > 0).mean()),
).round(3)
print(summary.to_string())


# ── Backtest vs realized ──────────────────────────────────────────────
print("\n" + "-" * 60)
print("Backtest expectation vs realized (all clean)")
print("-" * 60)

bt = clean.assign(band=bands).groupby('band', observed=True).agg(
    n=('net_pct', 'size'),
    avg_backtest=('backtest_net_pct', 'mean'),
    avg_realized=('net_pct', 'mean'),
).round(2)
bt['delta'] = (bt['avg_realized'] - bt['avg_backtest']).round(2)
print(bt.to_string())


# ── Kelly criterion ───────────────────────────────────────────────────
print("\n" + "-" * 60)
print("Kelly criterion (informational — flat $10 in shadow)")
print("-" * 60)

wins   = clean[clean['net_pct'] > 0]['net_pct'] / 100   # as decimals
losses = clean[clean['net_pct'] < 0]['net_pct'] / 100

if len(wins) > 0 and len(losses) > 0:
    p     = len(wins) / (len(wins) + len(losses))   # win probability
    avg_w = wins.mean()                              # avg win, decimal
    avg_l = -losses.mean()                           # avg loss, positive magnitude
    b     = avg_w / avg_l if avg_l > 0 else 0
    kelly = p - (1 - p) / b if b > 0 else 0
    print(f"win_rate:           {p:.1%}")
    print(f"avg_win:            {avg_w:+.2%}")
    print(f"avg_loss:           -{avg_l:.2%}")
    print(f"b (win/loss ratio): {b:.2f}")
    print(f"Kelly fraction:     {kelly:+.1%}  (of bankroll per trade)")
    if kelly <= 0:
        print("  -> Kelly is non-positive: edge does not justify any bet at current parameters.")
        print("     Confirms strategy is not yet ready for live capital regardless of size.")
    elif kelly > 0.10:
        print(f"  -> Kelly > 10%: aggressive. Half-Kelly typically used in practice: {kelly/2:.1%}")
    else:
        print(f"  -> Kelly modest. Half-Kelly: {kelly/2:.1%}")
else:
    print("Not enough wins and losses to compute Kelly")


# ── Interpretation guide ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("Interpretation (do NOT act yet — observe and report)")
print("=" * 60)
print("""
rho > +0.4
  Model is producing useful ordering. Higher conviction = higher outcomes.
  Proceed to feature-importance work as planned.

rho between -0.2 and +0.4
  Model produces binary signal (pass/skip) but not useful ordering above
  threshold. High-conviction signals are not meaningfully better than
  low-conviction. Feature importance still useful — will reveal what the
  model is and is not capturing.

rho < -0.2
  Anti-calibrated. Higher conviction predicts WORSE outcomes. Overfit on
  training set or production feature distribution drift. Stop before more
  downstream work; this is a retrain trigger.

The win-rate-by-band table is the same answer at lower resolution. If
0.80+ band has lower win rate than 0.65-0.70, that's diagnostic regardless
of Spearman.

The backtest-vs-realized delta says how much real execution friction
(slippage, gas, drift between signal and fill) eats the backtest's edge.
A consistent negative delta means execution costs more than the 1.5%
baseline. A delta that grows MORE negative with conviction means high-
conviction signals are over-exposed to slippage (low liquidity, fast
moves) — that's a tradable diagnostic separate from the calibration test.
""")

conn.close()

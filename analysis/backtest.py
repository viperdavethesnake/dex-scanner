"""
DEX Scanner — Realistic Backtest
=================================
Simulates trading the model's predictions on val data (May 21-23).
Answers: "If I started with $100, what happens?"

Design choices (explicit):
  - $10 flat per qualifying token per scan
  - Exit at 5m outcome (our prediction window)
  - 1.5% round-trip cost (gas + swap fee + slippage)
  - Two modes: all qualifying rows vs first-entry-per-token
  - Compares model thresholds vs current hard filter vs random
  - Separate Base / Solana breakdown

Run:
    cd analysis && source venv/bin/activate && python3 backtest.py
"""

import warnings; warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import psycopg2
import lightgbm as lgb
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns
from sklearn.metrics import roc_auc_score
import os

DB = dict(host='localhost', port=5434, user='collector',
          password='collector', dbname='collector_signals')
TRAIN_CUTOFF = '2026-05-21'
AGE_MIN, AGE_MAX = 15, 90
ROUND_TRIP_COST_PCT = 1.5   # % — gas + swap fee + slippage
BET_SIZE = 10.0              # $ per trade
STARTING_CAPITAL = 100.0
FIG_DIR = 'figures/backtest'
os.makedirs(FIG_DIR, exist_ok=True)

CATEGORICALS  = ['chain', 'dex', 'micro_trend', 'vol_trend']
FEATURE_COLS  = [
    'chain', 'dex', 'age_minutes',
    'liquidity_usd', 'market_cap', 'volume_5m', 'volume_1h', 'volume_6h',
    'price_ch_5m', 'price_ch_1h', 'price_ch_6h',
    'buys_1h', 'sells_1h', 'buys_5m', 'sells_5m',
    'vl_ratio', 'vol_trend', 'vol_trend_pct', 'micro_trend',
    'buy_pct_5m', 'buy_pct_1h',
    'vol5m_proj_ratio', 'vol1h_6h_ratio', 'liq_mcap_ratio',
    'net_txn_5m', 'net_txn_1h', 'txn_accel', 'sell_pressure_5m', 'momentum_score',
    'log_liquidity_usd', 'log_market_cap', 'log_volume_5m', 'log_volume_1h', 'log_volume_6h',
]

plt.rcParams.update({'figure.facecolor': '#1a1a1a', 'axes.facecolor': '#242424',
                     'axes.edgecolor': '#555', 'text.color': '#e8e8e8',
                     'axes.labelcolor': '#e8e8e8', 'xtick.color': '#aaa',
                     'ytick.color': '#aaa', 'grid.color': '#333',
                     'axes.titlecolor': '#fff'})

def savefig(name):
    path = f'{FIG_DIR}/{name}'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#1a1a1a')
    plt.close('all')
    print(f'  → {path}')

# ── Data & model ───────────────────────────────────────────────────────────────

def load_and_engineer():
    sql = f"""
    SELECT id, scanned_at, token_address, pair_address, symbol, chain, dex,
           age_minutes, liquidity_usd, market_cap,
           volume_5m, volume_1h, volume_6h,
           price_ch_5m, price_ch_1h, price_ch_6h,
           buys_1h, sells_1h, buys_5m, sells_5m,
           vl_ratio, vol_trend, vol_trend_pct, micro_trend,
           buy_pct_5m, buy_pct_1h, outcome_pct
    FROM raw_signals
    WHERE price_at_5m IS NOT NULL
      AND age_minutes >= {AGE_MIN} AND age_minutes <= {AGE_MAX}
    ORDER BY scanned_at
    """
    conn = psycopg2.connect(**DB)
    df = pd.read_sql(sql, conn, parse_dates=['scanned_at'])
    conn.close()

    def sdiv(a, b): return np.where(b > 0, a / b, np.nan)
    df['vol5m_proj_ratio']= sdiv(df['volume_5m'] * 12, df['volume_1h'])
    df['vol1h_6h_ratio']  = sdiv(df['volume_1h'] * 6,  df['volume_6h'])
    df['liq_mcap_ratio']  = sdiv(df['liquidity_usd'], df['market_cap'])
    df['net_txn_5m']      = df['buys_5m']  - df['sells_5m']
    df['net_txn_1h']      = df['buys_1h']  - df['sells_1h']
    df['txn_accel']       = sdiv(df['buys_5m'] * 12, df['buys_1h'].clip(lower=1))
    df['sell_pressure_5m']= sdiv(df['sells_5m'], (df['buys_5m'] + df['sells_5m']).clip(lower=1))
    df['momentum_score']  = df['price_ch_5m'] * df['vol5m_proj_ratio'].clip(upper=10)
    for col in ['liquidity_usd', 'market_cap', 'volume_5m', 'volume_1h', 'volume_6h']:
        df[f'log_{col}'] = np.log1p(df[col].fillna(0))
    for col in CATEGORICALS:
        df[col] = df[col].fillna('unknown').astype('category')
    df['target'] = (df['outcome_pct'] > 0).astype(int)
    return df

def train_model(train):
    X, y = train[FEATURE_COLS], train['target']
    pw = (y==0).sum() / max((y==1).sum(), 1)
    m = lgb.LGBMClassifier(
        objective='binary', metric='auc', n_estimators=300,
        learning_rate=0.05, num_leaves=20, min_child_samples=50,
        feature_fraction=0.7, bagging_fraction=0.8, bagging_freq=5,
        reg_lambda=5.0, reg_alpha=0.1, scale_pos_weight=pw,
        verbosity=-1, random_state=42,
    )
    m.fit(X, y, categorical_feature=[c for c in CATEGORICALS if c in FEATURE_COLS])
    return m

def current_filter_passes(row):
    age   = row['age_minutes']
    micro = str(row['micro_trend'])
    vl    = row['vl_ratio'] if pd.notna(row['vl_ratio']) else 0
    chain = str(row['chain'])
    vl_pass   = (vl <= 4.0) if chain == 'solana' else (vl <= 8.0)
    bad_micro = {'recovering','down','flat'} if chain == 'solana' else {'recovering','down'}
    return (15 <= age <= 90) and (micro not in bad_micro) and vl_pass

# ── Backtest engine ────────────────────────────────────────────────────────────

def run_backtest(trades_df, label, bet_size=BET_SIZE, cost_pct=ROUND_TRIP_COST_PCT):
    """
    trades_df: rows where we decide to enter a trade.
               Must have 'outcome_pct', 'scanned_at', 'chain', 'symbol'.

    Returns summary dict + equity curve series.
    """
    if len(trades_df) == 0:
        return None, None

    df = trades_df.copy().sort_values('scanned_at')

    # P&L per trade: outcome% minus round-trip cost
    df['gross_pct'] = df['outcome_pct']
    df['net_pct']   = df['gross_pct'] - cost_pct
    df['pnl']       = bet_size * df['net_pct'] / 100.0
    df['win']       = df['net_pct'] > 0

    # Equity curve: cumulative P&L starting from 0
    # (shows gains/losses relative to starting capital)
    df = df.sort_values('scanned_at').reset_index(drop=True)
    df['cum_pnl']   = df['pnl'].cumsum()
    df['equity']    = STARTING_CAPITAL + df['cum_pnl']

    n           = len(df)
    wins        = df['win'].sum()
    total_pnl   = df['pnl'].sum()
    final_eq    = STARTING_CAPITAL + total_pnl
    win_pct     = wins / n * 100
    avg_win     = df[df['win']]['pnl'].mean() if wins > 0 else 0
    avg_loss    = df[~df['win']]['pnl'].mean() if (~df['win']).sum() > 0 else 0
    best_trade  = df['pnl'].max()
    worst_trade = df['pnl'].min()
    avg_pnl     = df['pnl'].mean()
    total_deployed = n * bet_size
    roi         = total_pnl / STARTING_CAPITAL * 100   # vs starting $100

    # Max drawdown on equity curve
    roll_max    = df['equity'].cummax()
    drawdown    = df['equity'] - roll_max
    max_dd      = drawdown.min()

    # Profit factor
    gross_wins  = df[df['pnl'] > 0]['pnl'].sum()
    gross_losses= abs(df[df['pnl'] < 0]['pnl'].sum())
    pf          = gross_wins / gross_losses if gross_losses > 0 else np.inf

    chain_breakdown = df.groupby('chain', observed=True).agg(
        n=('pnl','count'),
        win_rate=('win','mean'),
        total_pnl=('pnl','sum'),
        avg_pnl=('pnl','mean'),
    ).reset_index()

    summary = dict(
        label=label, n_trades=n, win_pct=win_pct,
        avg_pnl=avg_pnl, total_pnl=total_pnl,
        final_equity=final_eq, roi_pct=roi,
        avg_win=avg_win, avg_loss=avg_loss,
        best=best_trade, worst=worst_trade,
        max_drawdown=max_dd, profit_factor=pf,
        total_deployed=total_deployed,
        chain_breakdown=chain_breakdown,
    )
    return summary, df

# ── Scenarios ─────────────────────────────────────────────────────────────────

def print_summary(s):
    if s is None:
        print('  No trades.')
        return
    print(f"\n  ── {s['label']} ──")
    print(f"  Trades        : {s['n_trades']:,}")
    print(f"  Win rate      : {s['win_pct']:.1f}%  (after {ROUND_TRIP_COST_PCT}% cost)")
    print(f"  Avg P&L/trade : ${s['avg_pnl']:+.3f}")
    print(f"  Avg win       : ${s['avg_win']:+.2f}  |  Avg loss: ${s['avg_loss']:+.2f}")
    print(f"  Best trade    : ${s['best']:+.2f}  |  Worst: ${s['worst']:+.2f}")
    print(f"  Profit factor : {s['profit_factor']:.2f}x")
    print(f"  Total P&L     : ${s['total_pnl']:+.2f}")
    print(f"  Final equity  : ${s['final_equity']:.2f}  (started $100)")
    print(f"  ROI on $100   : {s['roi_pct']:+.1f}%")
    print(f"  Max drawdown  : ${s['max_drawdown']:.2f}")
    print(f"  Total deployed: ${s['total_deployed']:,.0f}  ({s['n_trades']:,} × ${BET_SIZE})")
    if s['chain_breakdown'] is not None:
        print(f"  By chain:")
        for _, r in s['chain_breakdown'].iterrows():
            print(f"    {r['chain']:8s}: n={r['n']:,}  win={r['win_rate']*100:.1f}%  "
                  f"avg=${r['avg_pnl']:+.3f}  total=${r['total_pnl']:+.2f}")

def main():
    print('DEX Scanner — Backtest')
    print('='*60)
    print(f'Starting capital : ${STARTING_CAPITAL}')
    print(f'Bet size         : ${BET_SIZE} per trade (flat)')
    print(f'Round-trip cost  : {ROUND_TRIP_COST_PCT}% (gas + fee + slippage)')
    print(f'Exit             : 5m outcome (collected by scanner)')
    print(f'Val period       : {TRAIN_CUTOFF} → May 23')

    df = load_and_engineer()

    # Split
    train = df[df['scanned_at'] < TRAIN_CUTOFF]
    val   = df[df['scanned_at'] >= TRAIN_CUTOFF]
    val   = val[~val['token_address'].isin(set(train['token_address']))]

    print(f'\nTrain: {len(train):,} rows  Val: {len(val):,} rows')
    print(f'Val base rate: {val["target"].mean()*100:.1f}% win')
    print(f'Val date range: {val["scanned_at"].min().date()} → {val["scanned_at"].max().date()}')

    # Train model
    print('\nTraining model…')
    model = train_model(train)
    val = val.copy()
    val['model_score'] = model.predict_proba(val[FEATURE_COLS])[:, 1]

    auc = roc_auc_score(val['target'], val['model_score'])
    print(f'Val AUC: {auc:.4f}')

    # Current filter
    val['filter_pass'] = val.apply(current_filter_passes, axis=1)

    # ── Scenario definitions ──────────────────────────────────────────────────

    scenarios = []

    # 1. Random baseline: all val rows (no filter, no model)
    scenarios.append(('Random — no filter', val))

    # 2. Current hard filter only (no model)
    scenarios.append(('Current filter only (no model)', val[val['filter_pass']]))

    # 3. Model thresholds (all rows, no additional filter)
    for thr in [0.55, 0.60, 0.65, 0.70, 0.75]:
        label = f'Model ≥{thr:.2f} (all tokens)'
        scenarios.append((label, val[val['model_score'] >= thr]))

    # 4. Model + current filter combined
    for thr in [0.55, 0.60, 0.65]:
        label = f'Model ≥{thr:.2f} + current filter'
        sub = val[val['filter_pass'] & (val['model_score'] >= thr)]
        scenarios.append((label, sub))

    # 5. Base only, model threshold
    for thr in [0.60, 0.65, 0.70]:
        label = f'BASE only — model ≥{thr:.2f}'
        sub = val[(val['chain'] == 'base') & (val['model_score'] >= thr)]
        scenarios.append((label, sub))

    # 6. Solana only, model threshold
    for thr in [0.60, 0.65, 0.70]:
        label = f'SOLANA only — model ≥{thr:.2f}'
        sub = val[(val['chain'] == 'solana') & (val['model_score'] >= thr)]
        scenarios.append((label, sub))

    # ── Run all scenarios ─────────────────────────────────────────────────────

    print('\n' + '='*60)
    print('SCENARIO RESULTS')
    print('='*60)

    summaries = []
    equity_curves = {}

    for label, trades_df in scenarios:
        s, traded = run_backtest(trades_df, label)
        print_summary(s)
        if s is not None:
            summaries.append(s)
            equity_curves[label] = traded

    # ── Dedup mode: first entry per token only ────────────────────────────────

    print('\n' + '='*60)
    print('FIRST-ENTRY-PER-TOKEN MODE')
    print('(Each token traded once — first time it crosses threshold)')
    print('='*60)

    dedup_scenarios = []
    for thr in [0.55, 0.60, 0.65, 0.70]:
        filtered = val[val['model_score'] >= thr].sort_values('scanned_at')
        first_entry = filtered.drop_duplicates(subset='token_address', keep='first')
        dedup_scenarios.append((f'First-entry — model ≥{thr:.2f}', first_entry))

    # Also: current filter, first entry
    cf_first = val[val['filter_pass']].sort_values('scanned_at').drop_duplicates(
        subset='token_address', keep='first')
    dedup_scenarios.insert(0, ('First-entry — current filter', cf_first))

    dedup_summaries = []
    for label, trades_df in dedup_scenarios:
        s, traded = run_backtest(trades_df, label)
        print_summary(s)
        if s is not None:
            dedup_summaries.append(s)
            equity_curves[label] = traded

    # ── Key statistics table ──────────────────────────────────────────────────

    print('\n' + '='*60)
    print('COMPARISON TABLE — all scenarios')
    print('='*60)
    print(f"{'Strategy':45s} {'Trades':>7} {'Win%':>6} {'Avg$/t':>8} {'Total$':>8} "
          f"{'Final$':>8} {'ROI':>7} {'MaxDD':>7} {'PF':>5}")
    print('-'*110)

    all_summaries = summaries + dedup_summaries
    for s in all_summaries:
        print(f"{s['label']:45s} {s['n_trades']:7,} {s['win_pct']:5.1f}% "
              f"{s['avg_pnl']:+7.3f} {s['total_pnl']:+7.2f} "
              f"{s['final_equity']:7.2f} {s['roi_pct']:+6.1f}% "
              f"{s['max_drawdown']:+6.2f} {s['profit_factor']:4.2f}x")

    # ── Plots ─────────────────────────────────────────────────────────────────

    # Equity curves — key scenarios
    key_curves = [
        'Random — no filter',
        'Current filter only (no model)',
        'Model ≥0.60 (all tokens)',
        'Model ≥0.65 (all tokens)',
        'Model ≥0.70 (all tokens)',
        'BASE only — model ≥0.65',
        'First-entry — model ≥0.65',
    ]

    fig, ax = plt.subplots(figsize=(14, 7))
    colors = ['#555', '#aaa', '#4a9eff', '#50c87a', '#ff9f43', '#b06cff', '#ff6b6b']
    for curve_label, color in zip(key_curves, colors):
        if curve_label not in equity_curves:
            continue
        ec = equity_curves[curve_label]
        ax.plot(range(len(ec)), ec['equity'], label=curve_label, color=color, lw=1.5, alpha=0.85)

    ax.axhline(STARTING_CAPITAL, color='#aaa', ls=':', lw=1, alpha=0.5)
    ax.set_xlabel('Trade number (chronological)')
    ax.set_ylabel('Equity ($)')
    ax.set_title(f'Equity Curves — $10/trade, {ROUND_TRIP_COST_PCT}% round-trip cost\n'
                 f'Val period: May 21–23 (not seen in training)')
    ax.legend(fontsize=8, loc='upper left')
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter('$%.0f'))
    plt.tight_layout()
    savefig('equity_curves.png')

    # ROI bar chart
    key_labels = [s['label'] for s in all_summaries
                  if s['n_trades'] >= 10]
    rois   = [s['roi_pct'] for s in all_summaries if s['n_trades'] >= 10]
    colors_bar = ['#50c87a' if r > 0 else '#ff6b6b' for r in rois]

    fig, ax = plt.subplots(figsize=(13, 8))
    bars = ax.barh(key_labels, rois, color=colors_bar, alpha=0.85)
    ax.axvline(0, color='#aaa', lw=1)
    for bar, roi in zip(bars, rois):
        ax.text(bar.get_width() + (0.2 if roi >= 0 else -0.2),
                bar.get_y() + bar.get_height()/2,
                f'{roi:+.1f}%', va='center', fontsize=8,
                color='#e8e8e8', ha='left' if roi >= 0 else 'right')
    ax.set_xlabel('ROI on $100 starting capital')
    ax.set_title(f'Strategy ROI comparison — $10/trade, {ROUND_TRIP_COST_PCT}% cost')
    plt.tight_layout()
    savefig('roi_comparison.png')

    # Trade distribution: outcome_pct histogram for best model vs random
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for ax, (label, sub) in zip(axes, [
        ('Random', val),
        ('Current filter', val[val['filter_pass']]),
        ('Model ≥0.65', val[val['model_score'] >= 0.65]),
    ]):
        net = sub['outcome_pct'] - ROUND_TRIP_COST_PCT
        net_clip = net.clip(-30, 60)
        ax.hist(net_clip[net_clip < 0], bins=40, color='#ff6b6b', alpha=0.7, label='Loss')
        ax.hist(net_clip[net_clip >= 0], bins=40, color='#50c87a', alpha=0.7, label='Win')
        ax.axvline(0, color='#aaa', lw=1.5)
        ax.axvline(net.mean(), color='#ff9f43', lw=2, ls='--',
                   label=f'Mean {net.mean():+.2f}%')
        win_rate = (net > 0).mean() * 100
        ax.set_title(f'{label}\nwin={win_rate:.1f}%  mean={net.mean():+.2f}%  n={len(sub):,}')
        ax.set_xlabel('Net return % (after cost)')
        ax.legend(fontsize=8)
    plt.suptitle('Trade return distributions (clipped at -30/+60%)', y=1.02, fontsize=12)
    plt.tight_layout()
    savefig('return_distributions.png')

    # Win rate vs cost sensitivity
    costs = np.arange(0, 5.5, 0.5)
    fig, ax = plt.subplots(figsize=(10, 5))
    for thr, color in [(0.55,'#aaa'),(0.60,'#4a9eff'),(0.65,'#50c87a'),(0.70,'#ff9f43')]:
        sub = val[val['model_score'] >= thr]
        win_rates = [(sub['outcome_pct'] > c).mean() * 100 for c in costs]
        ax.plot(costs, win_rates, 'o-', color=color, label=f'Model ≥{thr}  n={len(sub):,}')

    # Random baseline
    win_rates_rand = [(val['outcome_pct'] > c).mean() * 100 for c in costs]
    ax.plot(costs, win_rates_rand, 's--', color='#555', label=f'Random  n={len(val):,}')
    ax.axhline(50, color='#ff6b6b', ls=':', lw=1.5, label='Break-even')
    ax.set_xlabel('Round-trip cost assumption (%)')
    ax.set_ylabel('Win rate (%)')
    ax.set_title('Win rate vs transaction cost assumption\n(higher cost = harder to profit)')
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    savefig('cost_sensitivity.png')

    print(f'\n✓ Figures saved to analysis/{FIG_DIR}/')


if __name__ == '__main__':
    main()

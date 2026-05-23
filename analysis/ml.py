"""
DEX Scanner — ML Analysis
=========================
Trains a LightGBM binary classifier on collector data to predict
5-minute win/loss (outcome_pct > 0) for tokens in the scanner window (15–90m).

Usage:
    cd analysis/
    source venv/bin/activate
    python3 ml.py [--chain base|solana|all] [--full-window]

Outputs:
    - Feature importance plot      → figures/feature_importance.png
    - SHAP summary plot            → figures/shap_summary.png
    - Lift curve                   → figures/lift_curve.png
    - Precision by threshold table → figures/precision_table.png
    - Threshold recommendation     → stdout
"""

import argparse
import os
import sys
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
import psycopg2
import lightgbm as lgb
import shap
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns

from sklearn.metrics import (
    roc_auc_score, average_precision_score,
    precision_score, recall_score, f1_score,
    classification_report
)
from sklearn.preprocessing import LabelEncoder

# ── Config ────────────────────────────────────────────────────────────────────

DB = dict(host='localhost', port=5434, user='collector',
          password='collector', dbname='collector_signals')

TRAIN_CUTOFF = '2026-05-21'   # train = May 17–20, val = May 21–23
AGE_MIN, AGE_MAX = 15, 90     # scanner window

CATEGORICAL_FEATURES = ['chain', 'dex', 'micro_trend', 'vol_trend']
DROP_COLS = ['token_address', 'pair_address', 'symbol', 'name',
             'scanned_at', 'pair_created_at', 'price_usd',
             'price_at_5m', 'outcome_pct', 'id']

os.makedirs('figures', exist_ok=True)

# ── Data pull ─────────────────────────────────────────────────────────────────

def load_data(chain_filter=None, age_min=AGE_MIN, age_max=AGE_MAX):
    print(f"\n{'='*60}")
    print(f"Loading data  chain={chain_filter or 'all'}  age={age_min}–{age_max}m")
    print(f"{'='*60}")

    where = f"""
        price_at_5m IS NOT NULL
        AND age_minutes >= {age_min}
        AND age_minutes <= {age_max}
    """
    if chain_filter:
        where += f" AND chain = '{chain_filter}'"

    sql = f"""
    SELECT
        id, scanned_at, token_address, pair_address, symbol, name,
        chain, dex, pair_created_at,
        age_minutes, price_usd, liquidity_usd, market_cap,
        volume_5m, volume_1h, volume_6h,
        price_ch_5m, price_ch_1h, price_ch_6h,
        buys_1h, sells_1h, buys_5m, sells_5m,
        vl_ratio, vol_trend, vol_trend_pct, micro_trend,
        buy_pct_5m, buy_pct_1h,
        price_at_5m, outcome_pct
    FROM raw_signals
    WHERE {where}
    ORDER BY scanned_at
    """

    conn = psycopg2.connect(**DB)
    df = pd.read_sql(sql, conn)
    conn.close()

    print(f"Rows loaded : {len(df):,}")
    print(f"Tokens      : {df['token_address'].nunique():,}")
    print(f"Date range  : {df['scanned_at'].min().date()} → {df['scanned_at'].max().date()}")
    print(f"Chain split : {df['chain'].value_counts().to_dict()}")
    print(f"Win rate    : {(df['outcome_pct'] > 0).mean()*100:.1f}%  "
          f"(base={df['chain'].map(df.groupby('chain').apply(lambda x: (x['outcome_pct']>0).mean())).values})")
    return df


# ── Feature engineering ───────────────────────────────────────────────────────

def engineer_features(df):
    df = df.copy()

    # Target
    df['target'] = (df['outcome_pct'] > 0).astype(int)

    # Derived ratios (safe divide)
    def sdiv(a, b, fill=np.nan):
        return np.where(b > 0, a / b, fill)

    df['vol5m_1h_ratio']  = sdiv(df['volume_5m'] * 12, df['volume_1h'])   # projected vs actual
    df['vol1h_6h_ratio']  = sdiv(df['volume_1h'] * 6,  df['volume_6h'])
    df['liq_mcap_ratio']  = sdiv(df['liquidity_usd'],   df['market_cap'])
    df['net_txn_5m']      = df['buys_5m']  - df['sells_5m']                # raw buy/sell delta
    df['net_txn_1h']      = df['buys_1h']  - df['sells_1h']
    df['txn_accel']       = sdiv(df['buys_5m'] * 12, df['buys_1h'])        # buy acceleration

    # Log transforms (volumes are very skewed)
    for col in ['liquidity_usd', 'market_cap', 'volume_5m', 'volume_1h', 'volume_6h']:
        df[f'log_{col}'] = np.log1p(df[col].fillna(0))

    # Encode categoricals as integer codes (LightGBM handles natively)
    for col in CATEGORICAL_FEATURES:
        df[col] = df[col].fillna('unknown').astype('category')

    print(f"\nFeatures engineered. Total columns: {len(df.columns)}")
    return df


def get_feature_cols(df):
    exclude = set(DROP_COLS) | {'target'}
    return [c for c in df.columns if c not in exclude]


# ── Train / val split ─────────────────────────────────────────────────────────

def split_data(df, cutoff=TRAIN_CUTOFF):
    train = df[df['scanned_at'] < cutoff].copy()
    val   = df[df['scanned_at'] >= cutoff].copy()

    # Token-level leak check: remove val tokens that appear in train
    train_tokens = set(train['token_address'])
    val_clean = val[~val['token_address'].isin(train_tokens)].copy()
    val_leaked = val[ val['token_address'].isin(train_tokens)].copy()

    print(f"\nTime split at {cutoff}:")
    print(f"  Train : {len(train):,} rows  {train['token_address'].nunique():,} tokens"
          f"  win={train['target'].mean()*100:.1f}%")
    print(f"  Val   : {len(val):,} rows  {val['token_address'].nunique():,} tokens"
          f"  win={val['target'].mean()*100:.1f}%")
    print(f"  Val (no train-token leak): {len(val_clean):,} rows")
    if len(val_leaked):
        print(f"  ⚠  {len(val_leaked):,} val rows share a token with train — excluded from clean eval")

    return train, val, val_clean


# ── Model ─────────────────────────────────────────────────────────────────────

def train_model(train, feature_cols):
    X = train[feature_cols]
    y = train['target']

    pos_weight = (y == 0).sum() / (y == 1).sum()
    print(f"\nTraining LightGBM  n={len(train):,}  pos_weight={pos_weight:.2f}")

    params = dict(
        objective='binary',
        metric='auc',
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=30,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=5,
        scale_pos_weight=pos_weight,
        verbosity=-1,
        random_state=42,
    )

    model = lgb.LGBMClassifier(**params)
    model.fit(
        X, y,
        categorical_feature=[c for c in CATEGORICAL_FEATURES if c in feature_cols],
        callbacks=[lgb.early_stopping(50, verbose=False),
                   lgb.log_evaluation(100)],
        eval_set=[(X, y)],
    )
    print(f"Best iteration: {model.best_iteration_}")
    return model


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate(model, val, feature_cols, label='val'):
    X = val[feature_cols]
    y = val['target']
    proba = model.predict_proba(X)[:, 1]

    auc  = roc_auc_score(y, proba)
    ap   = average_precision_score(y, proba)
    base = y.mean()

    print(f"\n── {label} evaluation ({len(val):,} rows, base rate={base*100:.1f}%) ──")
    print(f"  ROC-AUC       : {auc:.4f}")
    print(f"  Avg Precision : {ap:.4f}  (vs base {base:.4f})")

    # Precision / recall / lift at various thresholds
    print(f"\n  Threshold | Precision | Recall | Lift   | N flagged")
    print(f"  ----------|-----------|--------|--------|----------")
    rows = []
    for thr in [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]:
        pred = (proba >= thr).astype(int)
        n = pred.sum()
        if n == 0:
            continue
        prec = precision_score(y, pred, zero_division=0)
        rec  = recall_score(y, pred, zero_division=0)
        lift = prec / base if base > 0 else 0
        print(f"  {thr:.2f}      | {prec*100:6.1f}%   | {rec*100:5.1f}%  | {lift:.2f}x  | {n:,}")
        rows.append(dict(threshold=thr, precision=prec, recall=rec, lift=lift, n_flagged=n))

    val = val.copy()
    val['proba'] = proba
    return val, pd.DataFrame(rows)


# ── Current filter simulation ─────────────────────────────────────────────────

def eval_current_filter(df):
    """Simulate the current hard pre-filter and show its precision/lift."""
    df = df.copy()

    def passes(row):
        age = row['age_minutes']
        micro = row['micro_trend'] or ''
        vl = row['vl_ratio'] or 0
        chain = row['chain']
        vl_pass = (vl <= 4.0) if chain == 'solana' else (vl <= 8.0)
        bad_micro = {'recovering', 'down', 'flat'} if chain == 'solana' else {'recovering', 'down'}
        return (15 <= age <= 90) and (micro not in bad_micro) and vl_pass

    df['filter_pass'] = df.apply(passes, axis=1)

    print(f"\n── Current hard filter performance ──")
    for chain in ['all', 'base', 'solana']:
        sub = df if chain == 'all' else df[df['chain'] == chain]
        passed = sub[sub['filter_pass']]
        n = len(passed)
        if n == 0:
            continue
        win = passed['target'].mean()
        base = sub['target'].mean()
        lift = win / base if base > 0 else 0
        print(f"  {chain:8s}: pass={n:,}  precision={win*100:.1f}%  "
              f"base={base*100:.1f}%  lift={lift:.2f}x")


# ── Plots ─────────────────────────────────────────────────────────────────────

def plot_feature_importance(model, feature_cols, top_n=30):
    imp = pd.DataFrame({
        'feature': feature_cols,
        'importance': model.feature_importances_
    }).sort_values('importance', ascending=False).head(top_n)

    fig, ax = plt.subplots(figsize=(10, 8))
    sns.barplot(data=imp, y='feature', x='importance', ax=ax, color='#4a9eff')
    ax.set_title(f'LightGBM Feature Importance (top {top_n})', fontsize=13, pad=12)
    ax.set_xlabel('Importance (split count)')
    ax.set_ylabel('')
    plt.tight_layout()
    plt.savefig('figures/feature_importance.png', dpi=150)
    plt.close()
    print("\nSaved: figures/feature_importance.png")

    print(f"\nTop 15 features:")
    for _, r in imp.head(15).iterrows():
        print(f"  {r['feature']:30s} {r['importance']:,.0f}")


def plot_shap(model, val, feature_cols, max_display=20):
    print("\nComputing SHAP values (sample of 2000)…")
    sample = val.sample(min(2000, len(val)), random_state=42)
    X = sample[feature_cols]

    explainer = shap.TreeExplainer(model)
    sv = explainer(X)

    fig, ax = plt.subplots(figsize=(10, 8))
    shap.summary_plot(sv, X, max_display=max_display, show=False, plot_size=None)
    plt.title('SHAP Feature Impact on Win Probability', fontsize=13, pad=12)
    plt.tight_layout()
    plt.savefig('figures/shap_summary.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("Saved: figures/shap_summary.png")


def plot_lift_curve(val_scored):
    df = val_scored.sort_values('proba', ascending=False).copy()
    df['win'] = df['target']
    base = df['win'].mean()

    # Cumulative precision as we lower the threshold
    df['cumwin'] = df['win'].cumsum()
    df['cumprec'] = df['cumwin'] / (np.arange(len(df)) + 1)
    df['pct_flagged'] = (np.arange(len(df)) + 1) / len(df) * 100

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(df['pct_flagged'], df['cumprec'] * 100, color='#4a9eff', lw=2, label='Model precision')
    ax.axhline(base * 100, color='#ff6b6b', lw=1.5, ls='--', label=f'Base rate ({base*100:.1f}%)')
    ax.set_xlabel('% of tokens flagged (sorted by model score, highest first)')
    ax.set_ylabel('Precision (%)')
    ax.set_title('Lift Curve — Model vs Base Rate')
    ax.legend()
    ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig('figures/lift_curve.png', dpi=150)
    plt.close()
    print("Saved: figures/lift_curve.png")


def plot_outcome_by_score(val_scored):
    df = val_scored.copy()
    df['score_bin'] = pd.cut(df['proba'], bins=10)

    stats = df.groupby('score_bin', observed=True).agg(
        n=('target', 'count'),
        win_pct=('target', 'mean'),
        avg_outcome=('outcome_pct', 'mean')
    ).reset_index()

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    ax1.bar(range(len(stats)), stats['win_pct'] * 100, color='#4a9eff', edgecolor='#2a7edf')
    ax1.set_xticks(range(len(stats)))
    ax1.set_xticklabels([str(b) for b in stats['score_bin']], rotation=45, ha='right', fontsize=8)
    ax1.set_ylabel('Win rate (%)')
    ax1.set_title('Win Rate by Model Score Decile')
    ax1.axhline(df['target'].mean() * 100, color='#ff6b6b', ls='--', lw=1.5, label='Base rate')
    ax1.legend()

    ax2.bar(range(len(stats)), stats['avg_outcome'], color='#50c87a', edgecolor='#30a85a')
    ax2.set_xticks(range(len(stats)))
    ax2.set_xticklabels([str(b) for b in stats['score_bin']], rotation=45, ha='right', fontsize=8)
    ax2.set_ylabel('Avg outcome_pct (%)')
    ax2.set_title('Average 5m Return by Model Score Decile')
    ax2.axhline(0, color='#ff6b6b', ls='--', lw=1)

    plt.tight_layout()
    plt.savefig('figures/outcome_by_score.png', dpi=150)
    plt.close()
    print("Saved: figures/outcome_by_score.png")


def plot_chain_breakdown(val_scored):
    """Win rate and avg return by chain × score quartile."""
    df = val_scored.copy()
    df['score_q'] = pd.qcut(df['proba'], q=4,
                             labels=['Q1 (low)', 'Q2', 'Q3', 'Q4 (high)'])

    stats = df.groupby(['chain', 'score_q'], observed=True).agg(
        n=('target', 'count'),
        win_pct=('target', 'mean'),
        avg_ret=('outcome_pct', 'mean')
    ).reset_index()

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, col, label in zip(axes, ['win_pct', 'avg_ret'],
                              ['Win rate', 'Avg 5m return (%)']):
        for chain, grp in stats.groupby('chain'):
            ax.plot(grp['score_q'].astype(str), grp[col] * (100 if col == 'win_pct' else 1),
                    marker='o', label=chain)
        ax.set_title(f'{label} by chain × score quartile')
        ax.set_xlabel('Score quartile')
        ax.set_ylabel(label)
        ax.legend()
        ax.grid(alpha=0.3)
        if col == 'win_pct':
            ax.axhline(df['target'].mean() * 100, color='grey', ls=':', lw=1)
        else:
            ax.axhline(0, color='grey', ls=':', lw=1)

    plt.tight_layout()
    plt.savefig('figures/chain_breakdown.png', dpi=150)
    plt.close()
    print("Saved: figures/chain_breakdown.png")


# ── Recommendation ────────────────────────────────────────────────────────────

def print_recommendation(val_clean, val_scored_clean, lift_table):
    best = lift_table[lift_table['lift'] >= 1.3]
    if best.empty:
        print("\n⚠  No threshold achieves 1.3x lift on clean val set.")
        return
    best_row = best[best['precision'] == best['precision'].max()].iloc[0]
    print(f"\n{'='*60}")
    print(f"RECOMMENDATION")
    print(f"{'='*60}")
    print(f"  Best threshold : {best_row['threshold']:.2f}")
    print(f"  Precision      : {best_row['precision']*100:.1f}%")
    print(f"  Lift           : {best_row['lift']:.2f}x over base rate")
    print(f"  Tokens flagged : {int(best_row['n_flagged']):,} / {len(val_clean):,} val rows")
    print(f"  Recall         : {best_row['recall']*100:.1f}%")
    print(f"\n  → Score tokens in n8n, pass score into LLM prompt,")
    print(f"    or use as hard pre-filter at p≥{best_row['threshold']:.2f}.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chain', default='all', choices=['all', 'base', 'solana'])
    parser.add_argument('--full-window', action='store_true',
                        help='Use all ages (not just 15-90m scanner window)')
    args = parser.parse_args()

    age_min = 0 if args.full_window else AGE_MIN
    age_max = 9999 if args.full_window else AGE_MAX
    chain_filter = None if args.chain == 'all' else args.chain

    # 1. Load
    df = load_data(chain_filter, age_min, age_max)

    # 2. Feature engineering
    df = engineer_features(df)
    feature_cols = get_feature_cols(df)
    print(f"Feature columns ({len(feature_cols)}): {feature_cols}")

    # 3. Current filter baseline
    eval_current_filter(df)

    # 4. Split
    train, val, val_clean = split_data(df)

    if len(train) < 200 or len(val_clean) < 50:
        print("⚠  Not enough data for a meaningful split. Try --full-window or --chain all.")
        sys.exit(1)

    # 5. Train
    model = train_model(train, feature_cols)

    # 6. Evaluate
    val_scored, lift_table_all = evaluate(model, val, feature_cols, label='val (all)')
    val_clean_scored, lift_table_clean = evaluate(model, val_clean, feature_cols,
                                                   label='val (no token leak)')

    # 7. Recommendation
    print_recommendation(val_clean, val_clean_scored, lift_table_clean)

    # 8. Chain-level breakdown
    print(f"\n── Per-chain val performance ──")
    for chain in df['chain'].unique():
        sub = val_scored[val_scored['chain'] == chain]
        if len(sub) < 20:
            continue
        proba = sub['proba']
        y = sub['target']
        try:
            auc = roc_auc_score(y, proba)
            base = y.mean()
            print(f"  {chain:8s}: n={len(sub):,}  AUC={auc:.3f}  base={base*100:.1f}%")
        except Exception:
            pass

    # 9. Plots
    print("\nGenerating plots…")
    plot_feature_importance(model, feature_cols)
    plot_shap(model, val_scored, feature_cols)
    plot_lift_curve(val_scored)
    plot_outcome_by_score(val_scored)
    plot_chain_breakdown(val_scored)

    print(f"\n✓ Done. Figures in analysis/figures/")


if __name__ == '__main__':
    main()

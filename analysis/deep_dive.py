"""
DEX Scanner — Deep Dive Analysis
=================================
Exhaustive multi-angle ML analysis of collector data.

Sections:
  A. Data overview & feature distributions (winners vs losers)
  B. Correlation heatmap — all features vs outcome
  C. Regularized LightGBM — fix overfitting, token-grouped CV
  D. Chain-specific models (Base vs Solana separately)
  E. Alternative targets (>0%, >2%, >5%, >10%)
  F. Partial dependence plots — top 8 features
  G. Decision tree — interpretable hard rules from data
  H. Segment-conditional analysis (model within each filter segment)
  I. Time stability — rolling daily AUC
  J. Top/bottom scorer deep dive — what do the extremes look like?
  K. Feature interaction heatmap (SHAP)

Usage:
    cd analysis && source venv/bin/activate
    python3 deep_dive.py
"""

import warnings
warnings.filterwarnings('ignore')

import os, sys
import numpy as np
import pandas as pd
import psycopg2
import lightgbm as lgb
import shap
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import seaborn as sns
from scipy import stats

from sklearn.metrics import (
    roc_auc_score, average_precision_score,
    precision_score, recall_score
)
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.inspection import PartialDependenceDisplay
from sklearn.preprocessing import OrdinalEncoder
import sklearn.inspection as inspection

# ── Config ────────────────────────────────────────────────────────────────────

DB = dict(host='localhost', port=5434, user='collector',
          password='collector', dbname='collector_signals')
TRAIN_CUTOFF = '2026-05-21'
AGE_MIN, AGE_MAX = 15, 90
FIG_DIR = 'figures/deep'
os.makedirs(FIG_DIR, exist_ok=True)

CATEGORICALS = ['chain', 'dex', 'micro_trend', 'vol_trend']

sns.set_theme(style='darkgrid', palette='muted')
plt.rcParams.update({'figure.facecolor': '#1a1a1a', 'axes.facecolor': '#242424',
                     'axes.edgecolor': '#555', 'text.color': '#e8e8e8',
                     'axes.labelcolor': '#e8e8e8', 'xtick.color': '#aaa',
                     'ytick.color': '#aaa', 'grid.color': '#333',
                     'axes.titlecolor': '#fff'})

CHAIN_COLORS = {'base': '#4a9eff', 'solana': '#b06cff'}

def savefig(name):
    path = f'{FIG_DIR}/{name}'
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#1a1a1a')
    plt.close('all')
    print(f'  → {path}')

# ── Data ──────────────────────────────────────────────────────────────────────

def load():
    sql = f"""
    SELECT id, scanned_at, token_address, chain, dex,
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
    print(f'Loaded {len(df):,} rows, {df["token_address"].nunique():,} unique tokens')
    return df

def engineer(df):
    df = df.copy()
    def sdiv(a, b): return np.where(b > 0, a / b, np.nan)

    df['target']          = (df['outcome_pct'] > 0).astype(int)
    df['target_2pct']     = (df['outcome_pct'] > 2).astype(int)
    df['target_5pct']     = (df['outcome_pct'] > 5).astype(int)
    df['target_10pct']    = (df['outcome_pct'] > 10).astype(int)

    df['vol5m_proj_ratio']= sdiv(df['volume_5m'] * 12, df['volume_1h'])
    df['vol1h_6h_ratio']  = sdiv(df['volume_1h'] * 6, df['volume_6h'])
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

    df['date'] = df['scanned_at'].dt.date
    return df

FEATURE_COLS = [
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

# ── Model factory ─────────────────────────────────────────────────────────────

def make_model(pos_weight=1.0, n_est=300, leaves=20, min_child=50, reg_lambda=5.0):
    return lgb.LGBMClassifier(
        objective='binary', metric='auc',
        n_estimators=n_est, learning_rate=0.05,
        num_leaves=leaves, min_child_samples=min_child,
        feature_fraction=0.7, bagging_fraction=0.8, bagging_freq=5,
        reg_lambda=reg_lambda, reg_alpha=0.1,
        scale_pos_weight=pos_weight,
        verbosity=-1, random_state=42,
    )

def fit_model(train, target_col='target', feature_cols=FEATURE_COLS):
    X = train[feature_cols]
    y = train[target_col]
    pw = (y==0).sum() / max((y==1).sum(), 1)
    m = make_model(pos_weight=pw)
    m.fit(X, y, categorical_feature=[c for c in CATEGORICALS if c in feature_cols])
    return m

def score_df(model, df, feature_cols=FEATURE_COLS):
    return model.predict_proba(df[feature_cols])[:, 1]

def lift_table(y, proba, label=''):
    base = y.mean()
    rows = []
    for thr in np.arange(0.40, 0.85, 0.05):
        pred = (proba >= thr).astype(int)
        n = pred.sum()
        if n < 5: continue
        prec = precision_score(y, pred, zero_division=0)
        rec  = recall_score(y, pred, zero_division=0)
        rows.append(dict(threshold=round(thr, 2), precision=prec, recall=rec,
                         lift=prec/base, n=n))
    return pd.DataFrame(rows)

def split(df, cutoff=TRAIN_CUTOFF):
    train = df[df['scanned_at'] < cutoff]
    val   = df[df['scanned_at'] >= cutoff]
    # remove val tokens that appeared in train (leak prevention)
    val = val[~val['token_address'].isin(set(train['token_address']))]
    return train, val

def cv_auc(df, target_col='target', feature_cols=FEATURE_COLS, n_splits=5):
    """Token-grouped k-fold CV — prevents same token in train+val."""
    X = df[feature_cols]
    y = df[target_col]
    groups = df['token_address']
    gkf = GroupKFold(n_splits=n_splits)
    aucs = []
    for tr_idx, val_idx in gkf.split(X, y, groups):
        Xtr, ytr = X.iloc[tr_idx], y.iloc[tr_idx]
        Xval, yval = X.iloc[val_idx], y.iloc[val_idx]
        pw = (ytr==0).sum() / max((ytr==1).sum(), 1)
        m = make_model(pos_weight=pw)
        m.fit(Xtr, ytr, categorical_feature=[c for c in CATEGORICALS if c in feature_cols])
        proba = m.predict_proba(Xval)[:, 1]
        aucs.append(roc_auc_score(yval, proba))
    return aucs

# ══════════════════════════════════════════════════════════════════════════════
# SECTION A — Feature distributions: winners vs losers
# ══════════════════════════════════════════════════════════════════════════════

def section_a(df):
    print('\n=== A. Feature distributions: winners vs losers ===')

    numeric_features = [
        'price_ch_5m', 'price_ch_1h', 'age_minutes', 'vl_ratio',
        'buy_pct_5m', 'buy_pct_1h', 'vol_trend_pct', 'txn_accel',
        'liq_mcap_ratio', 'net_txn_1h', 'vol5m_proj_ratio', 'sell_pressure_5m'
    ]

    fig, axes = plt.subplots(4, 3, figsize=(15, 16))
    axes = axes.flatten()

    winners = df[df['target'] == 1]
    losers  = df[df['target'] == 0]

    for i, feat in enumerate(numeric_features):
        ax = axes[i]
        w = winners[feat].dropna()
        l = losers[feat].dropna()

        # clip extreme outliers for display
        lo, hi = np.percentile(df[feat].dropna(), [2, 98])
        w_c = w.clip(lo, hi)
        l_c = l.clip(lo, hi)

        ax.hist(l_c, bins=40, alpha=0.55, color='#ff6b6b', density=True, label='loser')
        ax.hist(w_c, bins=40, alpha=0.55, color='#50c87a', density=True, label='winner')

        # KS test
        ks_stat, ks_p = stats.ks_2samp(w.dropna(), l.dropna())
        ax.set_title(f'{feat}\nKS={ks_stat:.3f}  p={ks_p:.3f}', fontsize=9)
        ax.legend(fontsize=7)
        ax.set_yticks([])

    plt.suptitle('Feature Distributions: Winners vs Losers (2–98th pct clip)', y=1.01, fontsize=13)
    plt.tight_layout()
    savefig('A_feature_distributions.png')

    # Categorical feature win rates
    cat_feats = ['chain', 'micro_trend', 'vol_trend', 'dex']
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    axes = axes.flatten()

    for i, feat in enumerate(cat_feats):
        ax = axes[i]
        stats_df = df.groupby(feat.replace('category', ''), observed=True).agg(
            n=('target', 'count'),
            win_rate=('target', 'mean'),
            avg_ret=('outcome_pct', 'mean')
        ).reset_index().sort_values('win_rate', ascending=False)
        stats_df = stats_df[stats_df['n'] >= 10]

        colors = ['#50c87a' if w > df['target'].mean() else '#ff6b6b'
                  for w in stats_df['win_rate']]
        bars = ax.barh(stats_df[feat].astype(str), stats_df['win_rate'] * 100,
                       color=colors)
        ax.axvline(df['target'].mean() * 100, color='#aaa', ls='--', lw=1.5)

        for bar, (_, row) in zip(bars, stats_df.iterrows()):
            ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
                    f"n={row['n']:,}  avg={row['avg_ret']:+.1f}%",
                    va='center', fontsize=8, color='#aaa')

        ax.set_title(f'{feat} — win rate (dashed=base rate)')
        ax.set_xlabel('Win rate (%)')

    plt.tight_layout()
    savefig('A_categorical_win_rates.png')
    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# SECTION B — Correlation heatmap + feature vs outcome
# ══════════════════════════════════════════════════════════════════════════════

def section_b(df):
    print('\n=== B. Correlation with outcome ===')

    num_cols = [c for c in FEATURE_COLS if c not in CATEGORICALS]
    corr_rows = []
    for col in num_cols:
        sub = df[[col, 'outcome_pct', 'target']].dropna()
        if len(sub) < 100: continue
        spear, sp = stats.spearmanr(sub[col], sub['outcome_pct'])
        point, pp = stats.pointbiserialr(sub[col], sub['target'])
        corr_rows.append(dict(feature=col, spearman_r=spear, spearman_p=sp,
                              pointbiserial=point, pb_p=pp))

    corr_df = pd.DataFrame(corr_rows).sort_values('spearman_r', key=abs, ascending=False)

    fig, axes = plt.subplots(1, 2, figsize=(15, 8))

    # Spearman with outcome_pct
    colors = ['#50c87a' if v > 0 else '#ff6b6b' for v in corr_df['spearman_r']]
    axes[0].barh(corr_df['feature'], corr_df['spearman_r'], color=colors)
    axes[0].axvline(0, color='#aaa', lw=1)
    axes[0].set_title('Spearman r vs outcome_pct')
    axes[0].set_xlabel('Correlation')

    # Point biserial with target (win/loss)
    corr_df2 = corr_df.sort_values('pointbiserial', key=abs, ascending=False)
    colors2 = ['#50c87a' if v > 0 else '#ff6b6b' for v in corr_df2['pointbiserial']]
    axes[1].barh(corr_df2['feature'], corr_df2['pointbiserial'], color=colors2)
    axes[1].axvline(0, color='#aaa', lw=1)
    axes[1].set_title('Point-biserial r vs target (win/loss)')
    axes[1].set_xlabel('Correlation')

    plt.suptitle('Feature Correlations with Outcome', fontsize=13)
    plt.tight_layout()
    savefig('B_correlations.png')

    print('\n  Top correlates with outcome_pct (Spearman):')
    for _, r in corr_df.head(10).iterrows():
        sig = '***' if r['spearman_p'] < 0.001 else '**' if r['spearman_p'] < 0.01 else '*' if r['spearman_p'] < 0.05 else ''
        print(f"    {r['feature']:28s} r={r['spearman_r']:+.3f}  p={r['spearman_p']:.4f} {sig}")

    # Numeric feature correlation matrix (top 15 by abs correlation with target)
    top_feats = corr_df.head(15)['feature'].tolist()
    corr_matrix = df[top_feats + ['outcome_pct']].corr(method='spearman')

    fig, ax = plt.subplots(figsize=(12, 10))
    mask = np.triu(np.ones_like(corr_matrix, dtype=bool))
    sns.heatmap(corr_matrix, mask=mask, annot=True, fmt='.2f', cmap='RdYlGn',
                center=0, ax=ax, linewidths=0.5, annot_kws={'size': 8})
    ax.set_title('Feature Correlation Matrix (Spearman, top 15 by |r| with outcome)')
    plt.tight_layout()
    savefig('B_correlation_matrix.png')
    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# SECTION C — Regularized model + token-grouped CV
# ══════════════════════════════════════════════════════════════════════════════

def section_c(df, train, val):
    print('\n=== C. Regularized LightGBM + token-grouped CV ===')

    # Token-grouped 5-fold CV
    print('  Running 5-fold token-grouped CV…')
    aucs = cv_auc(df, target_col='target', feature_cols=FEATURE_COLS)
    print(f'  CV AUC: {np.mean(aucs):.4f} ± {np.std(aucs):.4f}  folds={aucs}')

    # Train on train set, eval on val
    model = fit_model(train, 'target', FEATURE_COLS)
    proba_val = score_df(model, val)
    proba_train = score_df(model, train)

    auc_train = roc_auc_score(train['target'], proba_train)
    auc_val   = roc_auc_score(val['target'],   proba_val)
    print(f'  Train AUC={auc_train:.4f}  Val AUC={auc_val:.4f}  '
          f'(gap={auc_train - auc_val:.4f})')

    lt = lift_table(val['target'].values, proba_val, 'val')
    print('\n  Threshold | Precision | Recall | Lift  | N')
    for _, r in lt.iterrows():
        print(f"  {r['threshold']:.2f}      | {r['precision']*100:5.1f}%   | "
              f"{r['recall']*100:5.1f}% | {r['lift']:.2f}x | {r['n']:,}")

    # Score distribution plot
    val_scored = val.copy()
    val_scored['proba'] = proba_val

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    # Score distribution by outcome
    axes[0].hist(val_scored[val_scored['target']==0]['proba'], bins=40,
                 alpha=0.6, color='#ff6b6b', density=True, label='loser')
    axes[0].hist(val_scored[val_scored['target']==1]['proba'], bins=40,
                 alpha=0.6, color='#50c87a', density=True, label='winner')
    axes[0].set_xlabel('Model score')
    axes[0].set_ylabel('Density')
    axes[0].set_title(f'Score distribution (val AUC={auc_val:.3f})')
    axes[0].legend()

    # Precision vs threshold
    axes[1].plot(lt['threshold'], lt['precision']*100, 'o-', color='#4a9eff', label='Precision')
    axes[1].plot(lt['threshold'], lt['recall']*100, 's--', color='#ff9f43', label='Recall')
    axes[1].plot(lt['threshold'], lt['lift']*20, '^:', color='#50c87a', label='Lift ×20')
    axes[1].axhline(val['target'].mean()*100, color='#ff6b6b', lw=1, ls=':', label='Base rate')
    axes[1].set_xlabel('Threshold')
    axes[1].set_ylabel('%  (lift scaled ×20)')
    axes[1].set_title('Precision / Recall / Lift vs Threshold')
    axes[1].legend(fontsize=8)

    plt.tight_layout()
    savefig('C_model_scores.png')

    return model, val_scored


# ══════════════════════════════════════════════════════════════════════════════
# SECTION D — Chain-specific models
# ══════════════════════════════════════════════════════════════════════════════

def section_d(df, train, val):
    print('\n=== D. Chain-specific models ===')
    results = {}
    chain_models = {}

    for chain in ['base', 'solana']:
        tr = train[train['chain'] == chain]
        vl = val[val['chain'] == chain]
        if len(tr) < 100 or len(vl) < 30:
            print(f'  {chain}: insufficient data')
            continue

        # CV
        chain_df = df[df['chain'] == chain]
        aucs = cv_auc(chain_df, 'target', FEATURE_COLS)
        print(f'\n  {chain.upper()} (n_train={len(tr):,}, n_val={len(vl):,})')
        print(f'    CV AUC: {np.mean(aucs):.4f} ± {np.std(aucs):.4f}')

        m = fit_model(tr, 'target', FEATURE_COLS)
        proba = score_df(m, vl)
        auc = roc_auc_score(vl['target'], proba)
        ap  = average_precision_score(vl['target'], proba)
        base = vl['target'].mean()
        print(f'    Val AUC={auc:.4f}  AP={ap:.4f}  base={base*100:.1f}%')

        lt = lift_table(vl['target'].values, proba)
        print(f'    Threshold | Precision | Lift  | N')
        for _, r in lt.iterrows():
            print(f"    {r['threshold']:.2f}      | {r['precision']*100:5.1f}%   | {r['lift']:.2f}x | {r['n']:,}")

        # Top features
        imp = pd.DataFrame({'feature': FEATURE_COLS,
                            'importance': m.feature_importances_})
        imp = imp.sort_values('importance', ascending=False)
        print(f'    Top 8 features: {imp.head(8)["feature"].tolist()}')

        vl = vl.copy()
        vl['proba'] = proba
        results[chain] = (m, vl, lt, auc, base)
        chain_models[chain] = m

    # Comparison plot
    if len(results) == 2:
        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        for ax, (chain, (m, vl, lt, auc, base)) in zip(axes, results.items()):
            ax.hist(vl[vl['target']==0]['proba'], bins=30, alpha=0.6,
                    color='#ff6b6b', density=True, label='loser')
            ax.hist(vl[vl['target']==1]['proba'], bins=30, alpha=0.6,
                    color='#50c87a', density=True, label='winner')
            ax.set_title(f'{chain.upper()} — AUC={auc:.3f}  base={base*100:.1f}%')
            ax.set_xlabel('Model score')
            ax.legend()

        plt.suptitle('Chain-specific model score distributions', fontsize=13)
        plt.tight_layout()
        savefig('D_chain_models.png')

        # Feature importance comparison
        fig, axes = plt.subplots(1, 2, figsize=(15, 8))
        for ax, (chain, (m, _, _, _, _)) in zip(axes, results.items()):
            imp = pd.DataFrame({'feature': FEATURE_COLS,
                                'importance': m.feature_importances_})
            imp = imp.sort_values('importance', ascending=False).head(20)
            ax.barh(imp['feature'], imp['importance'],
                    color=CHAIN_COLORS.get(chain, '#4a9eff'))
            ax.set_title(f'{chain.upper()} — Top 20 features')
            ax.invert_yaxis()

        plt.tight_layout()
        savefig('D_chain_feature_importance.png')

    return chain_models


# ══════════════════════════════════════════════════════════════════════════════
# SECTION E — Alternative targets
# ══════════════════════════════════════════════════════════════════════════════

def section_e(df, train, val):
    print('\n=== E. Alternative targets (>0%, >2%, >5%, >10%) ===')

    targets = [
        ('target',      '>0%  (win/loss)',  0),
        ('target_2pct', '>2%',              2),
        ('target_5pct', '>5%',              5),
        ('target_10pct','>10%',             10),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    axes = axes.flatten()
    summary = []

    for i, (tcol, label, thresh) in enumerate(targets):
        aucs = cv_auc(df, tcol, FEATURE_COLS)
        m = fit_model(train, tcol, FEATURE_COLS)
        proba = score_df(m, val)
        auc = roc_auc_score(val[tcol], proba)
        base = val[tcol].mean()

        lt = lift_table(val[tcol].values, proba)
        best = lt[lt['lift'] == lt['lift'].max()].iloc[0] if len(lt) else {}
        summary.append(dict(target=label, cv_auc=np.mean(aucs), val_auc=auc,
                            base_rate=base,
                            best_lift=best.get('lift', 0),
                            best_thr=best.get('threshold', 0),
                            best_prec=best.get('precision', 0)))

        ax = axes[i]
        if len(lt):
            ax.plot(lt['threshold'], lt['lift'], 'o-', color='#4a9eff')
            ax.axhline(1.0, color='#ff6b6b', ls='--', lw=1.5)
            ax.set_title(f'Target {label}\nCV AUC={np.mean(aucs):.3f}  base={base*100:.1f}%')
            ax.set_xlabel('Threshold')
            ax.set_ylabel('Lift over base rate')

        print(f'  {label:12s}: CV AUC={np.mean(aucs):.4f}  val AUC={auc:.4f}  '
              f'base={base*100:.1f}%  best lift={best.get("lift",0):.2f}x @ {best.get("threshold",0):.2f}')

    plt.suptitle('Model lift for different return thresholds', fontsize=13)
    plt.tight_layout()
    savefig('E_alternative_targets.png')

    print('\n  Summary table:')
    print(pd.DataFrame(summary).to_string(index=False, float_format='{:.3f}'.format))
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# SECTION F — Partial dependence plots (top 8 features)
# ══════════════════════════════════════════════════════════════════════════════

def section_f(model, df):
    print('\n=== F. Partial dependence plots ===')

    # Get top numeric features by importance
    imp = pd.DataFrame({'feature': FEATURE_COLS,
                        'importance': model.feature_importances_})
    top_numeric = (imp[~imp['feature'].isin(CATEGORICALS)]
                   .sort_values('importance', ascending=False)
                   .head(8)['feature'].tolist())

    print(f'  PDP features: {top_numeric}')

    # Encode categoricals for sklearn PDP (LightGBM native doesn't expose PDP directly)
    # Use a small sklearn-compatible wrapper via lgb's predict
    sample = df.sample(min(3000, len(df)), random_state=42)

    fig, axes = plt.subplots(2, 4, figsize=(16, 8))
    axes = axes.flatten()

    for i, feat in enumerate(top_numeric):
        ax = axes[i]
        sub = sample.dropna(subset=[feat])
        vals = np.linspace(np.percentile(sub[feat], 5),
                           np.percentile(sub[feat], 95), 50)
        mean_preds = []
        for v in vals:
            modified = sub.copy()
            modified[feat] = v
            preds = model.predict_proba(modified[FEATURE_COLS])[:, 1]
            mean_preds.append(preds.mean())

        ax.plot(vals, np.array(mean_preds) * 100, color='#4a9eff', lw=2)
        ax.axhline(df['target'].mean() * 100, color='#ff6b6b', ls='--', lw=1.5)
        ax.set_title(feat, fontsize=9)
        ax.set_xlabel(feat, fontsize=8)
        ax.set_ylabel('Predicted win %', fontsize=8)

    plt.suptitle('Partial Dependence Plots — how each feature shifts win probability\n'
                 '(all other features at observed values, dashed=base rate)', fontsize=11)
    plt.tight_layout()
    savefig('F_partial_dependence.png')
    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# SECTION G — Decision tree: simple interpretable rules
# ══════════════════════════════════════════════════════════════════════════════

def section_g(df, train, val):
    print('\n=== G. Decision tree — interpretable rules ===')

    # Encode categoricals for sklearn
    df_enc = df.copy()
    enc = OrdinalEncoder(handle_unknown='use_encoded_value', unknown_value=-1)
    df_enc[CATEGORICALS] = enc.fit_transform(df_enc[CATEGORICALS].astype(str))
    df_enc[FEATURE_COLS] = df_enc[FEATURE_COLS].fillna(-999)

    tr_enc = df_enc[df_enc['scanned_at'] < TRAIN_CUTOFF]
    vl_enc = df_enc[(df_enc['scanned_at'] >= TRAIN_CUTOFF) &
                    (~df_enc['token_address'].isin(set(tr_enc['token_address'])))]

    best_auc, best_depth = 0, 3
    for depth in [3, 4, 5, 6]:
        dt = DecisionTreeClassifier(max_depth=depth, min_samples_leaf=50, random_state=42)
        dt.fit(tr_enc[FEATURE_COLS], tr_enc['target'])
        proba = dt.predict_proba(vl_enc[FEATURE_COLS])[:, 1]
        auc = roc_auc_score(vl_enc['target'], proba)
        print(f'  depth={depth}: val AUC={auc:.4f}')
        if auc > best_auc:
            best_auc, best_depth = auc, depth

    dt_best = DecisionTreeClassifier(max_depth=best_depth, min_samples_leaf=50, random_state=42)
    dt_best.fit(tr_enc[FEATURE_COLS], tr_enc['target'])

    rules = export_text(dt_best, feature_names=FEATURE_COLS, max_depth=best_depth)
    print(f'\n  Best tree (depth={best_depth}, AUC={best_auc:.4f}):')
    print('\n'.join('    ' + l for l in rules.split('\n')[:80]))

    proba_val = dt_best.predict_proba(vl_enc[FEATURE_COLS])[:, 1]
    lt = lift_table(vl_enc['target'].values, proba_val)
    print('\n  Lift table (decision tree):')
    for _, r in lt.iterrows():
        print(f"    {r['threshold']:.2f}: precision={r['precision']*100:.1f}%  lift={r['lift']:.2f}x  n={r['n']:,}")

    # Feature importance from tree
    imp = pd.DataFrame({'feature': FEATURE_COLS,
                        'importance': dt_best.feature_importances_})
    imp = imp[imp['importance'] > 0].sort_values('importance', ascending=False)

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.barh(imp['feature'], imp['importance'], color='#b06cff')
    ax.set_title(f'Decision Tree Feature Importance (depth={best_depth}, AUC={best_auc:.3f})')
    ax.invert_yaxis()
    plt.tight_layout()
    savefig('G_decision_tree_importance.png')
    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# SECTION H — Segment-conditional analysis
# ══════════════════════════════════════════════════════════════════════════════

def section_h(model, val):
    print('\n=== H. Segment-conditional model performance ===')

    val = val.copy()
    val['proba'] = score_df(model, val)
    base_all = val['target'].mean()

    segments = {
        'micro_trend':  val['micro_trend'].astype(str).unique(),
        'vol_trend':    val['vol_trend'].astype(str).unique(),
        'vl_bucket':    ['0-2x','2-4x','4-6x','6-8x','8x+'],
        'age_bucket':   ['15-30m','30-60m','60-90m'],
    }

    def vl_bucket(row):
        v = row['vl_ratio']
        if pd.isna(v): return 'null'
        if v <= 2: return '0-2x'
        if v <= 4: return '2-4x'
        if v <= 6: return '4-6x'
        if v <= 8: return '6-8x'
        return '8x+'

    def age_bucket(row):
        a = row['age_minutes']
        if a <= 30: return '15-30m'
        if a <= 60: return '30-60m'
        return '60-90m'

    val['vl_bucket']  = val.apply(vl_bucket, axis=1)
    val['age_bucket'] = val.apply(age_bucket, axis=1)

    rows = []
    for seg_col in ['chain', 'micro_trend', 'vol_trend', 'vl_bucket', 'age_bucket']:
        for seg_val in val[seg_col].astype(str).unique():
            sub = val[val[seg_col].astype(str) == seg_val]
            if len(sub) < 30: continue
            base = sub['target'].mean()
            proba = sub['proba']
            try:
                auc = roc_auc_score(sub['target'], proba)
            except:
                auc = np.nan
            # Precision at 0.55
            pred = (proba >= 0.55).astype(int)
            n55 = pred.sum()
            prec55 = precision_score(sub['target'], pred, zero_division=0) if n55 > 0 else np.nan
            lift55 = prec55 / base if base > 0 and not np.isnan(prec55) else np.nan
            rows.append(dict(segment=f'{seg_col}={seg_val}', n=len(sub),
                             base=base, auc=auc,
                             prec_at_55=prec55, lift_at_55=lift55))

    seg_df = pd.DataFrame(rows).sort_values('lift_at_55', ascending=False, na_position='last')
    print('\n  Segment performance (model precision@0.55 vs base rate):')
    print(f"  {'Segment':35s} {'n':>6} {'base':>6} {'AUC':>6} {'prec@.55':>9} {'lift':>6}")
    print('  ' + '-'*75)
    for _, r in seg_df.iterrows():
        flag = ' ★' if (not np.isnan(r['lift_at_55']) and r['lift_at_55'] >= 1.4) else ''
        print(f"  {r['segment']:35s} {r['n']:6,} {r['base']*100:5.1f}% {r['auc']:5.3f} "
              f"{r['prec_at_55']*100 if not np.isnan(r['prec_at_55']) else 0:8.1f}% "
              f"{r['lift_at_55'] if not np.isnan(r['lift_at_55']) else 0:5.2f}x{flag}")

    # Heatmap: win rate vs model score decile × chain
    val['score_decile'] = pd.qcut(val['proba'], q=5,
                                   labels=['D1\n(low)','D2','D3','D4','D5\n(high)'])
    pivot = val.pivot_table(values='target', index='chain',
                             columns='score_decile', aggfunc='mean', observed=True)

    fig, ax = plt.subplots(figsize=(10, 4))
    sns.heatmap(pivot * 100, annot=True, fmt='.1f', cmap='RdYlGn', center=42,
                ax=ax, linewidths=1)
    ax.set_title('Win rate (%) by chain × model score quintile')
    ax.set_xlabel('Score quintile')
    plt.tight_layout()
    savefig('H_segment_heatmap.png')
    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# SECTION I — Time stability: daily rolling AUC
# ══════════════════════════════════════════════════════════════════════════════

def section_i(model, df):
    print('\n=== I. Time stability — daily AUC ===')

    df = df.copy()
    df['proba'] = score_df(model, df)
    df['date'] = pd.to_datetime(df['scanned_at']).dt.date

    daily = []
    for date, grp in df.groupby('date'):
        if len(grp) < 30: continue
        try:
            auc = roc_auc_score(grp['target'], grp['proba'])
            base = grp['target'].mean()
            prec = grp[grp['proba'] >= 0.60]['target'].mean() if (grp['proba'] >= 0.60).sum() > 0 else np.nan
            n60  = (grp['proba'] >= 0.60).sum()
            daily.append(dict(date=date, auc=auc, base=base, prec_at_60=prec, n=len(grp), n60=n60))
        except Exception as e:
            pass

    daily_df = pd.DataFrame(daily)
    print(f'\n  {"Date":12s} {"n":>5} {"base":>6} {"AUC":>6} {"prec@0.60":>10} {"n@0.60":>7}')
    print('  ' + '-'*55)
    for _, r in daily_df.iterrows():
        cutoff_marker = ' ← val start' if str(r['date']) == TRAIN_CUTOFF else ''
        print(f"  {str(r['date']):12s} {r['n']:5,} {r['base']*100:5.1f}% {r['auc']:5.3f} "
              f"{r['prec_at_60']*100 if not np.isnan(r['prec_at_60']) else 0:9.1f}% "
              f"{r['n60']:6,}{cutoff_marker}")

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    dates = [str(r['date']) for _, r in daily_df.iterrows()]
    axes[0].plot(dates, daily_df['auc'], 'o-', color='#4a9eff', label='AUC')
    axes[0].axhline(0.5, color='#aaa', ls=':', lw=1)
    axes[0].axvline(TRAIN_CUTOFF, color='#ff9f43', ls='--', lw=1.5, label='train/val split')
    axes[0].set_ylabel('AUC')
    axes[0].set_title('Daily model AUC over time')
    axes[0].legend()

    axes[1].plot(dates, daily_df['base']*100, 's-', color='#ff6b6b', label='Base rate')
    axes[1].plot(dates, daily_df['prec_at_60']*100, '^-', color='#50c87a', label='Precision @0.60')
    axes[1].axvline(TRAIN_CUTOFF, color='#ff9f43', ls='--', lw=1.5)
    axes[1].set_ylabel('%')
    axes[1].set_title('Base rate vs precision @0.60 threshold')
    axes[1].legend()

    plt.xticks(rotation=30, ha='right')
    plt.tight_layout()
    savefig('I_time_stability.png')
    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# SECTION J — Top/bottom scorer deep dive
# ══════════════════════════════════════════════════════════════════════════════

def section_j(model, val, df):
    print('\n=== J. Top/bottom scorer deep dive ===')

    val = val.copy()
    val['proba'] = score_df(model, val)

    top    = val.nlargest(200, 'proba')
    bottom = val.nsmallest(200, 'proba')
    mid    = val[(val['proba'] >= 0.45) & (val['proba'] <= 0.55)]

    for label, sub in [('TOP 200 (high score)', top),
                        ('BOTTOM 200 (low score)', bottom),
                        ('MID 200 (score 0.45–0.55)', mid)]:
        n = len(sub)
        if n == 0: continue
        print(f'\n  {label}  (n={n})')
        print(f'    Win rate      : {sub["target"].mean()*100:.1f}%')
        print(f'    Avg outcome   : {sub["outcome_pct"].mean():+.2f}%')
        print(f'    Chain split   : {sub["chain"].value_counts().to_dict()}')
        print(f'    Micro_trend   : {sub["micro_trend"].astype(str).value_counts().head(4).to_dict()}')
        print(f'    Vol_trend     : {sub["vol_trend"].astype(str).value_counts().head(3).to_dict()}')
        print(f'    Avg age       : {sub["age_minutes"].mean():.1f}m')
        print(f'    Avg vl_ratio  : {sub["vl_ratio"].mean():.2f}x')
        print(f'    Avg price_ch_5m: {sub["price_ch_5m"].mean():+.2f}%')
        print(f'    Avg buy_pct_5m: {sub["buy_pct_5m"].mean():.1f}%')

    # Scatter: score vs actual outcome
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, chain in zip(axes, ['base', 'solana']):
        sub = val[val['chain'] == chain].sample(min(1500, len(val[val['chain']==chain])),
                                                 random_state=42)
        colors = ['#50c87a' if t else '#ff6b6b' for t in sub['target']]
        ax.scatter(sub['proba'], sub['outcome_pct'].clip(-30, 50),
                   c=colors, alpha=0.35, s=8)
        ax.axhline(0, color='#aaa', lw=1)
        ax.axvline(0.5, color='#aaa', lw=1)
        ax.set_title(f'{chain.upper()} — score vs actual 5m outcome')
        ax.set_xlabel('Model score')
        ax.set_ylabel('outcome_pct (clipped ±30/50)')

    plt.tight_layout()
    savefig('J_score_vs_outcome.png')

    # Outcome distribution by score quintile
    val['score_q'] = pd.qcut(val['proba'], q=5,
                              labels=['Q1 (lowest)', 'Q2', 'Q3', 'Q4', 'Q5 (highest)'])
    qstats = val.groupby('score_q', observed=True).agg(
        n=('target', 'count'),
        win_pct=('target', 'mean'),
        avg_ret=('outcome_pct', 'mean'),
        p25_ret=('outcome_pct', lambda x: x.quantile(0.25)),
        p75_ret=('outcome_pct', lambda x: x.quantile(0.75)),
        median_ret=('outcome_pct', 'median'),
    ).reset_index()

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    x = range(len(qstats))
    axes[0].bar(x, qstats['win_pct']*100, color='#4a9eff', alpha=0.8)
    axes[0].axhline(val['target'].mean()*100, color='#ff6b6b', ls='--', lw=1.5, label='Base rate')
    axes[0].set_xticks(x); axes[0].set_xticklabels(qstats['score_q'], rotation=15)
    axes[0].set_ylabel('Win rate (%)'); axes[0].set_title('Win rate by score quintile')
    axes[0].legend()

    axes[1].bar(x, qstats['avg_ret'], color='#50c87a', alpha=0.8, label='Mean return')
    axes[1].plot(x, qstats['median_ret'], 'o--', color='#ff9f43', label='Median return')
    axes[1].axhline(0, color='#aaa', lw=1)
    axes[1].set_xticks(x); axes[1].set_xticklabels(qstats['score_q'], rotation=15)
    axes[1].set_ylabel('Return (%)'); axes[1].set_title('Avg/Median 5m return by score quintile')
    axes[1].legend()

    plt.tight_layout()
    savefig('J_score_quintile_outcomes.png')

    print('\n  Score quintile summary:')
    for _, r in qstats.iterrows():
        print(f"    {r['score_q']:15s}: n={r['n']:,}  win={r['win_pct']*100:.1f}%  "
              f"avg={r['avg_ret']:+.2f}%  median={r['median_ret']:+.2f}%  "
              f"IQR=[{r['p25_ret']:+.1f}%, {r['p75_ret']:+.1f}%]")
    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# SECTION K — SHAP deep dive
# ══════════════════════════════════════════════════════════════════════════════

def section_k(model, val):
    print('\n=== K. SHAP deep dive ===')
    sample = val.sample(min(2000, len(val)), random_state=42)
    X = sample[FEATURE_COLS]

    explainer = shap.TreeExplainer(model)
    sv = explainer(X)

    # Summary bar (mean |SHAP|)
    fig, ax = plt.subplots(figsize=(10, 8))
    shap.summary_plot(sv, X, plot_type='bar', max_display=20, show=False)
    plt.title('Mean |SHAP| — feature importance')
    plt.tight_layout()
    savefig('K_shap_bar.png')

    # Beeswarm
    fig, ax = plt.subplots(figsize=(10, 9))
    shap.summary_plot(sv, X, max_display=20, show=False)
    plt.title('SHAP beeswarm — direction & magnitude of feature effects')
    plt.tight_layout()
    savefig('K_shap_beeswarm.png')

    # Per-chain SHAP comparison: which features drive each chain differently?
    print('\n  Top SHAP features by chain:')
    for chain in ['base', 'solana']:
        sub = val[val['chain'] == chain].sample(min(800, (val['chain']==chain).sum()),
                                                 random_state=42)
        Xs = sub[FEATURE_COLS]
        sv_chain = explainer(Xs)
        mean_abs = np.abs(sv_chain.values).mean(axis=0)
        top_idx = np.argsort(mean_abs)[::-1][:8]
        top_feats = [(FEATURE_COLS[i], mean_abs[i]) for i in top_idx]
        print(f'  {chain.upper()}: {[f for f,_ in top_feats]}')

    print('  Done.')


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print('DEX Scanner — Deep Dive Analysis')
    print('='*60)

    # Load & engineer
    raw = load()
    df  = engineer(raw)
    train, val = split(df)

    print(f'\nTrain: {len(train):,} rows  {train["token_address"].nunique():,} tokens  '
          f'win={train["target"].mean()*100:.1f}%')
    print(f'Val:   {len(val):,} rows  {val["token_address"].nunique():,} tokens  '
          f'win={val["target"].mean()*100:.1f}%')

    # A — distributions
    section_a(df)

    # B — correlations
    section_b(df)

    # C — regularized model
    model, val_scored = section_c(df, train, val)

    # D — chain-specific
    chain_models = section_d(df, train, val)

    # E — alternative targets
    section_e(df, train, val)

    # F — partial dependence (use full-dataset model from C)
    section_f(model, df)

    # G — decision tree
    section_g(df, train, val)

    # H — segment analysis
    section_h(model, val_scored)

    # I — time stability
    section_i(model, df)

    # J — top/bottom scorer deep dive
    section_j(model, val_scored, df)

    # K — SHAP deep dive
    section_k(model, val_scored)

    print(f'\n{"="*60}')
    print(f'All figures saved to analysis/{FIG_DIR}/')
    print('='*60)


if __name__ == '__main__':
    main()

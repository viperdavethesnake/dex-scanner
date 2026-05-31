"""
DEX Scanner — Full ML Analysis (2026-05-31)
==========================================
Trains LightGBM classifiers on ALL available collector columns,
including sparse GoPlus + Birdeye enrichment (with missingness flags).

Runs per chain (base / solana) × 4 target thresholds:
  any_gain   : outcome_pct > 0
  good       : outcome_pct >= 5
  strong     : outcome_pct >= 10
  moonshot   : outcome_pct >= 20

Outputs:
  analysis/figures/ml_full/   — plots
  docs/ml-findings-YYYY-MM-DD.md  — findings report
"""

import os, sys, warnings, io
from datetime import date
warnings.filterwarnings('ignore')

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

from sklearn.metrics import roc_auc_score, average_precision_score, precision_score, recall_score
from sklearn.model_selection import TimeSeriesSplit

# ── Config ────────────────────────────────────────────────────────────────────

DB = dict(host='localhost', port=5434, user='collector',
          password='collector', dbname='collector_signals')

AGE_MIN, AGE_MAX = 15, 90

TARGETS = {
    'any_gain':  ('outcome_pct > 0',   lambda s: s > 0),
    'good':      ('outcome_pct >= 5',  lambda s: s >= 5),
    'strong':    ('outcome_pct >= 10', lambda s: s >= 10),
    'moonshot':  ('outcome_pct >= 20', lambda s: s >= 20),
}

# Sparse GoPlus columns — add has_goplus + per-col _missing flags
GOPLUS_COLS = [
    'top1_pct', 'top5_pct', 'top10_pct', 'holder_count_gp',
    'creator_pct', 'creator_balance', 'lp_holder_count', 'lp_locked_pct',
    'buy_tax', 'sell_tax', 'is_honeypot_gp', 'is_blacklisted',
    'is_mintable', 'hidden_owner', 'can_take_back_ownership',
    'owner_change_balance', 'honeypot_with_same_creator',
    'is_proxy', 'is_open_source', 'transfer_pausable',
    'trading_cooldown', 'anti_whale_modifiable', 'slippage_modifiable',
]

# Sparse Birdeye columns
BIRDEYE_COLS = [
    'unique_traders_1h', 'net_inflow_usd', 'unique_traders_30m',
    'unique_traders_24h', 'buy_volume_1h_usd', 'sell_volume_1h_usd',
    'volume_24h_usd', 'buy_volume_24h_usd', 'sell_volume_24h_usd',
    'trade_count_1h', 'trade_count_24h', 'holder_count_birdeye', 'market_count',
]

CATEGORICAL = ['dex', 'micro_trend', 'vol_trend']

DROP_COLS = {
    'id', 'token_address', 'pair_address', 'symbol', 'name',
    'scanned_at', 'pair_created_at', 'price_usd', 'price_at_5m',
    'outcome_pct', 'chain', 'birdeye_enriched', 'goplus_enriched',
    'goplus_found_in_db', 'last_trade_unix_ts',
}

FIGDIR = 'figures/ml_full'
os.makedirs(FIGDIR, exist_ok=True)

report_lines = []

def rpt(*args, **kwargs):
    line = ' '.join(str(a) for a in args)
    print(line, **kwargs)
    report_lines.append(line)

# ── Data ──────────────────────────────────────────────────────────────────────

def load_data():
    rpt("\n=== Loading data ===")
    sql = """
    SELECT *
    FROM raw_signals
    WHERE outcome_pct IS NOT NULL
      AND age_minutes >= 15
      AND age_minutes <= 90
    ORDER BY scanned_at
    """
    conn = psycopg2.connect(**DB)
    df = pd.read_sql(sql, conn)
    conn.close()
    rpt(f"Rows: {len(df):,}  |  Date range: {df['scanned_at'].min().date()} → {df['scanned_at'].max().date()}")
    rpt(f"Chain split: {df['chain'].value_counts().to_dict()}")
    return df


def fill_rates(df):
    rpt("\n=== Column fill rates ===")
    cols = [c for c in df.columns if c not in DROP_COLS and c != 'outcome_pct']
    for c in cols:
        n = df[c].notna().sum()
        pct = n / len(df) * 100
        if pct < 99.9:
            rpt(f"  {c:40s} {pct:5.1f}%  ({n:,}/{len(df):,})")

# ── Feature engineering ───────────────────────────────────────────────────────

def engineer(df):
    df = df.copy()

    def sdiv(a, b):
        return np.where((b.notna()) & (b > 0), a / b, np.nan)

    # Core derived features
    df['vol5m_1h_ratio']   = sdiv(df['volume_5m'] * 12, df['volume_1h'])
    df['vol1h_6h_ratio']   = sdiv(df['volume_1h'] * 6,  df['volume_6h'])
    df['liq_mcap_ratio']   = sdiv(df['liquidity_usd'],   df['market_cap'])
    df['net_txn_5m']       = df['buys_5m']  - df['sells_5m']
    df['net_txn_1h']       = df['buys_1h']  - df['sells_1h']
    df['txn_accel']        = sdiv(df['buys_5m'] * 12,   df['buys_1h'])
    df['sell_pressure_5m'] = df['sells_5m'] / (df['buys_5m'] + df['sells_5m']).clip(lower=1)
    df['momentum_score']   = df['price_ch_5m'] * df['vol5m_1h_ratio'].clip(upper=10)

    for col in ['liquidity_usd', 'market_cap', 'volume_5m', 'volume_1h', 'volume_6h']:
        df[f'log_{col}'] = np.log1p(df[col].fillna(0))

    # Birdeye derived (only meaningful when enriched)
    df['buy_vol_ratio_1h'] = sdiv(df['buy_volume_1h_usd'], df['volume_1h'])
    df['net_inflow_pct']   = sdiv(df['net_inflow_usd'],    df['liquidity_usd'])
    df['log_net_inflow']   = np.log1p(df['net_inflow_usd'].clip(lower=0).fillna(0))

    # GoPlus derived
    df['whale_conc']       = df['top10_pct'].fillna(np.nan)
    df['tax_sum']          = df['buy_tax'].fillna(0) + df['sell_tax'].fillna(0)
    df['lp_risk']          = (df['lp_locked_pct'].fillna(0) < 50).astype(float)

    # Missingness indicator features — carry signal even when sparse
    df['has_goplus']   = df['goplus_enriched'].fillna(False).astype(int)
    df['has_birdeye']  = df['birdeye_enriched'].fillna(False).astype(int)

    for col in GOPLUS_COLS:
        if col in df.columns:
            df[f'_miss_{col}'] = df[col].isna().astype(int)

    for col in BIRDEYE_COLS:
        if col in df.columns:
            df[f'_miss_{col}'] = df[col].isna().astype(int)

    # Hour of day (time-of-day effects in meme markets)
    df['hour_utc'] = df['scanned_at'].dt.hour

    # Encode categoricals
    for col in CATEGORICAL:
        df[col] = df[col].fillna('unknown').astype('category')

    return df


def feature_cols(df):
    return [c for c in df.columns if c not in DROP_COLS
            and not c.startswith('target_')
            and df[c].dtype != object]


# ── Train / evaluate ──────────────────────────────────────────────────────────

def train_lgbm(X_tr, y_tr, X_val, y_val, cats):
    pos_w = max((y_tr == 0).sum() / max((y_tr == 1).sum(), 1), 1.0)
    params = dict(
        objective='binary', metric='auc',
        n_estimators=800, learning_rate=0.04,
        num_leaves=63, min_child_samples=20,
        feature_fraction=0.75, bagging_fraction=0.8, bagging_freq=5,
        scale_pos_weight=pos_w,
        verbosity=-1, random_state=42,
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_tr, y_tr,
        categorical_feature=cats,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(60, verbose=False),
                   lgb.log_evaluation(-1)],
    )
    return model


def cv_auc(df, fcols, target_col, cats, n_splits=4):
    """Time-series CV — returns mean AUC across folds."""
    tscv = TimeSeriesSplit(n_splits=n_splits)
    aucs = []
    arr = df.sort_values('scanned_at')
    X = arr[fcols]
    y = arr[target_col]
    for tr_idx, val_idx in tscv.split(arr):
        X_tr, X_val = X.iloc[tr_idx], X.iloc[val_idx]
        y_tr, y_val = y.iloc[tr_idx], y.iloc[val_idx]
        if y_val.sum() < 5 or y_tr.sum() < 10:
            continue
        m = train_lgbm(X_tr, y_tr, X_val, y_val, cats)
        proba = m.predict_proba(X_val)[:, 1]
        try:
            aucs.append(roc_auc_score(y_val, proba))
        except Exception:
            pass
    return float(np.mean(aucs)) if aucs else np.nan


def precision_table(model, X_val, y_val, base_rate):
    proba = model.predict_proba(X_val)[:, 1]
    rows = []
    for thr in np.arange(0.40, 0.85, 0.05):
        pred = (proba >= thr).astype(int)
        n = pred.sum()
        if n == 0:
            break
        prec = precision_score(y_val, pred, zero_division=0)
        rec  = recall_score(y_val, pred, zero_division=0)
        lift = prec / base_rate if base_rate > 0 else 0
        rows.append(dict(threshold=round(thr, 2), precision=prec, recall=rec,
                         lift=lift, n_flagged=int(n)))
    return pd.DataFrame(rows)


# ── Plots ─────────────────────────────────────────────────────────────────────

def plot_importance(model, fcols, chain, target, top_n=30):
    imp = pd.DataFrame({'feature': fcols,
                        'gain': model.booster_.feature_importance(importance_type='gain')})
    imp = imp.sort_values('gain', ascending=False).head(top_n)

    fig, ax = plt.subplots(figsize=(10, 9))
    colors = ['#e05252' if f.startswith('_miss') else
              '#50c87a' if 'goplus' in f or f in GOPLUS_COLS else
              '#4a9eff' for f in imp['feature']]
    ax.barh(imp['feature'][::-1], imp['gain'][::-1], color=colors[::-1])
    ax.set_title(f'Feature Importance (gain) — {chain.upper()} / {target}', fontsize=12)
    ax.set_xlabel('Gain')
    plt.tight_layout()
    path = f'{FIGDIR}/importance_{chain}_{target}.png'
    plt.savefig(path, dpi=140); plt.close()
    return path


def plot_shap_summary(model, X_sample, chain, target, max_display=25):
    explainer = shap.TreeExplainer(model)
    sv = explainer(X_sample)
    fig, ax = plt.subplots(figsize=(10, 9))
    shap.summary_plot(sv, X_sample, max_display=max_display, show=False, plot_size=None)
    plt.title(f'SHAP — {chain.upper()} / {target}', fontsize=12)
    plt.tight_layout()
    path = f'{FIGDIR}/shap_{chain}_{target}.png'
    plt.savefig(path, dpi=140, bbox_inches='tight'); plt.close()
    return path


def plot_lift_curves(models_data, chain):
    """Overlay lift curves for all 4 targets on one chart."""
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = {'any_gain': '#aaaaaa', 'good': '#4a9eff',
              'strong': '#50c87a', 'moonshot': '#f5a623'}

    for target, (proba, y) in models_data.items():
        base = y.mean()
        df_lift = pd.DataFrame({'proba': proba, 'y': y})
        df_lift = df_lift.sort_values('proba', ascending=False).reset_index(drop=True)
        df_lift['cumprec'] = df_lift['y'].cumsum() / (np.arange(len(df_lift)) + 1)
        df_lift['pct_flagged'] = (np.arange(len(df_lift)) + 1) / len(df_lift) * 100
        ax.plot(df_lift['pct_flagged'], df_lift['cumprec'] * 100,
                color=colors[target], lw=2, label=f'{target} (base {base*100:.1f}%)')

    ax.set_xlabel('% tokens flagged (ranked by score, high→low)')
    ax.set_ylabel('Precision (%)')
    ax.set_title(f'Lift Curves — {chain.upper()}')
    ax.legend()
    ax.grid(alpha=0.3)
    plt.tight_layout()
    path = f'{FIGDIR}/lift_{chain}.png'
    plt.savefig(path, dpi=140); plt.close()
    return path


def plot_outcome_heatmap(df, chain):
    """Heatmap: micro_trend × vol_trend — mean outcome_pct."""
    sub = df[df['chain'] == chain].copy()
    sub['micro_trend_str'] = sub['micro_trend'].astype(str)
    sub['vol_trend_str']   = sub['vol_trend'].astype(str)
    piv = sub.pivot_table(values='outcome_pct', index='micro_trend_str',
                          columns='vol_trend_str', aggfunc='mean')
    fig, ax = plt.subplots(figsize=(7, 5))
    sns.heatmap(piv, annot=True, fmt='.1f', cmap='RdYlGn', center=0, ax=ax,
                linewidths=0.5, cbar_kws={'label': 'avg outcome_pct %'})
    ax.set_title(f'Avg outcome_pct by micro_trend × vol_trend — {chain.upper()}')
    plt.tight_layout()
    path = f'{FIGDIR}/heatmap_{chain}.png'
    plt.savefig(path, dpi=140); plt.close()
    return path


def plot_signal_distributions(df, chain, target_col):
    """Distribution of key signals split by target outcome."""
    sub = df[df['chain'] == chain].copy()
    cols_to_plot = ['vl_ratio', 'vol5m_1h_ratio', 'momentum_score',
                    'age_minutes', 'log_liquidity_usd', 'buy_pct_5m']
    cols_to_plot = [c for c in cols_to_plot if c in sub.columns]

    fig, axes = plt.subplots(2, 3, figsize=(14, 8))
    axes = axes.flatten()
    for i, col in enumerate(cols_to_plot):
        ax = axes[i]
        pos = sub[sub[target_col] == 1][col].dropna()
        neg = sub[sub[target_col] == 0][col].dropna()
        # Clip extremes for readability
        upper = sub[col].quantile(0.97) if sub[col].notna().any() else None
        lower = sub[col].quantile(0.03) if sub[col].notna().any() else None
        if upper is not None:
            pos = pos.clip(lower, upper); neg = neg.clip(lower, upper)
        ax.hist(neg, bins=40, alpha=0.55, color='#ff6b6b', density=True, label='miss/lose')
        ax.hist(pos, bins=40, alpha=0.55, color='#50c87a', density=True, label='win')
        ax.set_title(col)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.2)
    plt.suptitle(f'Signal distributions — {chain.upper()} / target={target_col}', fontsize=12)
    plt.tight_layout()
    path = f'{FIGDIR}/distributions_{chain}.png'
    plt.savefig(path, dpi=140); plt.close()
    return path


# ── Pattern mining ────────────────────────────────────────────────────────────

def mine_patterns(df, chain):
    """Top signal combinations by moonshot rate."""
    sub = df[df['chain'] == chain].copy()
    sub['moonshot'] = (sub['outcome_pct'] >= 20).astype(int)
    base_rate = sub['moonshot'].mean()

    # Discretise key signals
    sub['vl_bin'] = pd.cut(sub['vl_ratio'], bins=[0, 1, 2, 4, 8, np.inf],
                            labels=['<1', '1-2', '2-4', '4-8', '>8'])
    sub['mt'] = sub['micro_trend'].astype(str)
    sub['vt'] = sub['vol_trend'].astype(str)

    results = []
    for mt in sub['mt'].unique():
        for vt in sub['vt'].unique():
            for vl in sub['vl_bin'].cat.categories if hasattr(sub['vl_bin'], 'cat') else []:
                mask = (sub['mt'] == mt) & (sub['vt'] == vt) & (sub['vl_bin'] == vl)
                n = mask.sum()
                if n < 20:
                    continue
                moon = sub[mask]['moonshot'].mean()
                avg_out = sub[mask]['outcome_pct'].mean()
                results.append(dict(
                    micro_trend=mt, vol_trend=vt, vl_bin=str(vl),
                    n=n, moonshot_rate=moon, avg_outcome=avg_out,
                    lift=moon / base_rate if base_rate > 0 else 0
                ))

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values('moonshot_rate', ascending=False)


# ── Per-chain analysis ────────────────────────────────────────────────────────

def analyse_chain(df_all, chain):
    rpt(f"\n{'='*60}")
    rpt(f"CHAIN: {chain.upper()}")
    rpt(f"{'='*60}")

    df = df_all[df_all['chain'] == chain].copy()
    rpt(f"Rows: {len(df):,}")

    for tname, (_, tfn) in TARGETS.items():
        df[f'target_{tname}'] = tfn(df['outcome_pct']).astype(int)
        rate = df[f'target_{tname}'].mean()
        rpt(f"  {tname:10s}: {rate*100:.1f}% positive ({df[f'target_{tname}'].sum():,} rows)")

    fcols = feature_cols(df)
    cats  = [c for c in CATEGORICAL if c in fcols]
    rpt(f"Feature columns: {len(fcols)}")

    # Time split: last 3 days = val
    cutoff = df['scanned_at'].quantile(0.75)
    df_tr  = df[df['scanned_at'] <  cutoff].copy()
    df_val = df[df['scanned_at'] >= cutoff].copy()
    rpt(f"Train: {len(df_tr):,}  Val: {len(df_val):,}  (cutoff: {cutoff.date()})")

    models_lift_data = {}
    best_models = {}

    for tname, (tdesc, _) in TARGETS.items():
        tcol = f'target_{tname}'
        base_rate = df_val[tcol].mean()
        rpt(f"\n── Target: {tname} ({tdesc}) ──")
        rpt(f"   Val base rate: {base_rate*100:.1f}%  ({df_val[tcol].sum()} positives)")

        if df_tr[tcol].sum() < 30 or df_val[tcol].sum() < 10:
            rpt(f"   Skipping — not enough positives")
            continue

        # CV AUC
        cv_score = cv_auc(df_tr, fcols, tcol, cats, n_splits=4)
        rpt(f"   CV AUC (4-fold time-series): {cv_score:.4f}")

        # Final model on all train
        model = train_lgbm(
            df_tr[fcols], df_tr[tcol],
            df_val[fcols], df_val[tcol],
            cats
        )
        proba = model.predict_proba(df_val[fcols])[:, 1]
        try:
            val_auc = roc_auc_score(df_val[tcol], proba)
            val_ap  = average_precision_score(df_val[tcol], proba)
        except Exception:
            val_auc = val_ap = float('nan')

        rpt(f"   Val AUC: {val_auc:.4f}  |  Avg Precision: {val_ap:.4f}  (base AP: {base_rate:.4f})")

        ptable = precision_table(model, df_val[fcols], df_val[tcol], base_rate)
        rpt(f"\n   Threshold | Precision | Recall | Lift   | N flagged")
        rpt(f"   ----------|-----------|--------|--------|----------")
        for _, row in ptable.iterrows():
            rpt(f"   {row.threshold:.2f}      | {row.precision*100:5.1f}%    | "
                f"{row.recall*100:5.1f}%  | {row.lift:.2f}x  | {int(row.n_flagged):,}")

        models_lift_data[tname] = (proba, df_val[tcol].values)
        best_models[tname] = model

        # Plots
        plot_importance(model, fcols, chain, tname)

        # SHAP on moonshot/strong only (slow)
        if tname in ('moonshot', 'strong'):
            sample = df_val.sample(min(2000, len(df_val)), random_state=42)
            plot_shap_summary(model, sample[fcols], chain, tname)

        # Top 10 features by gain
        imp = pd.DataFrame({'feature': fcols,
                            'gain': model.booster_.feature_importance(importance_type='gain')})
        imp = imp.sort_values('gain', ascending=False).head(12)
        rpt(f"\n   Top 12 features ({tname}):")
        for _, r in imp.iterrows():
            tag = ' [SPARSE]' if r['feature'].startswith('_miss') or \
                  r['feature'] in GOPLUS_COLS or r['feature'] in BIRDEYE_COLS else ''
            rpt(f"     {r['feature']:35s} {r['gain']:,.0f}{tag}")

    # Lift curves overlay
    if models_lift_data:
        plot_lift_curves(models_lift_data, chain)

    # Signal distributions
    if 'target_strong' in df.columns:
        plot_signal_distributions(df, chain, 'target_strong')

    # Heatmap
    plot_outcome_heatmap(df_all, chain)

    # Pattern mining
    rpt(f"\n── Pattern mining (moonshot ≥20%) — {chain.upper()} ──")
    patterns = mine_patterns(df_all, chain)
    if not patterns.empty:
        top = patterns.head(15)
        rpt(f"{'micro_trend':15s} {'vol_trend':10s} {'vl_bin':8s} {'n':>6} {'moon%':>7} {'avg_out%':>9} {'lift':>6}")
        rpt('-' * 65)
        for _, row in top.iterrows():
            rpt(f"{row.micro_trend:15s} {row.vol_trend:10s} {row.vl_bin:8s} "
                f"{row.n:6,} {row.moonshot_rate*100:7.1f} {row.avg_outcome:9.2f} {row.lift:6.2f}x")

    return best_models


# ── Goplus flag analysis ──────────────────────────────────────────────────────

def analyse_goplus_flags(df_all):
    rpt(f"\n{'='*60}")
    rpt("GOPLUS FLAG ANALYSIS (enriched rows only)")
    rpt(f"{'='*60}")

    binary_flags = [
        'is_honeypot_gp', 'is_blacklisted', 'is_mintable', 'hidden_owner',
        'can_take_back_ownership', 'owner_change_balance', 'honeypot_with_same_creator',
        'is_proxy', 'is_open_source', 'transfer_pausable', 'trading_cooldown',
        'anti_whale_modifiable', 'slippage_modifiable',
    ]
    df = df_all[df_all['goplus_enriched'] == True].copy()
    rpt(f"GoPlus-enriched rows: {len(df):,}")

    rpt(f"\n{'Flag':40s} {'set%':>6}  {'avg_outcome when set':>20}  {'avg_outcome when 0':>18}")
    rpt('-' * 90)
    for flag in binary_flags:
        if flag not in df.columns:
            continue
        set_mask  = df[flag] == 1
        zero_mask = df[flag] == 0
        n_set  = set_mask.sum()
        n_zero = zero_mask.sum()
        if n_set < 5 or n_zero < 5:
            continue
        pct   = n_set / len(df) * 100
        avg_s = df[set_mask]['outcome_pct'].mean()
        avg_z = df[zero_mask]['outcome_pct'].mean()
        rpt(f"  {flag:38s} {pct:5.1f}%  {avg_s:+8.2f}%  (n={n_set:,})    {avg_z:+8.2f}%  (n={n_zero:,})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    rpt(f"DEX Scanner ML Analysis — {date.today()}")
    rpt("=" * 60)

    df_raw = load_data()
    fill_rates(df_raw)

    df = engineer(df_raw)

    # Outcome summary
    rpt("\n=== Outcome summary ===")
    for chain in ['base', 'solana']:
        sub = df[df['chain'] == chain]
        rpt(f"\n  {chain.upper()} (n={len(sub):,})")
        for tname, (tdesc, tfn) in TARGETS.items():
            r = tfn(sub['outcome_pct']).mean()
            rpt(f"    {tname:10s} ({tdesc}): {r*100:.1f}%")

    # GoPlus flag analysis
    analyse_goplus_flags(df)

    # Per-chain full analysis
    all_models = {}
    for chain in ['base', 'solana']:
        all_models[chain] = analyse_chain(df, chain)

    # Combined model (chain as feature, drop chain col from features)
    rpt(f"\n{'='*60}")
    rpt("COMBINED (base + solana)")
    rpt(f"{'='*60}")
    df_comb = df.copy()
    df_comb['chain_is_base'] = (df_comb['chain'] == 'base').astype(int)
    for tname, (tdesc, tfn) in TARGETS.items():
        df_comb[f'target_{tname}'] = tfn(df_comb['outcome_pct']).astype(int)
    fcols_comb = [c for c in feature_cols(df_comb) if c != 'chain'] + ['chain_is_base']
    fcols_comb = list(dict.fromkeys(fcols_comb))  # dedup
    cats_comb  = [c for c in CATEGORICAL if c in fcols_comb and c != 'chain']

    cutoff_comb = df_comb['scanned_at'].quantile(0.75)
    tr_comb     = df_comb[df_comb['scanned_at'] <  cutoff_comb]
    val_comb    = df_comb[df_comb['scanned_at'] >= cutoff_comb]

    for tname, (tdesc, _) in [('strong', TARGETS['strong']), ('moonshot', TARGETS['moonshot'])]:
        tcol     = f'target_{tname}'
        base_rate = val_comb[tcol].mean()
        rpt(f"\n── {tname} ({tdesc}) — combined ──")
        rpt(f"   Val base rate: {base_rate*100:.1f}%")
        if tr_comb[tcol].sum() < 30 or val_comb[tcol].sum() < 10:
            rpt("   Skipping — not enough positives"); continue
        cv_score = cv_auc(tr_comb, fcols_comb, tcol, cats_comb, n_splits=4)
        rpt(f"   CV AUC: {cv_score:.4f}")
        model = train_lgbm(tr_comb[fcols_comb], tr_comb[tcol],
                           val_comb[fcols_comb], val_comb[tcol], cats_comb)
        proba = model.predict_proba(val_comb[fcols_comb])[:, 1]
        try:
            rpt(f"   Val AUC: {roc_auc_score(val_comb[tcol], proba):.4f}")
        except Exception:
            pass

    # Write findings doc
    doc_path = f'../docs/ml-findings-{date.today()}.md'
    with open(doc_path, 'w') as f:
        f.write(f"# ML Findings — {date.today()}\n\n")
        f.write("Generated by `analysis/ml_full.py`.\n\n")
        f.write("```\n")
        f.write('\n'.join(report_lines))
        f.write("\n```\n")

    print(f"\n\nFindings written to: {doc_path}")
    print(f"Figures in: analysis/{FIGDIR}/")


if __name__ == '__main__':
    main()

"""
DEX Scanner — Model Export
===========================
Trains the LightGBM model using the same flow as ml.py, then serialises
three files to analysis/models/ for use by the dex-trader service:

    models/lgbm_base.txt      LightGBM native booster format
    models/feature_list.json  Ordered list of feature column names
    models/metadata.json      threshold, train_cutoff, val_auc, trained_at

Usage:
    cd /space/docker/containers/dex-scanner/analysis
    source venv/bin/activate
    python3 export_model.py [--train-cutoff YYYY-MM-DD] [--chain base|solana|all]

Canonical threshold: 0.70
  Source: docs/ML-FINDINGS.md — "First-entry ≥0.70 is the most practical
  strategy" (55% win rate, 2.50x profit factor, $11 max drawdown).
  NOTE: The shadow trader design doc (docs/decisions/SHADOW-TRADER-DESIGN.md §9)
  sets the default CONVICTION_THRESHOLD env var to 0.65. This is a conflict.
  The user was informed and must resolve it before Phase 3 go-live. This script
  uses 0.70 as the canonical value (matching ML-FINDINGS.md). The design doc env
  var default should be updated to match.
"""

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

import lightgbm as lgb
import numpy as np
import pandas as pd
import psycopg2
from sklearn.metrics import roc_auc_score

from features import engineer_features as _engineer_row

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR  = os.path.join(SCRIPT_DIR, "models")
BOOSTER_PATH  = os.path.join(MODELS_DIR, "lgbm_base.txt")
FEATURES_PATH = os.path.join(MODELS_DIR, "feature_list.json")
METADATA_PATH = os.path.join(MODELS_DIR, "metadata.json")

# ── Config (mirrors ml.py) ────────────────────────────────────────────────────

DB = dict(host="localhost", port=5434, user="collector",
          password="collector", dbname="collector_signals")

DEFAULT_TRAIN_CUTOFF = "2026-05-23"   # train on all data before this date
AGE_MIN, AGE_MAX     = 15, 90         # scanner window (minutes)
CANONICAL_THRESHOLD  = 0.70           # from ML-FINDINGS.md "First-entry ≥0.70"

CATEGORICAL_FEATURES = ["chain", "dex", "micro_trend", "vol_trend"]
DROP_COLS = [
    "token_address", "pair_address", "symbol", "name",
    "scanned_at", "pair_created_at", "price_usd",
    "price_at_5m", "outcome_pct", "id",
]


# ── Data ──────────────────────────────────────────────────────────────────────

def load_data(chain_filter=None):
    where = f"price_at_5m IS NOT NULL AND age_minutes >= {AGE_MIN} AND age_minutes <= {AGE_MAX}"
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
    FROM raw_signals WHERE {where} ORDER BY scanned_at
    """
    conn = psycopg2.connect(**DB)
    df   = pd.read_sql(sql, conn, parse_dates=["scanned_at"])
    conn.close()
    return df


def engineer_features(df):
    """Apply shared feature engineering to a DataFrame, returning enriched DataFrame.

    Delegates per-row computation to analysis/features.py (single source of truth).
    Categoricals and the binary target column are handled here after row expansion.
    """
    df = df.copy()
    df["target"] = (df["outcome_pct"] > 0).astype(int)

    # Apply shared dict-based engineer_features row-by-row
    rows = df.to_dict("records")
    enriched = pd.DataFrame([_engineer_row(r) for r in rows])

    # Restore index alignment (to_dict → DataFrame resets to 0-based)
    enriched.index = df.index

    # Categoricals (not handled in features.py — training-only concern)
    for col in CATEGORICAL_FEATURES:
        enriched[col] = enriched[col].fillna("unknown").astype("category")

    return enriched


def get_feature_cols(df):
    exclude = set(DROP_COLS) | {"target"}
    return [c for c in df.columns if c not in exclude]


# ── Train ──────────────────────────────────────────────────────────────────────

def train_model(train, feature_cols):
    X = train[feature_cols]
    y = train["target"]
    pos_weight = (y == 0).sum() / max((y == 1).sum(), 1)

    # Hyperparameters match backtest.py (the regularised, production-safe variant)
    model = lgb.LGBMClassifier(
        objective="binary",
        metric="auc",
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=20,
        min_child_samples=50,
        feature_fraction=0.7,
        bagging_fraction=0.8,
        bagging_freq=5,
        reg_lambda=5.0,
        reg_alpha=0.1,
        scale_pos_weight=pos_weight,
        verbosity=-1,
        random_state=42,
    )
    model.fit(
        X, y,
        categorical_feature=[c for c in CATEGORICAL_FEATURES if c in feature_cols],
    )
    return model


# ── Export ──────────────────────────────────────────────────────────────────────

def export(model, feature_cols, val_auc, train_cutoff, cat_mappings=None):
    os.makedirs(MODELS_DIR, exist_ok=True)

    # 1. Booster (LightGBM native text format — portable, no pickle)
    model.booster_.save_model(BOOSTER_PATH)

    # 2. Feature list (ordered — trader must use this exact order)
    with open(FEATURES_PATH, "w") as f:
        json.dump(feature_cols, f, indent=2)

    # 3. Metadata
    metadata = {
        "threshold":     CANONICAL_THRESHOLD,
        "train_cutoff":  train_cutoff,
        "val_auc":       round(float(val_auc), 4),
        "trained_at":    datetime.now(timezone.utc).isoformat(),
        "n_features":    len(feature_cols),
        # Training-time category vocabularies — required for correct inference.
        # LightGBM uses integer codes internally; the code for a given string
        # depends on the category list order.  Without pinning this at training
        # time, a single-row inference DataFrame would derive its own codes and
        # silently mis-score every signal.
        "categorical_mappings": cat_mappings or {},
        "note": (
            "Threshold 0.70 = ML-FINDINGS.md canonical (First-entry >=0.70, 2.50x PF). "
            "SHADOW-TRADER-DESIGN.md §9 defaults CONVICTION_THRESHOLD to 0.65 — "
            "conflict to resolve before Phase 3 go-live."
        ),
    }
    with open(METADATA_PATH, "w") as f:
        json.dump(metadata, f, indent=2)

    return metadata


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export trained LightGBM model for dex-trader")
    parser.add_argument("--train-cutoff", default=DEFAULT_TRAIN_CUTOFF,
                        help="ISO date: train on data before this date (default: %(default)s)")
    parser.add_argument("--chain", default="base", choices=["base", "solana", "all"],
                        help="Chain filter (default: base — matches shadow trader scope)")
    args = parser.parse_args()

    chain_filter = None if args.chain == "all" else args.chain
    print(f"\n{'='*60}")
    print(f"DEX Scanner — Model Export")
    print(f"{'='*60}")
    print(f"Train cutoff : {args.train_cutoff}")
    print(f"Chain filter : {args.chain}")
    print(f"Threshold    : {CANONICAL_THRESHOLD}  (ML-FINDINGS.md canonical)")
    print(f"Output dir   : {MODELS_DIR}")
    print()

    # 1. Load
    print("Loading data…")
    df = load_data(chain_filter)
    print(f"  {len(df):,} rows | {df['token_address'].nunique():,} tokens | "
          f"{df['scanned_at'].min().date()} → {df['scanned_at'].max().date()}")

    # 2. Features
    df = engineer_features(df)
    feature_cols = get_feature_cols(df)
    print(f"  {len(feature_cols)} feature columns")

    # Capture training-time categorical vocabularies before the train/val split.
    # Pandas keeps the full category set even after subsetting, so these categories
    # reflect what LightGBM will see during model.fit().
    cat_mappings = {}
    for col in CATEGORICAL_FEATURES:
        if col in df.columns and hasattr(df[col], "cat"):
            cat_mappings[col] = list(df[col].cat.categories)
    print(f"  categorical mappings: { {k: len(v) for k, v in cat_mappings.items()} }")

    # 3. Split
    train = df[df["scanned_at"] < args.train_cutoff].copy()
    val   = df[df["scanned_at"] >= args.train_cutoff].copy()
    val   = val[~val["token_address"].isin(set(train["token_address"]))].copy()

    print(f"\nSplit at {args.train_cutoff}:")
    print(f"  Train : {len(train):,} rows  win={train['target'].mean()*100:.1f}%")
    print(f"  Val   : {len(val):,} rows   win={val['target'].mean()*100:.1f}%")

    if len(train) < 200 or len(val) < 50:
        print("\n⚠  Insufficient data for a meaningful split.")
        print(   "   Adjust --train-cutoff or --chain and retry.")
        sys.exit(1)

    # 4. Train
    print("\nTraining…")
    model = train_model(train, feature_cols)

    # 5. Validate
    val_proba = model.predict_proba(val[feature_cols])[:, 1]
    val_auc   = roc_auc_score(val["target"], val_proba)
    print(f"  Val AUC : {val_auc:.4f}")

    # Threshold performance on val
    print(f"\n  Threshold performance on val set:")
    print(f"  {'Thr':>5}  {'Prec':>7}  {'N':>6}")
    for thr in [0.60, 0.65, 0.70, 0.75]:
        pred  = (val_proba >= thr).astype(int)
        n     = pred.sum()
        if n == 0:
            continue
        prec  = val["target"][pred == 1].mean()
        print(f"  {thr:>5.2f}  {prec*100:>6.1f}%  {n:>6,}")

    # 6. Export
    print(f"\nExporting to {MODELS_DIR}/ …")
    metadata = export(model, feature_cols, val_auc, args.train_cutoff,
                      cat_mappings=cat_mappings)

    # 7. Verify sizes
    sizes = {
        "lgbm_base.txt":    os.path.getsize(BOOSTER_PATH),
        "feature_list.json": os.path.getsize(FEATURES_PATH),
        "metadata.json":    os.path.getsize(METADATA_PATH),
    }
    print(f"\n  {'File':25s} {'Bytes':>10}")
    print(f"  {'-'*37}")
    for fname, size in sizes.items():
        print(f"  {fname:25s} {size:>10,}")

    print(f"\n{'='*60}")
    print(f"✓ Export complete")
    print(f"  threshold    : {metadata['threshold']}")
    print(f"  val_auc      : {metadata['val_auc']}")
    print(f"  trained_at   : {metadata['trained_at']}")
    print(f"{'='*60}")
    print()
    print("⚠  THRESHOLD CONFLICT — user decision required:")
    print(f"   ML-FINDINGS.md canonical = 0.70 (first-entry, 2.50x PF, 55% win)")
    print(f"   SHADOW-TRADER-DESIGN.md default env var CONVICTION_THRESHOLD = 0.65")
    print(f"   metadata.json uses 0.70 (matching ML-FINDINGS).")
    print(f"   Update SHADOW-TRADER-DESIGN.md §9 and compose.yaml CONVICTION_THRESHOLD")
    print(f"   to 0.70 before Phase 3 go-live, or explicitly choose 0.65 for")
    print(f"   higher recall at lower precision.")
    print()


if __name__ == "__main__":
    main()

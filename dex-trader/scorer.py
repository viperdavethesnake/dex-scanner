"""LightGBM model serving with hot-reload on model file mtime change.

Threshold logic:
  SHADOW_MODE=true  → use CONVICTION_THRESHOLD_SHADOW (default 0.65)
  SHADOW_MODE=false → use CONVICTION_THRESHOLD_LIVE   (default 0.70)

conviction_band column:
  'live_eligible'  → score >= CONVICTION_THRESHOLD_LIVE (0.70)
  'shadow_only'    → score >= CONVICTION_THRESHOLD_SHADOW but < 0.70
"""
import json
import logging
import os
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

MODEL_PATH    = os.environ.get("MODEL_PATH",    "/models/lgbm_base.txt")
FEATURES_PATH = os.environ.get("FEATURES_PATH", "/models/feature_list.json")
METADATA_PATH = os.environ.get("METADATA_PATH", "/models/metadata.json")
RELOAD_CHECK_INTERVAL = 300   # seconds between mtime checks

SHADOW_MODE = os.environ.get("SHADOW_MODE", "true").lower() == "true"
CONVICTION_THRESHOLD_SHADOW = float(os.environ.get("CONVICTION_THRESHOLD_SHADOW", "0.65"))
CONVICTION_THRESHOLD_LIVE   = float(os.environ.get("CONVICTION_THRESHOLD_LIVE",   "0.70"))

THRESHOLD      = CONVICTION_THRESHOLD_SHADOW if SHADOW_MODE else CONVICTION_THRESHOLD_LIVE
LIVE_THRESHOLD = CONVICTION_THRESHOLD_LIVE   # always used for band classification

CATEGORICAL_FEATURES = ["chain", "dex", "micro_trend", "vol_trend"]


class Scorer:
    def __init__(self):
        self.booster      = None
        self.features: list[str] = []
        self.meta: dict   = {}
        self.mtime        = 0.0
        self.last_checked = 0.0

    def load(self) -> None:
        """Load booster + feature list + metadata from disk."""
        self.booster = lgb.Booster(model_file=MODEL_PATH)
        with open(FEATURES_PATH) as f:
            self.features = json.load(f)
        with open(METADATA_PATH) as f:
            self.meta = json.load(f)
        self.mtime        = os.path.getmtime(MODEL_PATH)
        self.last_checked = time.monotonic()
        log.info(
            "model loaded: version=%s auc=%.3f threshold=%.2f (%s mode) n_features=%d",
            self.meta.get("trained_at", "?"),
            self.meta.get("val_auc", 0),
            THRESHOLD,
            "shadow" if SHADOW_MODE else "live",
            len(self.features),
        )

    def maybe_reload(self) -> None:
        """Check model file mtime every RELOAD_CHECK_INTERVAL; reload if changed."""
        if time.monotonic() - self.last_checked < RELOAD_CHECK_INTERVAL:
            return
        self.last_checked = time.monotonic()
        try:
            new_mtime = os.path.getmtime(MODEL_PATH)
            if new_mtime != self.mtime:
                log.info("model file changed — reloading…")
                self.load()
        except FileNotFoundError:
            log.warning("model file missing at %s — keeping current booster", MODEL_PATH)

    def score(self, enriched_signal: dict) -> float:
        """
        Return p(win) ∈ [0,1].
        enriched_signal must already have derived features applied (engineer_features()).
        Missing columns are filled with NaN (LightGBM handles gracefully).

        Numeric values are explicitly cast to float here because psycopg2 returns
        NUMERIC DB columns as Decimal objects, which pandas infers as object dtype
        and LightGBM rejects.
        """
        row = {}
        for col in self.features:
            val = enriched_signal.get(col, np.nan)
            if col not in CATEGORICAL_FEATURES:
                try:
                    val = float(val) if val is not None else np.nan
                except (TypeError, ValueError):
                    val = np.nan
            row[col] = val
        X = pd.DataFrame([row])[self.features]
        for col in CATEGORICAL_FEATURES:
            if col in X.columns:
                X[col] = X[col].astype("category")
        return float(self.booster.predict(X)[0])

    @staticmethod
    def conviction_band(score: float) -> str:
        """'live_eligible' if score >= 0.70, else 'shadow_only'."""
        return "live_eligible" if score >= LIVE_THRESHOLD else "shadow_only"

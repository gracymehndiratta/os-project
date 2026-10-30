"""isolation.py — Isolation-forest ablation detector.

Review 1 (§6, "Lightweight and online models") cites isolation forest as a
lightweight but batch-oriented alternative whose "standard implementations
require retraining or modification for incremental learning"
[Liu, Ting & Zhou, ICDM 2008].

This module implements that alternative **as an ablation, not as part of the
proposed solution**.  It deliberately contains no poisoning control.  A single
global forest is refit periodically on a sliding window of full feature
vectors.  ``evaluation.VARIANTS["iforest"]`` runs it on the same scenarios so
the report can show what a conventional point-anomaly detector does there.

Why one global forest (and not one per application/context)?
------------------------------------------------------------
A per-key forest would see only ~30-60 samples per warm-up window, which is
too few to calibrate a 0.5 % alert tail and makes the score saturate.  One
global forest is the standard form and has enough samples immediately; the
trade-off is that it loses the per-application context that the proposed
solution is built around.

Score calibration
-----------------
``IsolationForest.score_samples`` returns the negative of the paper's anomaly
score (higher = more normal).  We negate it to obtain ``raw`` (higher = more
anomalous) and convert it to an **empirical tail probability** against the
reference window::

    rank = #{r in window : r <= raw}
    risk = (rank + 0.5) / (n + 1)

This caps the false-positive rate at roughly ``1/(n+1)`` per sample for any
given threshold (the standard way to use iForest) instead of trusting the
heavy right tail to a Gaussian fit.  ``d_squared`` is reported as the squared
standardized score so the alert log stays monotone in anomaly; it is **not**
a Mahalanobis ``D²``.

Explanations
------------
Isolation forest has no closed-form per-feature attribution, and occlusion
probes are unreliable once the score saturates (any far-out point hits the
same path-length ceiling).  The ablation therefore ranks alert features by
**robust departure** from the reference window (median / IQR), which is the
same explanation surface the Gaussian scorer exposes.  This is a property of
the feature vector, not of the forest's internal split structure, and is
documented as such.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import numpy as np

from .config import FEATURE_NAMES, N_FEATURES, Config
from .data_model import BaselineKey, FeatureContribution, ScoreResult
from .scoring import risk_level

# IQR → standard deviation for a normal distribution (robust scaling).
_IQR_TO_SIGMA = 1.349


class IsolationScorer:
    """Windowed global isolation-forest detector (ablation only)."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()
        self._cfg = self.config.isolation
        self._window: Deque[np.ndarray] = deque(maxlen=self._cfg.max_samples)
        self._model: Any = None
        self._ref: Optional[Dict[str, Any]] = None
        self._last_fit: int = -(10 ** 9)
        self._lock = threading.RLock()

    # -- observation ------------------------------------------------------
    def observe(self, key: BaselineKey, x: np.ndarray, tick: int) -> None:
        """Add one feature vector to the window and refit when due.

        ``key`` is accepted for interface parity with the per-key scorers but
        is deliberately ignored: this ablation uses one global forest.
        """
        x = np.asarray(x, dtype=float)
        with self._lock:
            self._window.append(x)
            due = tick - self._last_fit >= self._cfg.refit_every
            if len(self._window) >= self._cfg.min_fit and (self._model is None or due):
                self._fit(tick)

    def _fit(self, tick: int) -> None:
        # Lazy import: the baseline pipeline must not pay sklearn's import
        # cost unless the ablation is actually selected.
        from sklearn.ensemble import IsolationForest

        if len(self._window) < 2:
            return
        x_matrix = np.vstack(list(self._window))
        model = IsolationForest(
            n_estimators=self._cfg.n_estimators,
            max_samples=int(min(self._cfg.max_samples, len(x_matrix))),
            random_state=self._cfg.random_state,
            n_jobs=1,
        )
        model.fit(x_matrix)
        # Negated so that higher = more anomalous.
        raw = -model.score_samples(x_matrix)

        self._model = model
        self._ref = {
            "mu": float(np.mean(raw)),
            "sd": float(np.std(raw) + 1e-9),
            "sorted_raw": np.sort(raw),
            "n": int(len(raw)),
            "median": np.median(x_matrix, axis=0),
            "iqr": np.percentile(x_matrix, 75, axis=0)
            - np.percentile(x_matrix, 25, axis=0),
        }
        self._last_fit = tick

    # -- scoring ----------------------------------------------------------
    def score(
        self, key: BaselineKey, x: np.ndarray, tick: int = 0
    ) -> Optional[ScoreResult]:
        """Score one sample, or ``None`` until the forest is fitted."""
        x = np.asarray(x, dtype=float)
        with self._lock:
            model = self._model
            ref = self._ref
        if model is None or ref is None:
            return None

        raw = float(-model.score_samples(x.reshape(1, -1))[0])
        z = (raw - ref["mu"]) / ref["sd"]
        rank = int(np.searchsorted(ref["sorted_raw"], raw, side="right"))
        risk = float(min(1.0, (rank + 0.5) / (ref["n"] + 1.0)))
        contributions = self._explain(ref, x)
        return ScoreResult(
            d_squared=float(max(0.0, z) ** 2),
            risk=risk,
            level=risk_level(risk, self.config),
            contributions=contributions,
            feature_vector=x,
        )

    def _explain(
        self, ref: Dict[str, Any], x: np.ndarray
    ) -> List[FeatureContribution]:
        """Rank features by robust departure from the reference window."""
        median = ref["median"]
        sigma = np.maximum(ref["iqr"] / _IQR_TO_SIGMA, 1e-9)
        robust_z = (x - median) / sigma

        weights = np.abs(robust_z)
        total = float(weights.sum())
        pct = (
            weights / total
            if total > 1e-12
            else np.full(N_FEATURES, 1.0 / N_FEATURES)
        )

        out = [
            FeatureContribution(
                name=FEATURE_NAMES[i],
                value=float(x[i]),
                baseline_mean=float(median[i]),
                baseline_std=float(ref["iqr"][i] / _IQR_TO_SIGMA),
                z_score=float(robust_z[i]),
                contribution_pct=float(pct[i]),
            )
            for i in range(N_FEATURES)
        ]
        out.sort(key=lambda c: abs(c.contribution_pct), reverse=True)
        return out

    # -- introspection ----------------------------------------------------
    def fitted(self, key: Optional[BaselineKey] = None) -> bool:
        with self._lock:
            return self._model is not None

    def fitted_count(self) -> int:
        with self._lock:
            return 1 if self._model is not None else 0

    def window_size(self, key: Optional[BaselineKey] = None) -> int:
        with self._lock:
            return len(self._window)

    def reset(self) -> None:
        with self._lock:
            self._window.clear()
            self._model = None
            self._ref = None
            self._last_fit = -(10 ** 9)


__all__ = ["IsolationScorer"]

"""scoring.py — Joint multivariate deviation scoring with explainability.

Implements design §3 ``scoring/mahalanobis`` and ``scoring/explain``:

* a regularized joint Mahalanobis distance against the long baseline, which
  accounts for correlations between resources and therefore detects
  *combinations* of moderate deviations rather than double-counting
  correlated ones;
* a ranked list of per-feature contributions so alerts are transparent.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
from scipy import stats

from .baseline import KeyState
from .config import FEATURE_NAMES, N_FEATURES, Config
from .data_model import FeatureContribution, RiskLevel, ScoreResult

logger = logging.getLogger(__name__)


def _risk_level(risk: float, config: Config) -> RiskLevel:
    if risk >= config.score.suspend:
        return RiskLevel.SUSPEND
    if risk >= config.score.throttle:
        return RiskLevel.THROTTLE
    if risk >= config.score.notify:
        return RiskLevel.NOTIFY
    return RiskLevel.NORMAL


def risk_level(risk: float, config: Config) -> RiskLevel:
    """Public alias for the risk-threshold mapping.

    Shared with the isolation-forest ablation so both detectors use exactly
    the same response thresholds.
    """
    return _risk_level(risk, config)


class Scorer:
    """Regularized Mahalanobis scorer with ranked feature contributions."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()

    # -- core distance ----------------------------------------------------
    def mahalanobis(self, state: KeyState, x: np.ndarray) -> Tuple[float, np.ndarray, np.ndarray]:
        """Return ``(d_squared, delta, inv_cov @ delta)``.

        ``inv_cov @ delta`` is returned so contributions can be decomposed
        exactly as ``delta_i * (inv_cov @ delta)_i``.
        """
        mu = state.long.mean
        cov = state.long.regularized_cov(self.config.baseline.ridge)
        delta = np.asarray(x, dtype=float) - mu
        try:
            inv = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            inv = np.linalg.pinv(cov)
        inv_delta = inv @ delta
        d_squared = float(delta @ inv_delta)
        return max(0.0, d_squared), delta, inv_delta

    # -- scoring ----------------------------------------------------------
    def score(self, state: KeyState, x: np.ndarray, tick: int = 0) -> ScoreResult:
        x = np.asarray(x, dtype=float)

        if state.long.n < 2:
            return self._neutral(state, x)

        d_squared, delta, inv_delta = self.mahalanobis(state, x)
        risk = float(stats.chi2.cdf(d_squared, df=N_FEATURES))
        level = _risk_level(risk, self.config)

        contributions = self.explain(x, delta, inv_delta, d_squared, state)
        return ScoreResult(
            d_squared=d_squared,
            risk=risk,
            level=level,
            contributions=contributions,
            feature_vector=x,
        )

    # -- explain (scoring/explain) ---------------------------------------
    def explain(
        self,
        x: np.ndarray,
        delta: np.ndarray,
        inv_delta: np.ndarray,
        d_squared: float,
        state: KeyState,
    ) -> List[FeatureContribution]:
        std = state.long.std()
        total = max(d_squared, 1e-12)
        out: List[FeatureContribution] = []
        for i, name in enumerate(FEATURE_NAMES):
            contrib = float(delta[i] * inv_delta[i] / total)
            out.append(
                FeatureContribution(
                    name=name,
                    value=float(x[i]),
                    baseline_mean=float(state.long.mean[i]),
                    baseline_std=float(std[i]),
                    z_score=float(delta[i] / (std[i] + 1e-9)),
                    contribution_pct=contrib,
                )
            )
        out.sort(key=lambda c: abs(c.contribution_pct), reverse=True)
        return out

    def neutral(self, state: KeyState, x: np.ndarray) -> ScoreResult:
        """No-opinion score returned before a detector has enough history."""
        return self._neutral(state, x)

    def _neutral(self, state: KeyState, x: np.ndarray) -> ScoreResult:
        std = state.long.std()
        mean = state.long.mean
        contributions = [
            FeatureContribution(
                name=FEATURE_NAMES[i] if i < len(FEATURE_NAMES) else f"f{i}",
                value=float(x[i]),
                baseline_mean=float(mean[i]),
                baseline_std=float(std[i]),
                z_score=0.0,
                contribution_pct=0.0,
            )
            for i in range(N_FEATURES)
        ]
        return ScoreResult(
            d_squared=0.0,
            risk=0.0,
            level=RiskLevel.NORMAL,
            contributions=contributions,
            feature_vector=x,
        )


__all__ = ["Scorer"]

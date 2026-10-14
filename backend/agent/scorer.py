"""
scorer.py — Joint multivariate anomaly scorer.

Uses Mahalanobis distance against the long-term baseline with a
regularised diagonal covariance estimate (simple per-feature variance).
Returns a RiskScore with ranked per-feature contributions so alerts are
human-readable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import numpy as np

from .baseline import BaselineEntry, BaselineState, N_FEATURES
from .telemetry import ProcessMetrics

FEATURE_NAMES = ProcessMetrics.FEATURE_NAMES

# ---------------------------------------------------------------------------
# Risk tiers
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    NORMAL   = "NORMAL"
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"

_THRESHOLDS = [
    (15.0, RiskLevel.CRITICAL),
    (8.0,  RiskLevel.HIGH),
    (4.0,  RiskLevel.MEDIUM),
    (2.0,  RiskLevel.LOW),
    (0.0,  RiskLevel.NORMAL),
]


def _risk_level(score: float) -> RiskLevel:
    for threshold, level in _THRESHOLDS:
        if score >= threshold:
            return level
    return RiskLevel.NORMAL


# ---------------------------------------------------------------------------
# Feature contribution
# ---------------------------------------------------------------------------

@dataclass
class FeatureContribution:
    name:       str
    value:      float   # observed value
    mean:       float   # baseline mean
    std:        float   # baseline std
    z_score:    float   # (value - mean) / std
    contribution: float # fractional contribution to total score (0-1)


# ---------------------------------------------------------------------------
# Risk score
# ---------------------------------------------------------------------------

@dataclass
class RiskScore:
    pid:          int
    name:         str
    score:        float                          # Mahalanobis distance
    level:        RiskLevel
    contributions: List[FeatureContribution]     # sorted by |contribution| desc
    baseline_state: str
    cusum_value:  float
    timestamp:    float = field(default_factory=__import__("time").time)

    @property
    def top_features(self) -> List[str]:
        return [c.name for c in self.contributions[:3] if c.contribution > 0.05]

    def to_dict(self) -> dict:
        return {
            "pid":           self.pid,
            "name":          self.name,
            "score":         round(self.score, 3),
            "level":         self.level.value,
            "baseline_state": self.baseline_state,
            "cusum_value":   round(self.cusum_value, 3),
            "timestamp":     self.timestamp,
            "top_features":  self.top_features,
            "contributions": [
                {
                    "name":         c.name,
                    "value":        round(c.value, 3),
                    "mean":         round(c.mean, 3),
                    "std":          round(c.std, 3),
                    "z_score":      round(c.z_score, 3),
                    "contribution": round(c.contribution, 3),
                }
                for c in self.contributions
            ],
        }


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class AnomalyScorer:
    """
    Computes per-process Mahalanobis distance against the long-term baseline.

    For processes still in the LEARNING state (baseline not yet trained),
    returns a score of 0 with NORMAL level so no spurious alerts fire.
    """

    def __init__(self, regularisation: float = 1e-3) -> None:
        self.reg = regularisation

    def score(
        self,
        metrics: ProcessMetrics,
        entry:   BaselineEntry,
    ) -> RiskScore:
        x   = np.array(metrics.feature_vector, dtype=float)
        lt  = entry.lt

        # Not yet trained — return benign score
        if lt.n < 5:
            return self._zero_score(metrics, entry)

        mu  = lt.mean
        std = lt.std() + self.reg

        z   = (x - mu) / std
        # Mahalanobis with diagonal (per-feature) covariance
        d   = float(np.sqrt(np.sum(z ** 2)))

        # Per-feature contributions (fraction of total squared distance)
        sq_total = max(np.sum(z ** 2), 1e-9)
        contribs = []
        for i, name in enumerate(FEATURE_NAMES):
            contribs.append(FeatureContribution(
                name=name,
                value=float(x[i]),
                mean=float(mu[i]),
                std=float(std[i]),
                z_score=float(z[i]),
                contribution=float(z[i] ** 2 / sq_total),
            ))
        contribs.sort(key=lambda c: -abs(c.z_score))

        return RiskScore(
            pid=metrics.pid,
            name=metrics.name,
            score=d,
            level=_risk_level(d),
            contributions=contribs,
            baseline_state=entry.state.value,
            cusum_value=entry.cusum.value,
        )

    def _zero_score(self, metrics: ProcessMetrics, entry: BaselineEntry) -> RiskScore:
        x  = np.array(metrics.feature_vector, dtype=float)
        mu = entry.lt.mean if entry.lt.n > 0 else np.zeros(N_FEATURES)
        std_v = entry.lt.std() if entry.lt.n > 0 else np.ones(N_FEATURES)
        contribs = [
            FeatureContribution(
                name=FEATURE_NAMES[i],
                value=float(x[i]),
                mean=float(mu[i]),
                std=float(std_v[i]),
                z_score=0.0,
                contribution=1.0 / N_FEATURES,
            )
            for i in range(N_FEATURES)
        ]
        return RiskScore(
            pid=metrics.pid,
            name=metrics.name,
            score=0.0,
            level=RiskLevel.NORMAL,
            contributions=contribs,
            baseline_state=entry.state.value,
            cusum_value=0.0,
        )

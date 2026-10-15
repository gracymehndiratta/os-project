"""data_model.py — Canonical data types shared across the pipeline.

These mirror the objects listed in design document §8 ("Configuration" /
"Data model"): ``Sample``, ``BaselineKey``, ``Gaussian``, ``KeyState``,
``Alert``.  The statistics types (``Gaussian``) live in ``baseline.py`` and
are re-exported here for convenience.

Everything in this module is intentionally dependency-light: it must be
importable by the collectors, the store, the scorer, the gate and the
response engine without creating cycles.
"""
from __future__ import annotations

import datetime
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional

import numpy as np

from .config import FEATURE_NAMES, N_FEATURES


# ---------------------------------------------------------------------------
# Sampling tiers (§6)
# ---------------------------------------------------------------------------

class Tier(IntEnum):
    """Sampling tiers, ordered cheapest → most detailed."""
    MINIMAL = 0    # every 8 ticks, CPU + memory only, 0.125 units/tick
    NORMAL = 1     # every 2 ticks, all five features, 2 units/tick
    DETAILED = 2   # every tick, all five features, 4 units/tick


# ---------------------------------------------------------------------------
# Gate states (§5)
# ---------------------------------------------------------------------------

class GateState(str, Enum):
    """Per-baseline-key state of the divergence gate."""
    LEARNING = "LEARNING"   # accumulating the first min_samples observations
    NORMAL = "NORMAL"       # long baseline learning under the update guard
    FROZEN = "FROZEN"       # long baseline locked, samples quarantined
    MERGE = "MERGE"         # quarantine merged after independent corroboration
    DISCARD = "DISCARD"     # quarantine dropped (TTL expired / not corroborated)


# ---------------------------------------------------------------------------
# Risk levels (§8 score.notify / throttle / suspend)
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    NORMAL = "NORMAL"
    NOTIFY = "NOTIFY"       # risk >= notify percentile
    THROTTLE = "THROTTLE"   # risk >= throttle percentile
    SUSPEND = "SUSPEND"     # risk >= suspend percentile


_RISK_ORDER = {
    RiskLevel.NORMAL: 0,
    RiskLevel.NOTIFY: 1,
    RiskLevel.THROTTLE: 2,
    RiskLevel.SUSPEND: 3,
}


def risk_rank(level: RiskLevel) -> int:
    return _RISK_ORDER.get(level, 0)


def max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if risk_rank(a) >= risk_rank(b) else b


# ---------------------------------------------------------------------------
# Baseline key (§8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaselineKey:
    """Identifies a behavioral baseline.

    ``application_id`` is a 16-hex-char hash of the executable / application
    identity (stable across PID reuse).  ``context`` encodes the
    time-bucket / session / parent context; the coarse fallback context is
    the literal ``"*"`` (design §8: 'application id, context ("*" is the
    coarse fallback)').
    """
    application_id: str
    context: str = "*"

    def coarse(self) -> "BaselineKey":
        return BaselineKey(self.application_id, "*")

    @property
    def is_coarse(self) -> bool:
        return self.context == "*"

    def __str__(self) -> str:
        return f"{self.application_id}|{self.context}"


# ---------------------------------------------------------------------------
# Sample (§8)
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """One telemetry observation for a process instance at a given tick."""
    process_instance_id: str
    application_id: str
    parent_application: str
    tick: int
    raw_features: np.ndarray
    measured_mask: np.ndarray
    tier: Tier
    cost: float
    exe_name: str = ""
    pid: int = 0
    username: str = ""
    start_time: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def is_full(self) -> bool:
        """True when every feature in the canonical vector was measured."""
        return bool(np.all(np.asarray(self.measured_mask, dtype=bool)))

    def feature_vector(self) -> np.ndarray:
        """Log-scale feature vector used by the baseline store and scorer.

        Design §4: 'Each full sample becomes a log-scale feature vector'.
        Values are clamped at >= 0 before the log to keep the transform
        finite for counter-like features.
        """
        x = np.asarray(self.raw_features, dtype=float)
        return np.log1p(np.maximum(x, 0.0))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "process_instance_id": self.process_instance_id,
            "application_id": self.application_id,
            "pid": self.pid,
            "exe_name": self.exe_name,
            "parent_application": self.parent_application,
            "username": self.username,
            "tick": self.tick,
            "tier": int(self.tier),
            "tier_name": self.tier.name,
            "cost": self.cost,
            "measured_mask": [bool(m) for m in self.measured_mask],
            "features": {
                FEATURE_NAMES[i]: round(float(self.raw_features[i]), 4)
                for i in range(min(len(FEATURE_NAMES), len(self.raw_features)))
            },
        }


# ---------------------------------------------------------------------------
# Scoring results (§8)
# ---------------------------------------------------------------------------

@dataclass
class FeatureContribution:
    """One feature's ranked contribution to a joint deviation score."""
    name: str
    value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    contribution_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": round(float(self.value), 4),
            "mean": round(float(self.baseline_mean), 4),
            "std": round(float(self.baseline_std), 4),
            "z_score": round(float(self.z_score), 3),
            "contribution": round(float(self.contribution_pct), 4),
        }


@dataclass
class ScoreResult:
    """Joint deviation score with explainability (§8).

    Field order is significant: ``ScoreResult(d_squared, risk, level,
    contributions, feature_vector)`` is constructed positionally in tests.
    """
    d_squared: float
    risk: float
    level: RiskLevel
    contributions: List[FeatureContribution]
    feature_vector: np.ndarray = field(default_factory=lambda: np.zeros(N_FEATURES))

    @property
    def distance(self) -> float:
        return float(np.sqrt(max(0.0, self.d_squared)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "d_squared": round(float(self.d_squared), 4),
            "distance": round(self.distance, 4),
            "risk": round(float(self.risk), 6),
            "level": self.level.value,
            "feature_vector": [round(float(v), 4) for v in self.feature_vector],
            "contributions": [c.to_dict() for c in self.contributions],
        }


# ---------------------------------------------------------------------------
# Alerts (§8)
# ---------------------------------------------------------------------------

_SEVERITY_MAP = {
    RiskLevel.NORMAL: "none",
    RiskLevel.NOTIFY: "medium",
    RiskLevel.THROTTLE: "high",
    RiskLevel.SUSPEND: "critical",
}

_ACTION_MAP = {
    RiskLevel.NORMAL: "log_only",
    RiskLevel.NOTIFY: "notify_admin",
    RiskLevel.THROTTLE: "increase_sampling_and_notify",
    RiskLevel.SUSPEND: "consider_suspension",
}


@dataclass
class Alert:
    """An explainable alert raised for an anomalous process (§8)."""
    alert_id: str
    key: BaselineKey
    pid: int
    process_name: str
    tick: int
    d_squared: float
    risk_score: float
    level: RiskLevel
    gate_state: GateState
    consecutive_count: int
    explanation: List[str]
    contributions: List[FeatureContribution]
    recommended_action: str
    dry_run: bool
    severity: str = "medium"
    acknowledged: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    @property
    def risk_level(self) -> str:
        return self.level.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "pid": self.pid,
            "process_name": self.process_name,
            "context_key": str(self.key),
            "risk_score": round(float(self.risk_score), 3),
            "risk_level": self.level.value,
            "severity": self.severity,
            "d_squared": round(float(self.d_squared), 4),
            "level": self.level.value,
            "gate_state": self.gate_state.value,
            "consecutive_count": self.consecutive_count,
            "explanation": self.explanation,
            "contributions": [c.to_dict() for c in self.contributions],
            "recommended_action": self.recommended_action,
            "dry_run": self.dry_run,
            "acknowledged": self.acknowledged,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Rollback actions (§8 response/actions)
# ---------------------------------------------------------------------------

@dataclass
class RollbackAction:
    """A reversible defensive action recorded for administrative rollback."""
    action_id: str
    action_type: RiskLevel
    pid: int
    tick: int
    dry_run: bool
    executed: bool = False
    rolled_back: bool = False
    details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "pid": self.pid,
            "tick": self.tick,
            "dry_run": self.dry_run,
            "executed": self.executed,
            "rolled_back": self.rolled_back,
            "details": self.details,
        }


__all__ = [
    "Tier",
    "GateState",
    "RiskLevel",
    "risk_rank",
    "max_risk",
    "BaselineKey",
    "Sample",
    "FeatureContribution",
    "ScoreResult",
    "Alert",
    "RollbackAction",
    "N_FEATURES",
    "FEATURE_NAMES",
]

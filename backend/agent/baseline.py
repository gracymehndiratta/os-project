"""
baseline.py — Per-(process, context) behavioral baseline store.

Each entry holds:
  • Short-term EWM  (fast decay α=0.15) — tracks recent behaviour
  • Long-term  EWM  (slow decay α=0.02) — stable reference
  • CUSUM accumulator — catches slow drift that keeps both baselines moving
  • Quarantine buffer — holds samples captured during a freeze

State machine per baseline entry:
  LEARNING  → accumulating initial observations (min_obs not reached)
  NORMAL    → updating both baselines freely
  FROZEN    → long-term baseline locked; samples go to quarantine
  RECONCILED→ quarantine merged after admin corroboration
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

from .context import ContextKey, ProcessIdentity

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------

N_FEATURES = 6   # must match ProcessMetrics.feature_vector length
MIN_OBSERVATIONS = 30          # samples before a baseline is considered trained
ST_ALPHA  = 0.15               # short-term EWM decay (fast)
LT_ALPHA  = 0.02               # long-term  EWM decay (slow)
CUSUM_SLACK = 0.5              # allow up to 0.5 σ drift per sample before CUSUM counts it
CUSUM_THRESHOLD = 8.0          # CUSUM alarm threshold (in normalised units)
QUARANTINE_MAX_SAMPLES = 2000
QUARANTINE_MAX_AGE_S   = 86400  # 24 hours


class BaselineState(str, Enum):
    LEARNING    = "LEARNING"
    NORMAL      = "NORMAL"
    FROZEN      = "FROZEN"
    RECONCILED  = "RECONCILED"


# ---------------------------------------------------------------------------
# EWM state (mean + variance, online update)
# ---------------------------------------------------------------------------

@dataclass
class EWMState:
    """Exponentially Weighted Mean and Variance, online, per-feature."""
    alpha: float
    mean: np.ndarray = field(default_factory=lambda: np.zeros(N_FEATURES))
    var:  np.ndarray = field(default_factory=lambda: np.ones(N_FEATURES) * 1e-3)
    n:    int = 0

    def update(self, x: np.ndarray) -> None:
        if self.n == 0:
            self.mean = x.copy()
            self.var  = np.ones(N_FEATURES) * 1e-3
        else:
            delta     = x - self.mean
            self.mean = self.mean + self.alpha * delta
            self.var  = (1 - self.alpha) * (self.var + self.alpha * delta ** 2)
        self.n += 1

    def std(self) -> np.ndarray:
        return np.sqrt(np.maximum(self.var, 1e-6))

    def as_dict(self) -> dict:
        return {
            "mean": self.mean.tolist(),
            "std":  self.std().tolist(),
            "n":    self.n,
        }


# ---------------------------------------------------------------------------
# CUSUM tracker
# ---------------------------------------------------------------------------

@dataclass
class CUSUMTracker:
    """
    Page's CUSUM on the L2 norm of the normalised deviation from the long-term
    baseline.  Detects persistent upward drift that stays below the
    instantaneous divergence threshold.
    """
    slack:     float = CUSUM_SLACK
    threshold: float = CUSUM_THRESHOLD
    _pos: float = 0.0   # positive (upper) CUSUM

    def update(self, normalised_deviation: float) -> bool:
        """Return True if CUSUM crosses the alarm threshold."""
        self._pos = max(0.0, self._pos + normalised_deviation - self.slack)
        return self._pos >= self.threshold

    def reset(self) -> None:
        self._pos = 0.0

    @property
    def value(self) -> float:
        return self._pos


# ---------------------------------------------------------------------------
# Quarantine buffer entry
# ---------------------------------------------------------------------------

@dataclass
class QuarantineEntry:
    x:         np.ndarray
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Baseline entry
# ---------------------------------------------------------------------------

@dataclass
class BaselineEntry:
    identity:   ProcessIdentity
    context_key: ContextKey
    state:      BaselineState = BaselineState.LEARNING
    st:         EWMState = field(default_factory=lambda: EWMState(alpha=ST_ALPHA))
    lt:         EWMState = field(default_factory=lambda: EWMState(alpha=LT_ALPHA))
    cusum:      CUSUMTracker = field(default_factory=CUSUMTracker)
    quarantine: Deque[QuarantineEntry] = field(
        default_factory=lambda: deque(maxlen=QUARANTINE_MAX_SAMPLES)
    )
    freeze_time: Optional[float] = None
    reconcile_time: Optional[float] = None
    freeze_count:   int = 0         # number of times frozen
    last_update:    float = field(default_factory=time.time)

    def update(self, x: np.ndarray, now: float) -> None:
        self.last_update = now
        # Always update short-term
        self.st.update(x)

        if self.state == BaselineState.LEARNING:
            self.lt.update(x)
            if self.lt.n >= MIN_OBSERVATIONS:
                self.state = BaselineState.NORMAL
                self.cusum.reset()

        elif self.state == BaselineState.NORMAL:
            self.lt.update(x)
            # CUSUM on normalised deviation from long-term
            norm_dev = np.linalg.norm(
                (x - self.lt.mean) / (self.lt.std() + 1e-9)
            )
            if self.cusum.update(norm_dev / np.sqrt(N_FEATURES)):
                # Slow drift alarm — freeze immediately
                self.freeze(now)

        elif self.state == BaselineState.FROZEN:
            # Queue in quarantine (do NOT update long-term)
            self._enqueue_quarantine(x, now)

    def freeze(self, now: float) -> None:
        self.state       = BaselineState.FROZEN
        self.freeze_time = now
        self.freeze_count += 1
        self.cusum.reset()

    def reconcile(self, now: float) -> int:
        """
        Admin-corroborated reconcile: merge quarantine into long-term baseline.
        Returns the number of samples merged.
        """
        cutoff = now - QUARANTINE_MAX_AGE_S
        valid  = [e for e in self.quarantine if e.timestamp >= cutoff]
        for entry in valid:
            self.lt.update(entry.x)
        self.quarantine.clear()
        self.state          = BaselineState.NORMAL
        self.reconcile_time = now
        self.cusum.reset()
        return len(valid)

    def dismiss_quarantine(self) -> None:
        """Discard quarantine without merging (if threat confirmed)."""
        self.quarantine.clear()
        self.state = BaselineState.NORMAL
        self.cusum.reset()

    def _enqueue_quarantine(self, x: np.ndarray, now: float) -> None:
        # Expire old entries first
        cutoff = now - QUARANTINE_MAX_AGE_S
        while self.quarantine and self.quarantine[0].timestamp < cutoff:
            self.quarantine.popleft()
        self.quarantine.append(QuarantineEntry(x=x.copy(), timestamp=now))

    @property
    def quarantine_size(self) -> int:
        return len(self.quarantine)

    def to_dict(self) -> dict:
        return {
            "pid":              self.identity.pid,
            "process_name":     self.context_key.process_name,
            "context":          str(self.context_key),
            "state":            self.state.value,
            "st_mean":          self.st.mean.tolist(),
            "st_std":           self.st.std().tolist(),
            "lt_mean":          self.lt.mean.tolist(),
            "lt_std":           self.lt.std().tolist(),
            "lt_n":             self.lt.n,
            "cusum_value":      round(self.cusum.value, 3),
            "quarantine_size":  self.quarantine_size,
            "freeze_count":     self.freeze_count,
            "freeze_time":      self.freeze_time,
            "reconcile_time":   self.reconcile_time,
            "last_update":      self.last_update,
        }


# ---------------------------------------------------------------------------
# Baseline Store
# ---------------------------------------------------------------------------

class BaselineStore:
    """
    Central registry of per-(ProcessIdentity, ContextKey) baseline entries.
    Provides fallback to coarser context keys when the exact key has insufficient
    observations.
    """

    def __init__(self) -> None:
        self._store: Dict[Tuple[ProcessIdentity, ContextKey], BaselineEntry] = {}

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_or_create(
        self, identity: ProcessIdentity, ctx: ContextKey
    ) -> BaselineEntry:
        key = (identity, ctx)
        if key not in self._store:
            self._store[key] = BaselineEntry(identity=identity, context_key=ctx)
        return self._store[key]

    def update(
        self, identity: ProcessIdentity, ctx: ContextKey, x: np.ndarray, now: float
    ) -> BaselineEntry:
        entry = self.get_or_create(identity, ctx)
        entry.update(x, now)
        return entry

    def get_reference(
        self, identity: ProcessIdentity, ctx: ContextKey
    ) -> Optional[BaselineEntry]:
        """
        Return the best available (trained) baseline for this context,
        falling back to coarser keys if the exact one is not yet trained.
        """
        key = (identity, ctx)
        entry = self._store.get(key)
        if entry and entry.lt.n >= MIN_OBSERVATIONS:
            return entry

        # Try progressively coarser fallback keys
        fallback_ctx = ctx.fallback()
        while fallback_ctx is not None:
            # Search across all identities for a matching context key
            for (ident, ck), e in self._store.items():
                if ck == fallback_ctx and e.lt.n >= MIN_OBSERVATIONS:
                    return e
            fallback_ctx = fallback_ctx.fallback()

        return entry  # return even if not trained (will be LEARNING state)

    def all_entries(self) -> List[BaselineEntry]:
        return list(self._store.values())

    def frozen_entries(self) -> List[BaselineEntry]:
        return [e for e in self._store.values() if e.state == BaselineState.FROZEN]

    def reconcile(self, pid: int, now: float) -> Optional[int]:
        """Admin-triggered reconcile for all entries matching pid."""
        merged = 0
        for (ident, _), entry in self._store.items():
            if ident.pid == pid and entry.state == BaselineState.FROZEN:
                merged += entry.reconcile(now)
        return merged if merged >= 0 else None

    def purge_stale(self, active_pids: set[int], max_age_s: float = 7200.0) -> int:
        now = time.time()
        stale = [
            k for k, e in self._store.items()
            if k[0].pid not in active_pids and (now - e.last_update) > max_age_s
        ]
        for k in stale:
            del self._store[k]
        return len(stale)

"""baseline.py — Baseline statistics and the per-(application, context) store.

Implements the two design document §3 modules:

* ``baseline/stats`` – incremental mean and covariance with exact merge
  (:class:`Gaussian`).
* ``baseline/store`` – per-(app, context) store, coarse fallback and LRU
  eviction (:class:`BaselineStore`), holding a :class:`KeyState` per key.

A :class:`KeyState` carries the three Gaussians the gate needs
(short / long / reference), the gate state, the CUSUM accumulator, the
quarantine buffer and freeze bookkeeping (design §8 "KeyState").
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .config import FEATURE_NAMES, N_FEATURES, Config
from .data_model import BaselineKey, GateState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# baseline/stats — Gaussian with incremental stats and exact merge
# ---------------------------------------------------------------------------

class Gaussian:
    """Incremental multivariate Gaussian.

    Supports two update modes:

    * exact Welford update (``alpha is None``) – used for a reference
      snapshot and for merging quarantine summaries;
    * exponentially-weighted update (``alpha`` given) – used for the short
      and long baselines so they can react at different rates.

    ``n`` is the *effective count*: it counts observations seen, which is
    what the trust threshold (``baseline.min_samples``) is compared against.
    """

    def __init__(self, dim: int = N_FEATURES) -> None:
        self.dim = dim
        self.n = 0
        self.mean = np.zeros(dim, dtype=float)
        # Small initial ridge keeps the very first covariance invertible; the
        # parallel merge is therefore exact only up to this O(1/n) offset.
        self.cov = np.eye(dim, dtype=float) * 1e-3

    # -- updates ----------------------------------------------------------
    def update(self, x: np.ndarray, alpha: Optional[float] = None) -> None:
        x = np.asarray(x, dtype=float)
        if self.n == 0:
            self.mean = x.copy()
            self.cov = np.eye(self.dim, dtype=float) * 1e-3
            self.n = 1
            return

        if alpha is None:
            # Exact online (Welford) update of mean and covariance.
            n_new = self.n + 1
            delta = x - self.mean
            mean_new = self.mean + delta / n_new
            outer = np.outer(delta, x - mean_new)
            self.cov = (self.n * self.cov + outer) / n_new
            self.mean = mean_new
            self.n = n_new
        else:
            # Exponentially-weighted mean and covariance.
            delta = x - self.mean
            self.mean = self.mean + alpha * delta
            self.cov = (1.0 - alpha) * (self.cov + alpha * np.outer(delta, delta))
            self.n += 1

    def merge(self, other: "Gaussian") -> None:
        """Exact parallel combination of two Gaussians (design §3)."""
        if other.n == 0:
            return
        if self.n == 0:
            self.mean = other.mean.copy()
            self.cov = other.cov.copy()
            self.n = other.n
            return

        n = self.n + other.n
        delta = other.mean - self.mean
        self.cov = (
            self.n * self.cov
            + other.n * other.cov
            + np.outer(delta, delta) * (self.n * other.n / n)
        ) / n
        self.mean = self.mean + delta * (other.n / n)
        self.n = n

    def merge_samples(self, samples: List[np.ndarray]) -> int:
        """Merge a batch of raw samples by summarising then combining."""
        if not samples:
            return 0
        summary = Gaussian(self.dim)
        for x in samples:
            summary.update(x)
        self.merge(summary)
        return summary.n

    # -- views ------------------------------------------------------------
    def std(self) -> np.ndarray:
        return np.sqrt(np.maximum(np.diag(self.cov), 1e-9))

    def regularized_cov(self, ridge: float) -> np.ndarray:
        return self.cov + np.eye(self.dim, dtype=float) * ridge

    def copy(self) -> "Gaussian":
        g = Gaussian(self.dim)
        g.n = self.n
        g.mean = self.mean.copy()
        g.cov = self.cov.copy()
        return g

    def standardized_distance(self, other: "Gaussian", ridge: float = 1e-6) -> float:
        """RMS standardized distance between two mean vectors.

        Uses the union covariance so the measure is symmetric.  Returns
        ``sqrt( delta^T Sigma^-1 delta / dim )`` so thresholds are expressed
        in per-feature standard deviations.
        """
        if self.n == 0 or other.n == 0:
            return 0.0
        delta = self.mean - other.mean
        cov = 0.5 * (self.cov + other.cov) + np.eye(self.dim) * ridge
        try:
            inv = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            inv = np.linalg.pinv(cov)
        d2 = float(delta @ inv @ delta)
        return float(np.sqrt(max(0.0, d2) / self.dim))

    def to_dict(self) -> Dict[str, object]:
        return {
            "n": int(self.n),
            "mean": [round(float(v), 4) for v in self.mean],
            "std": [round(float(v), 4) for v in self.std()],
        }


# ---------------------------------------------------------------------------
# Quarantine buffer (gate/quarantine)
# ---------------------------------------------------------------------------

@dataclass
class QuarantineBuffer:
    """Bounded summary-statistic buffer with a TTL (design §3, §5)."""
    max_samples: int = 512
    ttl_ticks: int = 1440
    samples: List[Tuple[np.ndarray, int]] = field(default_factory=list)

    def add(self, x: np.ndarray, tick: int) -> None:
        self.samples.append((np.asarray(x, dtype=float).copy(), tick))
        if len(self.samples) > self.max_samples:
            self.samples = self.samples[-self.max_samples:]

    def prune(self, now_tick: int) -> None:
        cutoff = now_tick - self.ttl_ticks
        self.samples = [(x, t) for (x, t) in self.samples if t >= cutoff]

    def expired(self, now_tick: int) -> bool:
        if not self.samples:
            return False
        oldest = min(t for _, t in self.samples)
        return (now_tick - oldest) >= self.ttl_ticks

    def vectors(self) -> List[np.ndarray]:
        return [x for x, _ in self.samples]

    def summary(self) -> Gaussian:
        g = Gaussian()
        for x, _ in self.samples:
            g.update(x)
        return g

    @property
    def size(self) -> int:
        return len(self.samples)

    def clear(self) -> None:
        self.samples = []


# ---------------------------------------------------------------------------
# KeyState (§8)
# ---------------------------------------------------------------------------

@dataclass
class KeyState:
    """All state associated with one :class:`BaselineKey`."""
    key: BaselineKey
    process_name: str = ""
    pid: int = 0
    short: Gaussian = field(default_factory=Gaussian)
    long: Gaussian = field(default_factory=Gaussian)
    reference: Gaussian = field(default_factory=Gaussian)
    gate_state: GateState = GateState.LEARNING
    cusum: float = 0.0
    buffer: QuarantineBuffer = field(default_factory=QuarantineBuffer)
    freeze_tick: Optional[int] = None
    freeze_reason: str = ""
    freeze_count: int = 0
    reconcile_tick: Optional[int] = None
    last_update_tick: int = -1
    flagged: bool = False
    last_risk: float = 0.0
    last_level: str = "NORMAL"

    # -- helpers ----------------------------------------------------------
    @property
    def trusted(self) -> bool:
        return self.long.n >= 0 and self.gate_state != GateState.LEARNING

    @property
    def quarantine_size(self) -> int:
        return self.buffer.size

    def reset_reference(self) -> None:
        self.reference = self.long.copy()

    def freeze(self, tick: int, reason: str) -> None:
        if self.gate_state != GateState.FROZEN:
            self.gate_state = GateState.FROZEN
            self.freeze_tick = tick
            self.freeze_reason = reason
            self.freeze_count += 1
            self.flagged = True
            self.cusum = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "pid": self.pid,
            "process_name": self.process_name,
            "context": str(self.key),
            "application_id": self.key.application_id,
            "state": self.gate_state.value,
            "flagged": self.flagged,
            "st_mean": [round(float(v), 4) for v in self.short.mean],
            "st_std": [round(float(v), 4) for v in self.short.std()],
            "lt_mean": [round(float(v), 4) for v in self.long.mean],
            "lt_std": [round(float(v), 4) for v in self.long.std()],
            "lt_n": int(self.long.n),
            "mean": [round(float(v), 4) for v in self.long.mean],
            "std": [round(float(v), 4) for v in self.long.std()],
            "n": int(self.long.n),
            "cusum_value": round(float(self.cusum), 3),
            "quarantine_size": self.quarantine_size,
            "freeze_count": self.freeze_count,
            "freeze_tick": self.freeze_tick,
            "freeze_time": self.freeze_tick,
            "freeze_reason": self.freeze_reason,
            "reconcile_tick": self.reconcile_tick,
            "reconcile_time": self.reconcile_tick,
            "last_update": self.last_update_tick,
            "last_risk": round(float(self.last_risk), 4),
            "last_level": self.last_level,
            "feature_names": FEATURE_NAMES,
        }


# ---------------------------------------------------------------------------
# BaselineStore (baseline/store)
# ---------------------------------------------------------------------------

class BaselineStore:
    """Per-(application, context) store with coarse fallback and LRU eviction."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()
        self._states: "OrderedDict[BaselineKey, KeyState]" = OrderedDict()

    # -- access -----------------------------------------------------------
    def get_or_create(
        self,
        key: BaselineKey,
        process_name: str = "",
        pid: int = 0,
    ) -> KeyState:
        state = self._states.get(key)
        if state is None:
            ttl = int(self.config.gate.ttl_days * self.config.ticks_per_day)
            state = KeyState(
                key=key,
                process_name=process_name,
                pid=pid,
                buffer=QuarantineBuffer(
                    max_samples=self.config.gate.max_buffer_samples,
                    ttl_ticks=ttl,
                ),
            )
            self._states[key] = state
            self._evict()
        else:
            self._states.move_to_end(key)
            if process_name:
                state.process_name = process_name
            if pid:
                state.pid = pid
        return state

    def get(self, key: BaselineKey) -> Optional[KeyState]:
        return self._states.get(key)

    def resolve(self, key: BaselineKey) -> KeyState:
        """Return the baseline to *score against*.

        Uses the context-specific key when it is trusted, otherwise the
        coarse application baseline (design §4, step 4).  Creates the
        coarse fallback if neither exists.
        """
        exact = self._states.get(key)
        if exact is not None and exact.gate_state != GateState.LEARNING:
            return exact
        coarse_key = key.coarse()
        if coarse_key != key:
            coarse = self._states.get(coarse_key)
            if coarse is not None and coarse.gate_state != GateState.LEARNING:
                return coarse
        return exact if exact is not None else self.get_or_create(key)

    # -- learning ---------------------------------------------------------
    def learn(self, state: KeyState, x: np.ndarray, tick: int) -> None:
        """Warm-up learning: update both baselines, promote to NORMAL."""
        state.short.update(x, alpha=self.config.baseline.alpha_short)
        state.long.update(x, alpha=self.config.baseline.alpha_long)
        state.last_update_tick = tick
        if (
            state.gate_state == GateState.LEARNING
            and state.long.n >= self.config.baseline.min_samples
        ):
            state.gate_state = GateState.NORMAL
            # The reference snapshot is taken later, by the gate, once the
            # long baseline has converged (see DivergenceGate.observe).

    # -- administration ---------------------------------------------------
    def reconcile(self, pid: int, tick: int) -> int:
        """Merge quarantined samples after independent corroboration."""
        merged = 0
        for state in self._states.values():
            if state.pid == pid and state.gate_state == GateState.FROZEN:
                merged += self._merge_buffer(state, tick)
        return merged

    def _merge_buffer(self, state: KeyState, tick: int) -> int:
        vectors = state.buffer.vectors()
        n = state.long.merge_samples(vectors)
        state.buffer.clear()
        state.gate_state = GateState.MERGE
        state.reconcile_tick = tick
        state.reset_reference()  # reference reset after merge (design §5)
        state.cusum = 0.0
        state.flagged = False
        # Return to NORMAL immediately after a merge.
        state.gate_state = GateState.NORMAL
        return n

    def dismiss(self, pid: int, tick: int) -> int:
        """Discard quarantine for a PID; reset short baseline to long."""
        dropped = 0
        for state in self._states.values():
            if state.pid == pid and state.gate_state == GateState.FROZEN:
                dropped += state.buffer.size
                state.buffer.clear()
                state.gate_state = GateState.DISCARD
                state.short = state.long.copy()
                state.cusum = 0.0
                state.flagged = False
                state.reconcile_tick = tick
                state.gate_state = GateState.NORMAL
        return dropped

    # -- introspection ----------------------------------------------------
    def all_states(self) -> List[KeyState]:
        return list(self._states.values())

    def frozen_states(self) -> List[KeyState]:
        return [s for s in self._states.values() if s.gate_state == GateState.FROZEN]

    def quarantine_count(self) -> int:
        return sum(s.buffer.size for s in self._states.values())

    def active_buffer_count(self) -> int:
        return sum(1 for s in self._states.values() if s.buffer.size > 0)

    def _evict(self) -> None:
        cap = self.config.baseline.max_entries
        while len(self._states) > cap:
            key, state = self._states.popitem(last=False)
            logger.debug("Evicted baseline %s (LRU)", key)


__all__ = ["Gaussian", "QuarantineBuffer", "KeyState", "BaselineStore"]

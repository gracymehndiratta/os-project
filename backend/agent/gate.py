"""gate.py — Divergence gate, quarantine and corroboration.

Implements the design §3 / §5 modules:

* ``gate/divergence``    – three drift measures: mean shift (short vs long),
  CUSUM (accumulated slow excess) and reference drift (long vs a trusted
  reference snapshot).
* ``gate/statemachine``  – NORMAL → FROZEN → MERGE / DISCARD transitions.
* ``gate/quarantine``    – bounded summary-statistic buffer with TTL.
* ``gate/corroboration`` – independent legitimacy evidence.

A return to normal behaviour is *never* treated as corroboration: malware can
stop on purpose (design §5).  Corroboration comes only from independent
sources – an administrator confirmation or a verified application update.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

import numpy as np

from .baseline import BaselineStore, KeyState
from .config import N_FEATURES, Config
from .data_model import GateState, ScoreResult

logger = logging.getLogger(__name__)

# Small ridge for gate standardization; distinct from the scorer's ridge so
# the drift measures are not over-smoothed.
_GATE_RIDGE = 1e-3


# ---------------------------------------------------------------------------
# Corroboration (gate/corroboration)
# ---------------------------------------------------------------------------

class CorroborationSource(str, Enum):
    ADMIN = "admin_confirmation"
    VERIFIED_UPDATE = "verified_application_update"


def is_independent(source: Optional[CorroborationSource]) -> bool:
    """Return True only for genuinely independent corroboration."""
    return source in (CorroborationSource.ADMIN, CorroborationSource.VERIFIED_UPDATE)


# ---------------------------------------------------------------------------
# Divergence measures (gate/divergence)
# ---------------------------------------------------------------------------

def mean_shift(state: KeyState) -> float:
    """RMS standardized shift between the short and long baselines.

    Standardized by the *trusted reference* covariance when one exists, so a
    slow ramp cannot hide by inflating the long baseline's own variance.
    """
    scale = state.reference.cov if state.reference.n > 0 else state.long.cov
    return _rms_distance(state.short.mean - state.long.mean, scale)


def reference_drift(state: KeyState) -> float:
    """RMS standardized shift between the long baseline and the reference."""
    if state.reference.n == 0:
        return 0.0
    return _rms_distance(state.long.mean - state.reference.mean, state.reference.cov)


def standardized_excess(state: KeyState, x: np.ndarray) -> float:
    """Map a sample's deviation from the long baseline to a unit-variance score.

    Centered on the *long* baseline (which tracks legitimate slow change such
    as time-of-day variation) but scaled by the *trusted reference*
    covariance, so a slow ramp that the long baseline is only partially
    following still accumulates.  A benign sample has a chi-squared deviation
    with mean ``df`` and variance ``2*df``; after standardizing, a benign
    stream averages zero, which makes the CUSUM slack/alarm levels meaningful.
    """
    if state.reference.n == 0:
        return 0.0
    inv = _inv(state.reference.cov)
    delta = np.asarray(x, dtype=float) - state.long.mean
    d2 = float(delta @ inv @ delta)
    return (d2 - float(N_FEATURES)) / math.sqrt(2.0 * N_FEATURES)


def _inv(cov: np.ndarray) -> np.ndarray:
    return np.linalg.inv(cov + np.eye(N_FEATURES) * _GATE_RIDGE)


def _rms_distance(delta: np.ndarray, cov: np.ndarray) -> float:
    d2 = float(delta @ _inv(cov) @ delta)
    return float(math.sqrt(max(0.0, d2) / N_FEATURES))


# ---------------------------------------------------------------------------
# Gate (gate/statemachine)
# ---------------------------------------------------------------------------

@dataclass
class FreezeReason:
    shift: float
    cusum: float
    reference: float
    reason: str


class DivergenceGate:
    """Protects each long-term baseline from slow poisoning."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()
        self._freeze_events: list = []

    # -- per-sample observation ------------------------------------------
    def observe(self, state: KeyState, score: ScoreResult, x: np.ndarray, tick: int) -> GateState:
        """Advance the gate by one *full* sample.

        Handles short-term learning, the guarded long-term update, the drift
        measures, freezing, quarantine and TTL-based discard.
        """
        cfg = self.config
        x = np.asarray(x, dtype=float)

        # Short baseline always tracks current behaviour.
        state.short.update(x, alpha=cfg.baseline.alpha_short)
        state.last_update_tick = tick

        if state.gate_state == GateState.FROZEN:
            # The buffer expires a fixed lifetime after the freeze; until
            # then, hold new samples in quarantine.
            if (
                state.freeze_tick is not None
                and (tick - state.freeze_tick) >= state.buffer.ttl_ticks
            ):
                self._discard(state, tick)
                # Fall through: the current sample is processed as NORMAL
                # and the gate may freeze again if the behaviour continues.
            else:
                state.buffer.add(x, tick)
                state.buffer.prune(tick)
                state.flagged = True
                return state.gate_state

        # NORMAL: long baseline learns only if the sample is under the guard.
        if score.distance <= cfg.gate.update_guard:
            state.long.update(x, alpha=cfg.baseline.alpha_long)

        # Take the trusted reference snapshot once the long baseline has
        # converged (twice the trust threshold), so the snapshot is not a
        # noisy early estimate.
        if state.reference.n == 0 and state.long.n >= 2 * cfg.baseline.min_samples:
            state.reset_reference()

        # Drift measures.
        shift = mean_shift(state)
        excess = standardized_excess(state, x)
        state.cusum = max(0.0, state.cusum + (excess - cfg.gate.cusum_k))
        ref = reference_drift(state) if state.reference.n > 0 else 0.0

        reason = None
        if shift >= cfg.gate.shift_thr:
            reason = f"mean_shift {shift:.2f} >= {cfg.gate.shift_thr}"
        elif state.cusum >= cfg.gate.cusum_h:
            reason = f"cusum {state.cusum:.2f} >= {cfg.gate.cusum_h}"
        elif ref >= cfg.gate.ref_thr:
            reason = f"reference_drift {ref:.2f} >= {cfg.gate.ref_thr}"

        if reason:
            state.freeze(tick, reason)
            self._freeze_events.append(
                FreezeReason(shift=shift, cusum=state.cusum, reference=ref, reason=reason)
            )
            logger.warning(
                "[GATE FREEZE] %s (pid=%d) frozen at tick %d: %s",
                state.process_name or state.key, state.pid, tick, reason,
            )

        return state.gate_state

    # -- lifecycle --------------------------------------------------------
    def _discard(self, state: KeyState, tick: int) -> None:
        """TTL expired: drop the buffer, reset short baseline to long."""
        dropped = state.buffer.size
        state.buffer.clear()
        state.gate_state = GateState.DISCARD
        state.short = state.long.copy()
        state.cusum = 0.0
        state.flagged = False
        state.reconcile_tick = tick
        state.gate_state = GateState.NORMAL
        logger.info(
            "[GATE DISCARD] dropped %d quarantined samples for %s (TTL expired)",
            dropped, state.process_name or state.key,
        )

    def corroborate(
        self,
        store: BaselineStore,
        pid: int,
        tick: int,
        source: CorroborationSource = CorroborationSource.ADMIN,
    ) -> int:
        """Merge quarantined samples only on independent corroboration."""
        if not is_independent(source):
            logger.warning("Reconcile rejected for pid=%d: corroboration not independent", pid)
            return 0
        return store.reconcile(pid, tick)

    def freeze_events(self) -> list:
        return list(self._freeze_events)


__all__ = [
    "CorroborationSource",
    "is_independent",
    "mean_shift",
    "reference_drift",
    "standardized_excess",
    "DivergenceGate",
]

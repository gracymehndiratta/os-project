"""features.py — Feature building, stable identity and context keys.

Covers three of the design document §3 modules:

* ``features/builder``  – raw metrics → log-scale feature vector
* ``features/identity`` – stable application identity vs. process instance
* ``features/context``  – time bucket and parent-process context key

Stable identity matters: a baseline must survive PID reuse, so the
*application* identity is a hash of the executable and the *process
instance* identity is ``pid@start_time``.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import time
from typing import Optional

import numpy as np

from .config import N_FEATURES
from .data_model import BaselineKey

# ---------------------------------------------------------------------------
# Time bucketing
# ---------------------------------------------------------------------------

_TIME_BUCKETS = [
    (0, 6, "night"),
    (6, 12, "morning"),
    (12, 18, "afternoon"),
    (18, 24, "evening"),
]


def time_bucket(ts: Optional[float] = None) -> str:
    """Return a coarse time-of-day label for a Unix timestamp.

    Prevents a midnight cron job from being scored against a daytime
    interactive baseline.
    """
    t = datetime.datetime.fromtimestamp(ts if ts is not None else time.time())
    for lo, hi, label in _TIME_BUCKETS:
        if lo <= t.hour < hi:
            return label
    return "night"


# ---------------------------------------------------------------------------
# Identity (features/identity)
# ---------------------------------------------------------------------------

def application_id(executable: str) -> str:
    """Return a 16-hex-char, PID-reuse-safe application identity.

    The executable path/name is normalized and hashed so that a restarted
    process reuses the same baseline while a different binary does not.
    """
    token = (executable or "unknown").strip().lower()
    token = os.path.basename(token) or token
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def process_instance_id(pid: int, start_time: float) -> str:
    """Stable identity for a *process instance* (PID reuse safe)."""
    return f"{int(pid)}@{int(start_time)}"


# ---------------------------------------------------------------------------
# Context (features/context)
# ---------------------------------------------------------------------------

def build_context(
    username: str,
    parent_name: str,
    ts: Optional[float] = None,
) -> str:
    """Build the context discriminator half of a :class:`BaselineKey`.

    Format: ``time-bucket|username|parent``.  The coarse fallback context is
    the literal ``"*"`` handled by :class:`BaselineKey`.
    """
    return f"{time_bucket(ts)}|{username or 'unknown'}|{parent_name or 'unknown'}"


def baseline_key(
    executable: str,
    username: str,
    parent_name: str,
    ts: Optional[float] = None,
) -> BaselineKey:
    """Convenience: build a full :class:`BaselineKey` from raw attributes."""
    return BaselineKey(
        application_id=application_id(executable),
        context=build_context(username, parent_name, ts),
    )


# ---------------------------------------------------------------------------
# Feature vector (features/builder)
# ---------------------------------------------------------------------------

def to_feature_vector(raw_features) -> np.ndarray:
    """Map raw metrics to the canonical log-scale feature vector.

    Design §4 treats the log transform as part of feature building so that
    multiplicative / heavy-tailed resources (memory, I/O) behave additively
    and the Gaussian assumptions of the scorer are more reasonable.
    """
    x = np.asarray(raw_features, dtype=float)
    if x.shape[0] != N_FEATURES:
        raise ValueError(
            f"expected {N_FEATURES} raw features, got {x.shape[0]}"
        )
    return np.log1p(np.maximum(x, 0.0))


__all__ = [
    "time_bucket",
    "application_id",
    "process_instance_id",
    "build_context",
    "baseline_key",
    "to_feature_vector",
]

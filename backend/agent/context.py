"""
context.py — Builds stable per-process context keys.

Context key = (process_name, time_bucket, session_user, parent_name)

Time buckets prevent a midnight cron job from being flagged against a
daytime interactive baseline.

Process identity uses (pid, create_time) pairs so that PID reuse across
process restarts does not corrupt the baseline store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Time bucketing
# ---------------------------------------------------------------------------

_TIME_BUCKETS = [
    (0,  6,  "night"),
    (6,  12, "morning"),
    (12, 18, "afternoon"),
    (18, 24, "evening"),
]


def time_bucket(ts: Optional[float] = None) -> str:
    """Return a coarse time-of-day label for the given Unix timestamp."""
    import datetime
    t = datetime.datetime.fromtimestamp(ts or time.time())
    h = t.hour
    for lo, hi, label in _TIME_BUCKETS:
        if lo <= h < hi:
            return label
    return "night"


# ---------------------------------------------------------------------------
# Context key
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextKey:
    """Immutable composite key that uniquely identifies a behavioral context."""
    process_name: str
    time_bucket: str
    username: str
    parent_name: str

    def __str__(self) -> str:
        return (
            f"{self.process_name}|{self.time_bucket}"
            f"|{self.username}|{self.parent_name}"
        )

    # Progressively coarser fallback keys
    def fallback(self) -> Optional["ContextKey"]:
        """Return a coarser context key (drop time bucket, then username)."""
        if self.time_bucket != "*":
            return ContextKey(
                process_name=self.process_name,
                time_bucket="*",
                username=self.username,
                parent_name=self.parent_name,
            )
        if self.username != "*":
            return ContextKey(
                process_name=self.process_name,
                time_bucket="*",
                username="*",
                parent_name=self.parent_name,
            )
        if self.parent_name != "*":
            return ContextKey(
                process_name=self.process_name,
                time_bucket="*",
                username="*",
                parent_name="*",
            )
        return None  # already at broadest


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcessIdentity:
    """Stable identity across PID reuse — keyed on (pid, create_time)."""
    pid: int
    create_time: float  # seconds since epoch, from psutil

    def __str__(self) -> str:
        return f"pid={self.pid}@{self.create_time:.0f}"


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

class ContextBuilder:
    """
    Builds ContextKey and ProcessIdentity from raw process attributes.
    Maintains a mapping from ProcessIdentity → ContextKey for quick lookup.
    """

    def __init__(self) -> None:
        self._cache: dict[ProcessIdentity, ContextKey] = {}

    def build(
        self,
        pid: int,
        create_time: float,
        name: str,
        username: str,
        parent_name: str,
        ts: Optional[float] = None,
    ) -> tuple[ProcessIdentity, ContextKey]:
        identity = ProcessIdentity(pid=pid, create_time=round(create_time, 1))
        key = ContextKey(
            process_name=name,
            time_bucket=time_bucket(ts),
            username=username or "unknown",
            parent_name=parent_name or "unknown",
        )
        self._cache[identity] = key
        return identity, key

    def get_key(self, identity: ProcessIdentity) -> Optional[ContextKey]:
        return self._cache.get(identity)

    def purge_old(self, active_pids: set[int]) -> None:
        """Remove stale entries for processes that no longer exist."""
        stale = [ident for ident in self._cache if ident.pid not in active_pids]
        for ident in stale:
            del self._cache[ident]

"""sampling.py — Self-meter and overhead-budgeted adaptive sampling.

Implements design §3 / §6 modules:

* ``sampling/selfmeter`` – measures the agent's *own* CPU / RSS usage.
* ``sampling/controller`` – assigns tiers in priority order within a cost
  budget, adjusts the budget against the CPU target with hysteresis, and
  counts reduced-coverage ticks.

Budget policy (design §6): when measured agent CPU exceeds the target the
budget shrinks by 20%; when usage stays below 70% of target for several
windows the budget grows by 15%.  The different up/down thresholds and the
dwell count provide hysteresis.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import psutil

from .config import Config
from .data_model import Tier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Self-meter (sampling/selfmeter)
# ---------------------------------------------------------------------------

class SelfMeter:
    """Measures the agent's own resource usage over a sliding window."""

    def __init__(
        self,
        window_ticks: int = 20,
        config: Optional[Config] = None,
    ) -> None:
        self.config = config or Config()
        self.window_ticks = window_ticks
        self._samples: deque = deque(maxlen=window_ticks)
        self._mock: Optional[Dict[str, float]] = None
        self._proc = psutil.Process()

    # -- test / replay hook ----------------------------------------------
    def set_mock(self, cpu: Optional[float] = None, rss: Optional[float] = None) -> None:
        self._mock = {
            "cpu_pct": float(cpu) if cpu is not None else 0.0,
            "rss_mb": float(rss) if rss is not None else 0.0,
            "io_mbps": 0.0,
        }

    def clear_mock(self) -> None:
        self._mock = None

    # -- reading ----------------------------------------------------------
    def _read(self) -> Dict[str, float]:
        if self._mock is not None:
            return dict(self._mock)
        try:
            cpu = self._proc.cpu_percent(interval=None)
            rss = self._proc.memory_info().rss / 1024.0 / 1024.0
            io = 0.0
            try:
                counters = self._proc.io_counters()
                io = (counters.read_bytes + counters.write_bytes) / 1024.0 / 1024.0
            except Exception:
                pass
            return {"cpu_pct": float(cpu), "rss_mb": float(rss), "io_mbps": float(io)}
        except Exception:
            return {"cpu_pct": 0.0, "rss_mb": 0.0, "io_mbps": 0.0}

    def measure(self, tick: int = 0) -> Dict[str, float]:
        """Take a measurement and return the windowed average."""
        self._samples.append(self._read())
        n = len(self._samples)
        avg_cpu = sum(s["cpu_pct"] for s in self._samples) / n
        avg_rss = sum(s["rss_mb"] for s in self._samples) / n
        avg_io = sum(s["io_mbps"] for s in self._samples) / n
        return {
            "cpu_pct": avg_cpu,
            "rss_mb": avg_rss,
            "io_mbps": avg_io,
            "window": n,
        }

    def get_usage_fraction(self, budget_pct: float) -> float:
        """Current windowed CPU average as a fraction of the budget target."""
        if not self._samples or budget_pct <= 0:
            return 0.0
        n = len(self._samples)
        avg_cpu = sum(s["cpu_pct"] for s in self._samples) / n
        return avg_cpu / budget_pct


# ---------------------------------------------------------------------------
# Sampling controller (sampling/controller)
# ---------------------------------------------------------------------------

class SamplingController:
    """Assigns tiers within a cost budget and adapts the budget to usage."""

    def __init__(
        self,
        config: Optional[Config] = None,
        initial_budget: Optional[float] = None,
        min_budget: Optional[float] = None,
        max_budget: Optional[float] = None,
    ) -> None:
        self.config = config or Config()
        s = self.config.sampling
        self.budget = float(initial_budget if initial_budget is not None else s.initial_budget)
        self.min_budget = float(min_budget if min_budget is not None else s.min_budget)
        self.max_budget = float(max_budget if max_budget is not None else s.max_budget)
        self.budget_violation_count = 0
        self.below_count = 0
        self.reduced_coverage_ticks = 0
        self._last_tier_map: Dict[str, Tier] = {}

    # -- budget adaptation -----------------------------------------------
    def adjust_budget(self, measured_cpu_pct: float) -> float:
        """Shrink/grow the cost budget based on measured agent CPU (§6)."""
        s = self.config.sampling
        target = s.cpu_budget_pct

        if measured_cpu_pct > target:
            self.budget = self._clamp(self.budget * (1.0 - s.budget_shrink))
            self.budget_violation_count += 1
            self.below_count = 0
            logger.debug(
                "[BUDGET] cpu %.2f%% > target %.2f%% → budget %.2f",
                measured_cpu_pct, target, self.budget,
            )
        elif measured_cpu_pct <= s.grow_threshold * target:
            self.below_count += 1
            if self.below_count >= s.dwell_count:
                self.budget = self._clamp(self.budget * (1.0 + s.budget_grow))
                self.below_count = 0
                logger.debug(
                    "[BUDGET] under-utilised → budget grows to %.2f", self.budget
                )
        else:
            self.below_count = 0

        return self.budget

    def _clamp(self, value: float) -> float:
        return max(self.min_budget, min(self.max_budget, value))

    # -- tier assignment --------------------------------------------------
    def assign_tiers(
        self,
        procs: Sequence[Tuple[str, int, bool]],
    ) -> Dict[str, Tier]:
        """Assign a tier to every process, in priority order, within budget.

        ``procs`` is a sequence of ``(key, desired_tier, flagged)``.  Every
        process keeps at least tier 0 (MINIMAL); flagged and high-risk
        processes are served first.  A tick where any process receives less
        than it wanted is counted as reduced coverage.
        """
        s = self.config.sampling
        base_cost = len(procs) * s.tier0_cost
        remaining = self.budget - base_cost

        # Flagged first, then highest desired tier, then stable by key.
        order = sorted(procs, key=lambda p: (not bool(p[2]), -int(p[1]), str(p[0])))

        assigned: Dict[str, Tier] = {}
        reduced = False
        for key, desired, _flagged in order:
            desired = int(desired)
            granted = Tier.MINIMAL
            for t in range(min(desired, int(Tier.DETAILED)), 0, -1):
                delta = s.tier_cost(t) - s.tier0_cost
                if remaining + 1e-9 >= delta:
                    granted = Tier(t)
                    remaining -= delta
                    break
            if int(granted) < desired:
                reduced = True
            assigned[key] = granted

        if reduced:
            self.reduced_coverage_ticks += 1

        self._last_tier_map = dict(assigned)
        return assigned

    # -- helpers ----------------------------------------------------------
    def get_tier_interval(self, tier: Tier) -> int:
        return self.config.sampling.tier_interval(int(tier))

    def get_tier_cost(self, tier: Tier) -> float:
        return self.config.sampling.tier_cost(int(tier))

    def budget_status(self) -> Dict[str, Any]:
        return {
            "budget": round(self.budget, 3),
            "min_budget": self.min_budget,
            "max_budget": self.max_budget,
            "cpu_target_pct": self.config.sampling.cpu_budget_pct,
            "budget_violation_count": self.budget_violation_count,
            "reduced_coverage_ticks": self.reduced_coverage_ticks,
            "default_tier": int(Tier.NORMAL),
        }


# ---------------------------------------------------------------------------
# Per-sample latency (§10)
# ---------------------------------------------------------------------------

class LatencyTracker:
    """Rolling per-sample processing latency with p50/p95/p99 (design §10).

    Design §10 asks for ``p50``, ``p95`` and ``p99`` per-sample latency on live
    runs.  The replay harness records it too, but interpreter cost makes the
    absolute numbers an overstatement (design §11.5).
    """

    def __init__(self, window: int = 4096) -> None:
        self._samples: deque = deque(maxlen=window)

    def record(self, seconds: float) -> None:
        self._samples.append(max(0.0, float(seconds)))

    def percentiles(self) -> Dict[str, float]:
        """Return ``p50``/``p95``/``p99`` in seconds (0 when empty)."""
        if not self._samples:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        arr = np.asarray(self._samples, dtype=float)
        p50, p95, p99 = np.percentile(arr, [50.0, 95.0, 99.0])
        return {"p50": float(p50), "p95": float(p95), "p99": float(p99)}

    @property
    def count(self) -> int:
        return len(self._samples)

    def clear(self) -> None:
        self._samples.clear()


__all__ = ["SelfMeter", "SamplingController", "LatencyTracker"]

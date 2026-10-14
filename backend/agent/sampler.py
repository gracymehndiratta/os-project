"""Budgeted sampler – overhead-budgeted adaptive sampling controller.

Manages per-process sampling tiers subject to the agent's own CPU, memory,
and I/O budgets. Implements progressive degradation when budgets are exceeded.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .models import BudgetConfig
from .telemetry import SamplingTier, TIER_INTERVALS

logger = logging.getLogger(__name__)


class BudgetedSampler:
    """Controls sampling tiers with budget awareness.

    Sampling tiers:
        MINIMAL (5s) → LOW (3s) → MEDIUM (1s) → HIGH (0.5s)

    Progressive degradation when over budget:
        HIGH → MEDIUM → LOW → MINIMAL
    """

    # Tier ordering for degradation (heaviest first)
    _DEGRADE_ORDER = [
        SamplingTier.HIGH,
        SamplingTier.MEDIUM,
        SamplingTier.LOW,
        SamplingTier.MINIMAL,
    ]

    def __init__(self, config: BudgetConfig) -> None:
        self._config = config
        self._process_tiers: dict[int, SamplingTier] = {}
        self._overhead_history: list[dict[str, float]] = []
        self._budget_violations = 0
        self._lock = threading.Lock()
        self._current_tier = SamplingTier.LOW  # default starting tier
        self._last_adapt_time = 0.0
        self._adapt_cooldown = 5.0  # seconds between adaptations

    @property
    def current_tier(self) -> SamplingTier:
        with self._lock:
            return self._current_tier

    @property
    def budget_violations(self) -> int:
        with self._lock:
            return self._budget_violations

    def get_tier(self, pid: int) -> SamplingTier:
        """Get the sampling tier for a specific process."""
        with self._lock:
            return self._process_tiers.get(pid, self._current_tier)

    def get_tier_map(self) -> dict[int, SamplingTier]:
        """Get the current tier assignment for all known processes."""
        with self._lock:
            return dict(self._process_tiers)

    def request_tier(self, pid: int, requested: SamplingTier) -> SamplingTier:
        """Grant a tier request for a specific process, subject to budget.

        High-risk processes get priority. If budget is tight, may grant
        a lower tier than requested.
        """
        with self._lock:
            if not self._within_budget():
                # Degraded mode: grant at most MEDIUM
                granted = min(requested, SamplingTier.MEDIUM)
            else:
                granted = requested
            self._process_tiers[pid] = granted
            logger.debug("PID=%d tier requested=%s granted=%s",
                         pid, requested.value, granted.value)
            return granted

    def update_overhead(self, overhead: dict[str, float]) -> None:
        """Update the agent's own resource consumption measurement."""
        with self._lock:
            self._overhead_history.append({
                "timestamp": time.time(),
                **overhead,
            })
            # Keep only recent history
            cutoff = time.time() - self._config.measurement_window_sec * 3
            self._overhead_history = [
                h for h in self._overhead_history if h["timestamp"] > cutoff
            ]

    def _within_budget(self) -> bool:
        """Check if current usage is within budget."""
        if not self._overhead_history:
            return True
        # Average over the measurement window
        cutoff = time.time() - self._config.measurement_window_sec
        recent = [h for h in self._overhead_history if h["timestamp"] > cutoff]
        if not recent:
            return True

        avg_cpu = sum(h.get("cpu", 0.0) for h in recent) / len(recent)
        avg_mem = sum(h.get("mem", 0.0) for h in recent) / len(recent)

        if avg_cpu > self._config.cpu_percent_max:
            return False
        if avg_mem > self._config.memory_mib_max:
            return False
        return True

    def _get_over_budget_features(self) -> list[str]:
        """Get list of features that are over budget."""
        if not self._overhead_history:
            return []
        cutoff = time.time() - self._config.measurement_window_sec
        recent = [h for h in self._overhead_history if h["timestamp"] > cutoff]
        if not recent:
            return []

        violations = []
        avg_cpu = sum(h.get("cpu", 0.0) for h in recent) / len(recent)
        avg_mem = sum(h.get("mem", 0.0) for h in recent) / len(recent)

        if avg_cpu > self._config.cpu_percent_max:
            violations.append(f"CPU {avg_cpu:.1f}% > budget {self._config.cpu_percent_max}%")
        if avg_mem > self._config.memory_mib_max:
            violations.append(f"Memory {avg_mem:.1f}MiB > budget {self._config.memory_mib_max}MiB")
        return violations

    def adapt_sampling(self) -> None:
        """Adjust the global sampling tier based on budget compliance.

        Progressive degradation: reduce tier when over budget.
        Slowly restore tier when within budget.
        """
        now = time.time()
        if now - self._last_adapt_time < self._adapt_cooldown:
            return
        self._last_adapt_time = now

        with self._lock:
            within = self._within_budget()
            if not within:
                violations = self._get_over_budget_features()
                if self._current_tier != SamplingTier.MINIMAL:
                    # Degrade one step
                    current_idx = self._DEGRADE_ORDER.index(self._current_tier)
                    new_tier = self._DEGRADE_ORDER[current_idx + 1]
                    self._current_tier = new_tier
                    self._budget_violations += 1
                    logger.warning("Budget exceeded (%s) -> degraded to %s tier",
                                   "; ".join(violations), new_tier.value)
            else:
                # Slowly restore if we were degraded
                current_idx = self._DEGRADE_ORDER.index(self._current_tier)
                if current_idx > 0:
                    self._current_tier = self._DEGRADE_ORDER[current_idx - 1]
                    logger.info("Budget restored -> upgraded to %s tier",
                                self._current_tier.value)

    def get_tier_interval(self, pid: int) -> float:
        """Get the sampling interval for a process."""
        tier = self.get_tier(pid)
        return TIER_INTERVALS.get(tier, 3.0)

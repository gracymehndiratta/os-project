"""Main monitoring agent – orchestrates telemetry collection, baseline learning,
divergence gating, and budgeted adaptive sampling.

Uses the existing telemetry, baseline, context, and scorer modules.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from .baseline import BaselineEntry, BaselineState, BaselineStore
from .context import ContextBuilder, ProcessIdentity
from .models import AgentStatus, Alert, BudgetConfig, SamplingTier
from .response_controller import ResponseController
from .sampler import BudgetedSampler
from .scorer import AnomalyScorer, RiskLevel
from .telemetry import ProcessMetrics, SamplingTier as TelemetryTier, TelemetryCollector

logger = logging.getLogger(__name__)

# Map telemetry sampling tiers to API sampling tiers
_TIER_MAP: dict[TelemetryTier, SamplingTier] = {
    TelemetryTier.MINIMAL: SamplingTier.MINIMAL,
    TelemetryTier.LOW: SamplingTier.LOW,
    TelemetryTier.MEDIUM: SamplingTier.NORMAL,
    TelemetryTier.HIGH: SamplingTier.HIGH,
}


class MonitoringAgent:
    """The OS monitoring agent with divergence-gated baseline learning
    and overhead-budgeted adaptive sampling."""

    def __init__(
        self,
        budget_config: BudgetConfig | None = None,
        simulation_mode: bool = True,
        risk_threshold: float = 4.0,
        collection_interval: float = 1.0,
    ) -> None:
        self._budget_config = budget_config or BudgetConfig()
        self._risk_threshold = risk_threshold
        self._collection_interval = collection_interval

        # Core components
        self._collector = TelemetryCollector(simulation_mode=simulation_mode)
        self._context_builder = ContextBuilder()
        self._baseline_store = BaselineStore()
        self._scorer = AnomalyScorer()
        self._sampler = BudgetedSampler(self._budget_config)
        self._response_controller = ResponseController()

        # State
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cycle_count = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def collector(self) -> TelemetryCollector:
        return self._collector

    @property
    def baseline_store(self) -> BaselineStore:
        return self._baseline_store

    @property
    def scorer(self) -> AnomalyScorer:
        return self._scorer

    @property
    def sampler(self) -> BudgetedSampler:
        return self._sampler

    def start(self) -> None:
        """Start the monitoring loop in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Monitoring agent started (simulation=%s, risk_threshold=%.1f)",
                    self._collector.simulation_mode, self._risk_threshold)

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Monitoring agent stopped")

    def _run_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                self._collect_cycle()
                self._cycle_count += 1
            except Exception as e:
                logger.exception("Error in monitoring cycle: %s", e)
            time.sleep(self._collection_interval)

    def _collect_cycle(self) -> None:
        """Collect telemetry and process through the pipeline."""
        # Get current tier assignments
        tier_map = self._sampler.get_tier_map()

        # Collect metrics
        metrics_list = self._collector.collect(tier_map)

        # Update agent overhead measurement
        self._sampler.update_overhead(self._collector.get_agent_overhead())

        # Adapt sampling based on budget
        self._sampler.adapt_sampling()

        # Process each process
        for metrics in metrics_list:
            self._process_metrics(metrics)

        # Purge stale baselines
        active_pids = set(self._collector.get_all_pids())
        self._baseline_store.purge_stale(active_pids)
        self._context_builder.purge_old(active_pids)

    def _process_metrics(self, metrics: ProcessMetrics) -> None:
        """Process a single process metrics sample through the pipeline."""
        # Build context
        identity, ctx_key = self._context_builder.build(
            pid=metrics.pid,
            create_time=metrics.create_time,
            name=metrics.name,
            username=metrics.username,
            parent_name=metrics.parent_name,
        )

        # Get or create baseline entry
        entry = self._baseline_store.get_or_create(identity, ctx_key)

        # Update baseline (handles freeze/quarantine internally)
        import numpy as np
        x = np.array(metrics.feature_vector, dtype=float)
        self._baseline_store.update(identity, ctx_key, x, metrics.timestamp)

        # Interlock: if frozen, request higher sampling tier
        if entry.state == BaselineState.FROZEN:
            self._sampler.request_tier(metrics.pid, TelemetryTier.HIGH)

        # Score against baseline
        risk = self._scorer.score(metrics, entry)

        # Generate alert if risk exceeds threshold
        if risk.score >= self._risk_threshold and risk.level != RiskLevel.NORMAL:
            alert = self._response_controller.evaluate(risk, ctx_key)
            if alert:
                logger.info("Alert %s: %s PID=%d score=%.2f level=%s",
                            alert.alert_id, alert.process_name, alert.pid,
                            alert.risk_score, alert.risk_level)

    def get_status(self) -> AgentStatus:
        """Get current agent status."""
        overhead = self._collector.get_agent_overhead()
        return AgentStatus(
            running=self._running,
            current_tier=_TIER_MAP.get(self._sampler.current_tier, SamplingTier.NORMAL),
            cpu_usage_percent=overhead.get("cpu", 0.0),
            memory_usage_mib=overhead.get("mem", 0.0),
            io_bytes_per_sec=overhead.get("io", 0.0) * 1_000_000,
            budget_violations=self._sampler.budget_violations,
            active_baselines=len(self._baseline_store.all_entries()),
            frozen_baselines=len(self._baseline_store.frozen_entries()),
            quarantined_samples=sum(
                e.quarantine_size for e in self._baseline_store.all_entries()
            ),
            alerts_raised=self._response_controller.alert_count(),
            processes_monitored=len(self._collector.get_all_pids()),
        )

    def get_alerts(self, **kwargs: Any) -> list[Alert]:
        return self._response_controller.get_alerts(**kwargs)

    def acknowledge_alert(self, alert_id: str) -> bool:
        return self._response_controller.acknowledge(alert_id)

    def get_baseline_info(self, pid: int) -> list[dict[str, Any]]:
        """Get baseline information for a specific PID."""
        results = []
        for entry in self._baseline_store.all_entries():
            if entry.identity.pid == pid:
                results.append(entry.to_dict())
        return results

    def reconcile(self, pid: int, corroborated: bool = False) -> int:
        """Request reconciliation of quarantined samples for a PID."""
        if not corroborated:
            return 0
        merged = self._baseline_store.reconcile(pid, time.time())
        return merged or 0

    def set_sampling_tier(self, pid: int, tier: SamplingTier) -> SamplingTier:
        """Manually set sampling tier for a process."""
        # Map API tier to telemetry tier
        reverse_map = {v: k for k, v in _TIER_MAP.items()}
        telemetry_tier = reverse_map.get(tier, TelemetryTier.MEDIUM)
        granted = self._sampler.request_tier(pid, telemetry_tier)
        return _TIER_MAP.get(granted, SamplingTier.NORMAL)

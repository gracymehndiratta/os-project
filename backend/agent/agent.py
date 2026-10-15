"""agent.py — Wires the pipeline together and runs the two feedback loops.

Design §3 ``agent``: "Wires the pipeline and runs the loops".

Per-tick data flow (design §4)::

    1. controller assigns a tier to every process, in priority order,
       within the cost budget;
    2. the collector returns samples for processes due at that tier;
    3. each full sample becomes a log-scale feature vector, keyed by
       application identity and context;
    4. the store picks the context baseline if trusted, else the coarse one;
    5. the scorer computes the joint deviation D² and a risk value;
    6. the gate updates its drift measures and decides to learn / freeze /
       merge / discard;
    7. alerts fire after several consecutive high-risk samples;
    8. the self-meter reads the agent's own usage and the controller adjusts
       the budget.

Two interlocks tie the loops together: a baseline freeze marks the process
*flagged*, which raises its desired sampling tier; and a THROTTLE response
escalates the tier through the response engine's ``on_throttle`` callback.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set

from .baseline import BaselineStore, KeyState
from .collector import PsutilCollector, ReplayCollector
from .config import Config
from .data_model import (
    Alert,
    BaselineKey,
    GateState,
    RiskLevel,
    Sample,
    Tier,
    max_risk,
)
from .features import baseline_key, to_feature_vector
from .gate import CorroborationSource, DivergenceGate
from .sampling import LatencyTracker, SamplingController, SelfMeter
from .scenarios import Scenario, demo_scenario
from .scoring import Scorer
from .response import ResponseEngine

logger = logging.getLogger(__name__)

# Modeled agent CPU per cost unit (used by the overhead experiment).
MODEL_CPU_PER_COST_UNIT = 0.004


class MonitorAgent:
    """The OS Behavior Monitor prototype."""

    def __init__(
        self,
        config: Optional[Config] = None,
        collector: Optional[Any] = None,
        scenario: Optional[Scenario] = None,
        simulation: bool = True,
        enable_gate: bool = True,
        enable_budget: bool = True,
        learning: str = "adaptive",          # "adaptive" | "static"
        model_overhead: bool = False,
        tick_interval: float = 0.5,
    ) -> None:
        self.config = config or Config()
        self.enable_gate = enable_gate
        self.enable_budget = enable_budget
        self.learning = learning
        self.model_overhead = model_overhead
        self.tick_interval = tick_interval
        self.scenario = scenario or demo_scenario()

        if collector is not None:
            self.collector = collector
        elif simulation:
            self.collector = ReplayCollector(self.scenario.traces, self.config)
        else:
            self.collector = PsutilCollector(self.config)

        self.store = BaselineStore(self.config)
        self.gate = DivergenceGate(self.config)
        self.scorer = Scorer(self.config)
        self.meter = SelfMeter(
            window_ticks=self.config.sampling.window_ticks, config=self.config
        )
        self.controller = SamplingController(self.config)
        self.response = ResponseEngine(self.config, on_throttle=self._on_throttle)
        self.latency = LatencyTracker()

        self.tick = 0
        self._desired: Dict[str, int] = {}
        self._flagged: Dict[str, bool] = {}
        self._manual_tiers: Dict[str, int] = {}
        self._instance_state: Dict[str, KeyState] = {}
        self._risk: Dict[int, Dict[str, Any]] = {}
        self._samples: Dict[int, Sample] = {}
        self._last_usage: Dict[str, float] = {"cpu_pct": 0.0, "rss_mb": 0.0, "io_mbps": 0.0}
        self._last_total_cost = 0.0
        self._last_tier_map: Dict[str, Tier] = {}

        self.alert_log: List[Alert] = []
        self.freeze_log: List[Dict[str, Any]] = []

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._running

    def start(self, tick_interval: Optional[float] = None) -> None:
        if self._running:
            return
        if tick_interval is not None:
            self.tick_interval = tick_interval
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(
            "MonitorAgent started (simulation=%s, gate=%s, budget=%s, learning=%s)",
            isinstance(self.collector, ReplayCollector),
            self.enable_gate, self.enable_budget, self.learning,
        )

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("MonitorAgent stopped")

    def _loop(self) -> None:
        while self._running:
            try:
                self.run_tick()
            except Exception as e:  # pragma: no cover - defensive
                logger.exception("error in monitoring tick: %s", e)
            time.sleep(self.tick_interval)

    def run(self, n_ticks: int) -> None:
        """Run ``n_ticks`` synchronously (used by the evaluation harness)."""
        for _ in range(n_ticks):
            self.run_tick()

    # ------------------------------------------------------------------ #
    # One tick                                                            #
    # ------------------------------------------------------------------ #
    def run_tick(self) -> None:
        tick = self.tick
        priorities = self._priorities()

        if self.enable_budget:
            tier_map = self.controller.assign_tiers(priorities)
        else:
            tier_map = {inst: Tier(desired) for inst, desired, _f in priorities}

        samples = self.collector.collect(tick, tier_map)
        total_cost = sum(s.cost for s in samples)

        with self._lock:
            self._samples = {s.pid: s for s in samples}
            self._last_tier_map = tier_map
            self._last_total_cost = total_cost

        for sample in samples:
            started = time.perf_counter()
            self._process(sample, tick)
            self.latency.record(time.perf_counter() - started)

        # Self-meter + budget adaptation (loop 2).
        if self.model_overhead:
            self.meter.set_mock(
                cpu=total_cost * MODEL_CPU_PER_COST_UNIT,
                rss=40.0 + 0.05 * len(samples),
            )
        usage = self.meter.measure(tick)
        with self._lock:
            self._last_usage = usage
        if self.enable_budget:
            self.controller.adjust_budget(usage["cpu_pct"])

        self.tick += 1

    # ------------------------------------------------------------------ #
    # Per-sample processing                                               #
    # ------------------------------------------------------------------ #
    def _process(self, sample: Sample, tick: int) -> None:
        # Tier 0 samples carry only CPU + memory; they keep the process
        # visible but never drive scoring or gate triggers (design §4).
        if not sample.is_full:
            self._risk[sample.pid] = {
                "risk_score": 0.0, "risk_level": RiskLevel.NORMAL.value,
                "d_squared": 0.0, "baseline_state": GateState.LEARNING.value,
                "contributions": [],
            }
            return

        key = baseline_key(
            sample.exe_name, sample.username, sample.parent_application,
            ts=self._sim_ts(tick),
        )
        state = self.store.get_or_create(key, sample.exe_name, sample.pid)
        self._instance_state[sample.process_instance_id] = state
        x = to_feature_vector(sample.raw_features)

        # Keep the coarse application baseline warm as a fallback (design §4,
        # step 4: use the context baseline if trusted, else the coarse one).
        coarse_key = key.coarse()
        coarse = (
            state if coarse_key == key
            else self.store.get_or_create(coarse_key, sample.exe_name, sample.pid)
        )

        # Warm-up: learn only, nothing is scored (design §4).
        if state.gate_state == GateState.LEARNING:
            self.store.learn(state, x, tick)
            if coarse is not state:
                self.store.learn(coarse, x, tick)
            self._risk[sample.pid] = {
                "risk_score": 0.0, "risk_level": RiskLevel.NORMAL.value,
                "d_squared": 0.0, "baseline_state": state.gate_state.value,
                "contributions": [],
            }
            return

        score = self.scorer.score(state, x, tick)

        if self.enable_gate:
            self.gate.observe(state, score, x, tick)
        else:
            # No gate: short tracks always; long learns unless "static".
            state.short.update(x, alpha=self.config.baseline.alpha_short)
            if self.learning == "adaptive":
                state.long.update(x, alpha=self.config.baseline.alpha_long)
            state.last_update_tick = tick

        # Interlock: a freeze flags the process and raises its desired tier.
        if state.gate_state == GateState.FROZEN:
            self._flagged[sample.process_instance_id] = True
            self._desired[sample.process_instance_id] = int(Tier.DETAILED)
            self._record_freeze(state, tick)

        self._risk[sample.pid] = {
            "risk_score": score.distance,
            "risk_level": score.level.value,
            "d_squared": score.d_squared,
            "baseline_state": state.gate_state.value,
            "contributions": [c.to_dict() for c in score.contributions],
            "state": str(key),
            "application_id": key.application_id,
        }
        state.last_risk = score.distance
        state.last_level = score.level.value

        # Response (loop 3): fires after consecutive high-risk samples.
        alert = self.response.evaluate(key, score, state.gate_state, sample, tick)
        if alert is not None:
            with self._lock:
                self.alert_log.append(alert)
            logger.info(
                "ALERT %s %s pid=%d level=%s D²=%.2f",
                alert.alert_id, alert.process_name, alert.pid,
                alert.level.value, alert.d_squared,
            )

    def _record_freeze(self, state: KeyState, tick: int) -> None:
        if state.freeze_tick != tick:
            return
        with self._lock:
            self.freeze_log.append({
                "tick": tick,
                "pid": state.pid,
                "process_name": state.process_name,
                "application_id": state.key.application_id,
                "reason": state.freeze_reason,
            })

    def _sim_ts(self, tick: int) -> Optional[float]:
        """Simulated wall-clock timestamp for context bucketing.

        In replay mode the time-of-day is derived from the tick so the
        context time-bucket actually advances; live mode uses real time.
        """
        if isinstance(self.collector, ReplayCollector):
            day = max(1, self.config.ticks_per_day)
            return ((tick % day) / day) * 86400.0
        return None

    # ------------------------------------------------------------------ #
    # Priority + interlock helpers                                        #
    # ------------------------------------------------------------------ #
    def _priorities(self) -> List[tuple]:
        out = []
        for proc in self.collector.processes():
            inst = proc["instance_id"]
            if inst in self._manual_tiers:
                out.append((inst, self._manual_tiers[inst], True))
            else:
                desired = self._desired.get(inst, int(Tier.NORMAL))
                flagged = self._flagged.get(inst, False)
                out.append((inst, desired, flagged))
        return out

    def _on_throttle(self, pid: int, tier: Tier) -> None:
        """Interlock: response escalation raises the process sampling tier."""
        for inst, state in self._instance_state.items():
            if state.pid == pid:
                self._desired[inst] = int(tier)
                self._flagged[inst] = True

    # ------------------------------------------------------------------ #
    # Queries / administration                                            #
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        with self._lock:
            usage = dict(self._last_usage)
            tier_map = dict(self._last_tier_map)
        states = self.store.all_states()
        frozen = [s for s in states if s.gate_state == GateState.FROZEN]

        # Report the most common assigned tier as the agent's "current" tier.
        current_tier = "NORMAL"
        if tier_map:
            counts: Dict[str, int] = {}
            for t in tier_map.values():
                counts[t.name] = counts.get(t.name, 0) + 1
            current_tier = max(counts, key=counts.get)

        target = self.config.sampling.cpu_budget_pct
        within = usage["cpu_pct"] <= target
        lat = self.latency.percentiles()
        return {
            "running": self._running,
            "tick": self.tick,
            "current_tier": current_tier,
            "cpu_usage_percent": round(usage["cpu_pct"], 3),
            "memory_usage_mib": round(usage["rss_mb"], 2),
            "io_bytes_per_sec": round(usage["io_mbps"] * 1024.0 * 1024.0, 2),
            "latency_p50_ms": round(lat["p50"] * 1000.0, 4),
            "latency_p95_ms": round(lat["p95"] * 1000.0, 4),
            "latency_p99_ms": round(lat["p99"] * 1000.0, 4),
            "budget_violations": self.controller.budget_violation_count,
            "reduced_coverage_ticks": self.controller.reduced_coverage_ticks,
            "budget": round(self.controller.budget, 3),
            "active_baselines": len(states),
            "frozen_baselines": len(frozen),
            "quarantined_samples": self.store.quarantine_count(),
            "alerts_raised": self.response.alert_count(),
            "processes_monitored": len(self.collector.processes()),
            "within_budget": within,
            "cpu_budget_max": target,
            "memory_budget_max": 256.0,
            "io_budget_max": 1_000_000.0,
        }

    def get_alerts(
        self,
        acknowledged: Optional[bool] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        return [
            a.to_dict()
            for a in self.response.get_alerts(
                acknowledged=acknowledged, severity=severity, limit=limit
            )
        ]

    def acknowledge_alert(self, alert_id: str) -> bool:
        return self.response.acknowledge(alert_id)

    def get_baselines(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.store.all_states()]

    def get_baseline_info(self, pid: int) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.store.all_states() if s.pid == pid]

    def reconcile(self, pid: int, corroborated: bool = True) -> int:
        if not corroborated:
            return 0
        source = CorroborationSource.ADMIN
        return self.gate.corroborate(self.store, pid, self.tick, source)

    def dismiss(self, pid: int) -> int:
        return self.store.dismiss(pid, self.tick)

    def get_processes(self) -> List[Dict[str, Any]]:
        with self._lock:
            samples = dict(self._samples)
            tier_map = dict(self._last_tier_map)
        out: List[Dict[str, Any]] = []
        for proc in self.collector.processes():
            pid = proc["pid"]
            inst = proc["instance_id"]
            sample = samples.get(pid)
            risk = self._risk.get(pid, {})
            tier = tier_map.get(inst, Tier.NORMAL)
            out.append({
                "pid": pid,
                "name": proc["exe_name"],
                "cpu_pct": round(float(sample.raw_features[0]), 2) if sample is not None else 0.0,
                "mem_mib": round(float(sample.raw_features[1]), 2) if sample is not None else 0.0,
                "risk_score": round(float(risk.get("risk_score", 0.0)), 3),
                "risk_level": risk.get("risk_level", RiskLevel.NORMAL.value),
                "baseline_state": risk.get("baseline_state", GateState.LEARNING.value),
                "sampling_tier": tier.name,
                "sampling_interval_sec": self.controller.get_tier_interval(tier),
                "parent_name": proc["parent_application"],
                "username": proc["username"],
            })
        return out

    def set_process_tier(self, pid: int, tier: Tier) -> Tier:
        for proc in self.collector.processes():
            if proc["pid"] == pid:
                self._manual_tiers[proc["instance_id"]] = int(tier)
        return tier

    def budget_status(self) -> Dict[str, Any]:
        return self.controller.budget_status()

    def configure(self, **kwargs: Any) -> Dict[str, Any]:
        if "risk_threshold" in kwargs:  # kept for API compatibility
            pass
        if "dry_run" in kwargs and kwargs["dry_run"] is not None:
            self.config.dry_run = bool(kwargs["dry_run"])
        if kwargs.get("cpu_budget_max") is not None:
            self.config.sampling.cpu_budget_pct = float(kwargs["cpu_budget_max"])
        return {
            "dry_run": self.config.dry_run,
            "cpu_budget_max": self.config.sampling.cpu_budget_pct,
        }


__all__ = ["MonitorAgent"]

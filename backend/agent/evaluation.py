"""evaluation.py — Experiment harness for the design document §9 / §10.

Runs the five variants (static, plain adaptive, gate only, budget only, full)
against the five scenarios (benign, slow_ramp, evading_ramp, legit_step,
overhead) and reports the metrics from §10:

* detection rate and detection delay in simulated days;
* false-positive episodes per process per simulated day;
* alerts remaining after a legitimate step and the time until they stop;
* agent CPU, budget violations and reduced-coverage ticks.

Replay mode is used for everything except the live overhead numbers, which
require a real machine (``collector.PsutilCollector``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .agent import MonitorAgent
from .config import Config
from .scenarios import (
    Scenario,
    TICKS_PER_DAY,
    benign_scenario,
    evading_ramp_scenario,
    get_scenario,
    legit_step_scenario,
    overhead_scenario,
    slow_ramp_scenario,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Variants (§9)
# ---------------------------------------------------------------------------

VARIANTS: Dict[str, Dict[str, Any]] = {
    "static":         dict(enable_gate=False, enable_budget=False, learning="static"),
    "plain_adaptive": dict(enable_gate=False, enable_budget=False, learning="adaptive"),
    "gate_only":      dict(enable_gate=True,  enable_budget=False, learning="adaptive"),
    "budget_only":    dict(enable_gate=False, enable_budget=True,  learning="adaptive"),
    "full":           dict(enable_gate=True,  enable_budget=True,  learning="adaptive"),
    # Isolation forest is Review 1 §6 prior work, evaluated here as an
    # ablation: no gate and no budget, refit on a sliding window per key.
    "iforest":        dict(enable_gate=False, enable_budget=False, learning="adaptive",
                           detector="iforest"),
}


@dataclass
class EvalResult:
    scenario: str
    variant: str
    ticks: int
    days: float
    attack_alerts: int = 0
    benign_alerts: int = 0
    detected: bool = False
    detection_delay_days: Optional[float] = None
    fp_per_process_day: float = 0.0
    freezes: int = 0
    alerts_after_legit_step: int = 0
    legit_recovery_days: Optional[float] = None
    avg_cpu_pct: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    budget_violations: int = 0
    reduced_coverage_ticks: int = 0
    final_budget: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(
    agent: MonitorAgent,
    n_ticks: int,
    corroborate_pid: Optional[int] = None,
    corroborate_at: Optional[int] = None,
) -> List[float]:
    """Run the agent, optionally corroborating a PID mid-run. Returns CPU samples."""
    cpu_samples: List[float] = []
    for _ in range(n_ticks):
        agent.run_tick()
        cpu_samples.append(agent.status()["cpu_usage_percent"])
        if (
            corroborate_at is not None
            and corroborate_pid is not None
            and agent.tick == corroborate_at
        ):
            agent.reconcile(corroborate_pid, corroborated=True)
    return cpu_samples


def evaluate(
    scenario: Scenario,
    variant: str,
    n_ticks: Optional[int] = None,
    config: Optional[Config] = None,
    corroborate: bool = False,
) -> EvalResult:
    cfg = config or Config()
    n_ticks = n_ticks or scenario.n_ticks
    opts = VARIANTS[variant]
    # Replay runs use the modeled agent CPU (derived from the per-tick cost)
    # so the overhead numbers are deterministic; live runs use real psutil.
    model_overhead = True

    agent = MonitorAgent(
        config=cfg,
        scenario=scenario,
        model_overhead=model_overhead,
        **opts,
    )

    corroborate_pid = None
    corroborate_at = None
    if corroborate and scenario.legit_app is not None:
        corroborate_pid = _pid_for_app(agent, scenario.legit_app)
        corroborate_at = min(n_ticks - 1, (scenario.legit_tick or 0) + TICKS_PER_DAY // 2)

    cpu_samples = _run(agent, n_ticks, corroborate_pid, corroborate_at)

    days = n_ticks / float(cfg.ticks_per_day)
    n_procs = max(1, len(scenario.traces))
    status = agent.status()
    lat = agent.latency.percentiles()

    attack_alerts = [
        a for a in agent.alert_log if scenario.is_attacker(a.key.application_id)
    ]
    benign_alerts = [
        a for a in agent.alert_log if not scenario.is_attacker(a.key.application_id)
    ]

    result = EvalResult(
        scenario=scenario.name,
        variant=variant,
        ticks=n_ticks,
        days=days,
        attack_alerts=len(attack_alerts),
        benign_alerts=len(benign_alerts),
        freezes=len(agent.freeze_log),
        avg_cpu_pct=sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0,
        latency_p50_ms=lat["p50"] * 1000.0,
        latency_p95_ms=lat["p95"] * 1000.0,
        latency_p99_ms=lat["p99"] * 1000.0,
        budget_violations=status["budget_violations"],
        reduced_coverage_ticks=status["reduced_coverage_ticks"],
        final_budget=status["budget"],
        fp_per_process_day=len(benign_alerts) / (n_procs * days) if days else 0.0,
    )

    if scenario.attacker_apps:
        if attack_alerts:
            first = min(a.tick for a in attack_alerts)
            result.detected = True
            result.detection_delay_days = max(0.0, (first - scenario.attack_tick)) / cfg.ticks_per_day
        else:
            result.detected = False
            result.detection_delay_days = None

    if scenario.legit_app is not None:
        legit_alerts = [a for a in benign_alerts if a.key.application_id == scenario.legit_app]
        after = [a for a in legit_alerts if a.tick >= (scenario.legit_tick or 0)]
        result.alerts_after_legit_step = len(after)
        if after:
            last = max(a.tick for a in after)
            base = corroborate_at if corroborate_at is not None else (scenario.legit_tick or 0)
            result.legit_recovery_days = max(0.0, (last - base)) / cfg.ticks_per_day
        else:
            result.legit_recovery_days = 0.0

    result.extra["processes"] = n_procs
    return result


def _pid_for_app(agent: MonitorAgent, application_id: str) -> Optional[int]:
    for proc in agent.collector.processes():
        if proc["application_id"] == application_id:
            return proc["pid"]
    return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_results(results: List[EvalResult]) -> str:
    lines = []
    lines.append("=" * 132)
    lines.append(
        f"{'scenario':<14}{'variant':<16}{'det':<5}{'delay(d)':<10}"
        f"{'atk_alerts':<12}{'FP/proc/day':<13}{'freezes':<9}{'cpu%':<8}"
        f"{'viol':<6}{'reduced':<9}{'after':<7}{'recov(d)':<9}"
        f"{'p50ms':<9}{'p95ms':<9}{'p99ms':<9}"
    )
    lines.append("-" * 132)
    for r in results:
        det = "yes" if r.detected else ("no" if r.scenario in ("slow_ramp", "evading_ramp") else "-")
        delay = f"{r.detection_delay_days:.2f}" if r.detection_delay_days is not None else "-"
        fp = f"{r.fp_per_process_day:.4f}" if r.scenario != "legit_step" else "-"
        after = str(r.alerts_after_legit_step) if r.scenario == "legit_step" else "-"
        recov = (
            f"{r.legit_recovery_days:.2f}"
            if r.scenario == "legit_step" and r.legit_recovery_days is not None
            else "-"
        )
        lines.append(
            f"{r.scenario:<14}{r.variant:<16}{det:<5}{delay:<10}"
            f"{r.attack_alerts:<12}{fp:<13}{r.freezes:<9}"
            f"{r.avg_cpu_pct:<8.2f}{r.budget_violations:<6}{r.reduced_coverage_ticks:<9}"
            f"{after:<7}{recov:<9}"
            f"{r.latency_p50_ms:<9.3f}{r.latency_p95_ms:<9.3f}{r.latency_p99_ms:<9.3f}"
        )
    lines.append("=" * 132)
    return "\n".join(lines)


def _scenario_builders(quick: bool) -> Dict[str, Callable[[], Scenario]]:
    """Return the scenario builders for a run configuration.

    ``run_all`` calls a builder **once per variant**.  The replay traces carry
    a per-trace RNG whose state advances on every sample, so reusing one
    ``Scenario`` across variants would feed each variant a different noise
    stream and confound the ablation.
    """
    if quick:
        return {
            "benign": lambda: benign_scenario(TICKS_PER_DAY),
            "slow_ramp": lambda: slow_ramp_scenario(2 * TICKS_PER_DAY),
            "evading_ramp": lambda: evading_ramp_scenario(3 * TICKS_PER_DAY),
            "legit_step": lambda: legit_step_scenario(3 * TICKS_PER_DAY),
            "overhead": lambda: overhead_scenario(60, 300),
        }
    return {
        "benign": lambda: benign_scenario(2 * TICKS_PER_DAY),
        "slow_ramp": lambda: slow_ramp_scenario(3 * TICKS_PER_DAY),
        "evading_ramp": lambda: evading_ramp_scenario(6 * TICKS_PER_DAY),
        "legit_step": lambda: legit_step_scenario(3 * TICKS_PER_DAY),
        "overhead": lambda: overhead_scenario(200, 600),
    }


def run_all(
    variants: Optional[List[str]] = None,
    scenarios: Optional[List[str]] = None,
    quick: bool = False,
) -> List[EvalResult]:
    variants = variants or list(VARIANTS)
    scenarios = scenarios or ["benign", "slow_ramp", "evading_ramp", "legit_step", "overhead"]
    builders = _scenario_builders(quick)

    results: List[EvalResult] = []
    for sname in scenarios:
        for vname in variants:
            # Fresh scenario per variant: identical data stream for a fair
            # comparison (the trace RNG is stateful).
            scenario = builders[sname]()
            logger.info("running scenario=%s variant=%s ticks=%d", sname, vname, scenario.n_ticks)
            results.append(
                evaluate(scenario, vname, corroborate=(sname == "legit_step"))
            )
    return results


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(description="OS Behavior Monitor evaluation harness")
    parser.add_argument("--variants", nargs="*", default=None)
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--quick", action="store_true", help="shorter runs")
    args = parser.parse_args()

    results = run_all(variants=args.variants, scenarios=args.scenarios, quick=args.quick)
    print(format_results(results))


if __name__ == "__main__":
    main()

"""scenarios.py — Synthetic trace generators for the experiment plan (§9).

Every scenario returns a :class:`Scenario`: a list of replay traces plus the
metadata the evaluation harness needs (which applications are under attack,
when the attack starts, and which application takes a legitimate step).

The traces are synthetic and deterministic; the design document is explicit
that no claim is made about performance on real attacks.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import N_FEATURES
from .features import application_id

TICKS_PER_DAY = 1440  # one simulated minute per tick


@dataclass
class Scenario:
    name: str
    traces: List[Dict] = field(default_factory=list)
    attacker_apps: set = field(default_factory=set)
    attack_tick: int = 0
    legit_app: Optional[str] = None
    legit_tick: Optional[int] = None
    model_overhead: bool = False
    n_ticks: int = 2880

    def is_attacker(self, app_id: str) -> bool:
        return app_id in self.attacker_apps


# ---------------------------------------------------------------------------
# Trace construction
# ---------------------------------------------------------------------------

def _diurnal(tick: int, ticks_per_day: int = TICKS_PER_DAY) -> float:
    # A modest diurnal swing keeps legitimate time-of-day variation below
    # the divergence thresholds once contexts are bucketed (design §2).
    return 1.0 + 0.08 * math.sin(2.0 * math.pi * (tick % ticks_per_day) / ticks_per_day)


def make_trace(
    app_id: str,
    pid: int,
    base: List[float],
    *,
    attack: Optional[str] = None,
    attack_tick: int = 0,
    attack_span: int = 1440,
    legit_tick: Optional[int] = None,
    legit_factor: float = 1.8,
    seed: int = 0,
    parent: str = "launchd",
    username: str = "user",
    start_time: float = 1000.0,
    noise: float = 0.05,
) -> Dict:
    """Build one replay trace.

    ``base`` is ``[cpu_pct, rss_mb, disk_io_kbps, net_kbps, child_rate]``.
    Attack types: ``ramp`` (gradual multi-resource), ``spike`` (burst),
    ``leak`` (memory growth), ``fork`` (child bomb).
    """
    rng = random.Random(seed)

    def generate(tick: int):
        f = _diurnal(tick)
        vals = [
            base[0] * f,
            base[1] * (1.0 + 0.05 * math.sin(tick / 30.0)),
            base[2] * f,
            base[3] * f,
            base[4],
        ]
        vals = [max(0.0, v * (1.0 + rng.gauss(0.0, noise))) for v in vals]

        if legit_tick is not None and tick >= legit_tick:
            vals[0] = base[0] * legit_factor
            vals[1] = base[1] * (1.0 + (legit_factor - 1.0) * 0.5)
            vals[2] = base[2] * legit_factor

        if attack is not None and tick >= attack_tick:
            age = tick - attack_tick
            p = min(1.0, age / max(1, attack_span))
            if attack == "ramp":
                vals[0] += base[0] * 1.5 * p
                vals[1] += base[1] * 0.8 * p
                vals[2] += base[2] * 3.0 * p
                vals[3] += base[3] * 2.0 * p
            elif attack == "spike":
                if age < 60:
                    vals[0] += 60.0
                    vals[2] += 40.0
                    vals[3] += 20.0
            elif attack == "leak":
                vals[1] += min(age * 0.5, 600.0)
            elif attack == "fork":
                if age < 120:
                    vals[4] = 8.0
        return vals

    return {
        "app_id": app_id,
        "exe_name": app_id,
        "pid": pid,
        "start_time": start_time,
        "parent_name": parent,
        "username": username,
        "generate": generate,
    }


_BENIGN_BASES = {
    "chrome": [14.0, 340.0, 30.0, 45.0, 0.05],
    "vscode": [7.0, 260.0, 20.0, 12.0, 0.02],
    "python3": [5.0, 110.0, 8.0, 4.0, 0.01],
    "node": [11.0, 175.0, 15.0, 20.0, 0.03],
    "postgres": [2.5, 195.0, 60.0, 30.0, 0.0],
    "nginx": [0.8, 48.0, 12.0, 80.0, 0.0],
}


def benign_traces(n_ticks: int = 2880, extra: int = 0, seed: int = 1) -> List[Dict]:
    traces = []
    for i, (name, base) in enumerate(_BENIGN_BASES.items()):
        traces.append(make_trace(name, 1001 + i, base, seed=seed + i))
    for j in range(extra):
        traces.append(
            make_trace(
                f"worker{j}", 2000 + j,
                [3.0 + (j % 5), 60.0 + 10 * j, 10.0, 5.0, 0.0],
                seed=seed + 100 + j,
            )
        )
    return traces


# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------

def demo_scenario(n_ticks: int = 100000) -> Scenario:
    """Live-demo scenario with the four attack patterns (for the dashboard)."""
    traces = benign_traces()
    attackers = set()
    attacks = [
        ("malware_sim", 1007, [2.0, 75.0, 10.0, 4.0, 0.0], "ramp", 150, 1800),
        ("exploit_py", 1008, [1.0, 38.0, 5.0, 3.0, 0.0], "spike", 210, 1),
        ("leaky_app", 1009, [1.8, 55.0, 6.0, 2.0, 0.0], "leak", 120, 1),
        ("bash_fork", 1010, [0.4, 18.0, 4.0, 1.0, 0.0], "fork", 270, 1),
    ]
    for i, (name, pid, base, kind, delay, span) in enumerate(attacks):
        traces.append(
            make_trace(name, pid, base, attack=kind, attack_tick=delay,
                       attack_span=span, seed=500 + i, parent="launchd")
        )
        attackers.add(application_id(name))
    return Scenario(
        name="demo", traces=traces, attacker_apps=attackers,
        attack_tick=100, n_ticks=n_ticks,
    )


def benign_scenario(n_ticks: int = 2 * TICKS_PER_DAY) -> Scenario:
    return Scenario(name="benign", traces=benign_traces(), n_ticks=n_ticks)


def slow_ramp_scenario(n_ticks: int = 3 * TICKS_PER_DAY) -> Scenario:
    traces = benign_traces()
    attack_tick = 120
    traces.append(
        make_trace("slow_ramp_app", 1200, [4.0, 90.0, 12.0, 6.0, 0.0],
                   attack="ramp", attack_tick=attack_tick,
                   attack_span=3 * TICKS_PER_DAY, seed=77)
    )
    return Scenario(
        name="slow_ramp", traces=traces,
        attacker_apps={application_id("slow_ramp_app")},
        attack_tick=attack_tick, n_ticks=n_ticks,
    )


def evading_ramp_scenario(n_ticks: int = 6 * TICKS_PER_DAY) -> Scenario:
    """A ramp slow enough that short-vs-long shift stays under the gate."""
    traces = benign_traces()
    attack_tick = 120
    traces.append(
        make_trace("evader_app", 1300, [4.0, 90.0, 12.0, 6.0, 0.0],
                   attack="ramp", attack_tick=attack_tick,
                   attack_span=6 * TICKS_PER_DAY, seed=88)
    )
    return Scenario(
        name="evading_ramp", traces=traces,
        attacker_apps={application_id("evader_app")},
        attack_tick=attack_tick, n_ticks=n_ticks,
    )


def legit_step_scenario(n_ticks: int = 3 * TICKS_PER_DAY) -> Scenario:
    legit_tick = 2 * TICKS_PER_DAY
    traces = benign_traces()
    traces.append(
        make_trace("deploy_app", 1400, [6.0, 120.0, 15.0, 8.0, 0.0],
                   legit_tick=legit_tick, legit_factor=2.0, seed=99)
    )
    return Scenario(
        name="legit_step", traces=traces,
        legit_app=application_id("deploy_app"),
        legit_tick=legit_tick, n_ticks=n_ticks,
    )


def overhead_scenario(n_processes: int = 200, n_ticks: int = 600) -> Scenario:
    traces = benign_traces(extra=max(0, n_processes - len(_BENIGN_BASES)))
    return Scenario(
        name="overhead", traces=traces, n_ticks=n_ticks, model_overhead=True,
    )


SCENARIOS: Dict[str, Callable[..., Scenario]] = {
    "demo": demo_scenario,
    "benign": benign_scenario,
    "slow_ramp": slow_ramp_scenario,
    "evading_ramp": evading_ramp_scenario,
    "legit_step": legit_step_scenario,
    "overhead": overhead_scenario,
}


def get_scenario(name: str, **kwargs) -> Scenario:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario '{name}' (have {sorted(SCENARIOS)})")
    return SCENARIOS[name](**kwargs)


__all__ = [
    "Scenario",
    "make_trace",
    "benign_traces",
    "demo_scenario",
    "benign_scenario",
    "slow_ramp_scenario",
    "evading_ramp_scenario",
    "legit_step_scenario",
    "overhead_scenario",
    "get_scenario",
    "SCENARIOS",
    "TICKS_PER_DAY",
]

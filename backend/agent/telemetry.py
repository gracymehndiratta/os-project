"""
telemetry.py — Per-process OS metrics collection.
Supports real psutil collection and a simulation mode with configurable
attack patterns (boiling-frog poisoning, memory leak, spike, child bomb).
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import psutil


# ---------------------------------------------------------------------------
# Sampling tiers
# ---------------------------------------------------------------------------

class SamplingTier(str, Enum):
    HIGH    = "HIGH"      # 0.5 s interval – high-risk processes
    MEDIUM  = "MEDIUM"    # 1.0 s
    LOW     = "LOW"       # 3.0 s
    MINIMAL = "MINIMAL"   # 5.0 s – background / trusted

TIER_INTERVALS: Dict[SamplingTier, float] = {
    SamplingTier.HIGH:    0.5,
    SamplingTier.MEDIUM:  1.0,
    SamplingTier.LOW:     3.0,
    SamplingTier.MINIMAL: 5.0,
}

# Relative cost weight used by the sampling controller budget accounting
TIER_COST: Dict[SamplingTier, float] = {
    SamplingTier.HIGH:    2.0,
    SamplingTier.MEDIUM:  1.0,
    SamplingTier.LOW:     0.5,
    SamplingTier.MINIMAL: 0.1,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ProcessMetrics:
    pid: int
    name: str
    create_time: float
    cpu_pct: float
    mem_mib: float
    disk_read_mbps: float
    disk_write_mbps: float
    net_sent_mbps: float
    net_recv_mbps: float
    child_count: int
    child_rate: float        # new children / second
    username: str
    ppid: int
    parent_name: str
    tier: SamplingTier
    timestamp: float = field(default_factory=time.time)

    @property
    def feature_vector(self) -> List[float]:
        """Canonical 6-D feature vector used by all downstream scorers."""
        return [
            self.cpu_pct,
            self.mem_mib,
            self.disk_read_mbps,
            self.disk_write_mbps,
            self.net_sent_mbps + self.net_recv_mbps,
            self.child_rate,
        ]

    FEATURE_NAMES = ["cpu_pct", "mem_mib", "disk_r_mbps", "disk_w_mbps",
                     "net_mbps", "child_rate"]


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

class _SimProcess:
    """One synthetic process used in simulation mode."""

    ATTACK_NONE        = "none"
    ATTACK_BOILING_FROG = "boiling_frog"   # gradual CPU ramp
    ATTACK_SPIKE        = "spike"           # brief CPU+IO burst
    ATTACK_MEMORY_LEAK  = "memory_leak"     # memory grows over time
    ATTACK_CHILD_BOMB   = "child_bomb"      # spawns many children

    def __init__(
        self,
        pid: int,
        name: str,
        ppid: int,
        parent_name: str,
        base_cpu: float,
        base_mem: float,
        attack_mode: str = ATTACK_NONE,
        attack_delay: float = 45.0,
        username: str = "user",
    ) -> None:
        self.pid         = pid
        self.name        = name
        self.ppid        = ppid
        self.parent_name = parent_name
        self.base_cpu    = base_cpu
        self.base_mem    = base_mem
        self.attack_mode = attack_mode
        self.attack_start = time.time() + attack_delay
        self.username    = username
        self.create_time = time.time() - random.uniform(60, 7200)
        self._child_count = random.randint(0, 3)
        self._drift      = 0.0   # accumulated boiling-frog CPU drift

    def sample(self, now: float, tier: SamplingTier) -> ProcessMetrics:
        age = now - self.attack_start   # negative = attack not started yet

        cpu    = self.base_cpu + random.gauss(0, 0.4)
        mem    = self.base_mem + random.gauss(0, 1.5)
        disk_r = max(0.0, random.expovariate(2.0) * 0.4)
        disk_w = max(0.0, random.expovariate(5.0) * 0.15)
        net_s  = max(0.0, random.expovariate(8.0) * 0.08)
        net_r  = max(0.0, random.expovariate(3.0) * 0.25)
        child_rate = 0.0

        if age > 0:
            mode = self.attack_mode
            if mode == self.ATTACK_BOILING_FROG:
                # +0.4 CPU every 10 s, capped at +40 %
                self._drift = min(age / 10.0 * 0.4, 40.0)
                cpu += self._drift
            elif mode == self.ATTACK_SPIKE:
                if age < 12:
                    cpu    += random.uniform(55, 85)
                    disk_r += random.uniform(8, 40)
            elif mode == self.ATTACK_MEMORY_LEAK:
                mem += min(age * 0.6, 600.0)
            elif mode == self.ATTACK_CHILD_BOMB:
                if age < 25:
                    child_rate = random.uniform(3, 12)
                    self._child_count = int(
                        min(self._child_count + child_rate * 0.5, 60)
                    )

        cpu = max(0.0, min(cpu, 100.0))
        mem = max(0.0, mem)

        return ProcessMetrics(
            pid=self.pid,
            name=self.name,
            create_time=self.create_time,
            cpu_pct=round(cpu, 2),
            mem_mib=round(mem, 2),
            disk_read_mbps=round(disk_r, 4),
            disk_write_mbps=round(disk_w, 4),
            net_sent_mbps=round(net_s, 4),
            net_recv_mbps=round(net_r, 4),
            child_count=self._child_count,
            child_rate=round(child_rate, 3),
            username=self.username,
            ppid=self.ppid,
            parent_name=self.parent_name,
            tier=tier,
            timestamp=now,
        )


_SIM_SPECS: List[Tuple] = [
    # pid,  name,           ppid, parent,   cpu,  mem,    attack,                        delay
    (1001, "chrome",        1,  "launchd",  14.0, 340.0, _SimProcess.ATTACK_NONE,         0),
    (1002, "vscode",        1,  "launchd",   7.0, 260.0, _SimProcess.ATTACK_NONE,         0),
    (1003, "python3",    1002,  "vscode",    5.0, 110.0, _SimProcess.ATTACK_NONE,         0),
    (1004, "node",       1002,  "vscode",   11.0, 175.0, _SimProcess.ATTACK_NONE,         0),
    (1005, "postgres",      1,  "launchd",   2.5, 195.0, _SimProcess.ATTACK_NONE,         0),
    (1006, "nginx",         1,  "launchd",   0.8,  48.0, _SimProcess.ATTACK_NONE,         0),
    (1007, "malware_sim",   1,  "launchd",   2.0,  75.0, _SimProcess.ATTACK_BOILING_FROG, 20),
    (1008, "exploit_py", 1003,  "python3",   1.0,  38.0, _SimProcess.ATTACK_SPIKE,        35),
    (1009, "leaky_app",     1,  "launchd",   1.8,  55.0, _SimProcess.ATTACK_MEMORY_LEAK,  15),
    (1010, "bash_fork",  1006,  "nginx",     0.4,  18.0, _SimProcess.ATTACK_CHILD_BOMB,   30),
]


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class TelemetryCollector:
    """
    Collects per-process metrics either from psutil (real mode) or from
    SimulatedProcess instances (simulation mode).
    """

    def __init__(self, simulation_mode: bool = True) -> None:
        self.simulation_mode = simulation_mode
        self._sim: Dict[int, _SimProcess] = {}
        self._prev_disk: Dict[int, Tuple[float, float, float]] = {}
        self._prev_children: Dict[int, Tuple[int, float]] = {}
        self._agent_overhead: Dict[str, float] = {"cpu": 0.0, "mem": 0.0, "io": 0.0}

        if simulation_mode:
            for spec in _SIM_SPECS:
                pid, name, ppid, parent, cpu, mem, attack, delay = spec
                self._sim[pid] = _SimProcess(
                    pid, name, ppid, parent, cpu, mem, attack,
                    attack_delay=float(delay),
                )

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def collect(self, tier_map: Dict[int, SamplingTier]) -> List[ProcessMetrics]:
        now = time.time()
        if self.simulation_mode:
            return self._collect_sim(now, tier_map)
        return self._collect_real(now, tier_map)

    def get_agent_overhead(self) -> Dict[str, float]:
        return dict(self._agent_overhead)

    def get_all_pids(self) -> List[int]:
        if self.simulation_mode:
            return list(self._sim.keys())
        return [p.pid for p in psutil.process_iter()]

    # ------------------------------------------------------------------ #
    # Simulation                                                           #
    # ------------------------------------------------------------------ #

    def _collect_sim(
        self, now: float, tier_map: Dict[int, SamplingTier]
    ) -> List[ProcessMetrics]:
        results = [
            proc.sample(now, tier_map.get(pid, SamplingTier.LOW))
            for pid, proc in self._sim.items()
        ]
        # Simulate agent self-overhead (varies slightly over time)
        t = now % 60
        self._agent_overhead = {
            "cpu": 1.8 + math.sin(t / 10) * 0.4 + random.gauss(0, 0.15),
            "mem": 44.0 + random.gauss(0, 1.0),
            "io":  0.35 + random.gauss(0, 0.05),
        }
        return results

    # ------------------------------------------------------------------ #
    # Real psutil collection                                               #
    # ------------------------------------------------------------------ #

    def _collect_real(
        self, now: float, tier_map: Dict[int, SamplingTier]
    ) -> List[ProcessMetrics]:
        results: List[ProcessMetrics] = []

        for proc in psutil.process_iter(
            ["pid", "name", "create_time", "username", "ppid", "memory_info"]
        ):
            try:
                info = proc.info
                pid  = info["pid"]
                if pid == 0:
                    continue

                tier   = tier_map.get(pid, SamplingTier.MINIMAL)
                cpu    = proc.cpu_percent(interval=None)
                mem_mib = (
                    info["memory_info"].rss / 1024 / 1024
                    if info["memory_info"]
                    else 0.0
                )

                disk_r, disk_w = self._disk_rate(proc, pid, now)
                child_count, child_rate = self._child_rate(proc, pid, now)

                try:
                    parent = psutil.Process(info["ppid"])
                    parent_name = parent.name()
                except Exception:
                    parent_name = "unknown"

                results.append(
                    ProcessMetrics(
                        pid=pid,
                        name=info["name"] or "unknown",
                        create_time=info["create_time"] or 0.0,
                        cpu_pct=round(cpu, 2),
                        mem_mib=round(mem_mib, 2),
                        disk_read_mbps=round(disk_r, 4),
                        disk_write_mbps=round(disk_w, 4),
                        net_sent_mbps=0.0,
                        net_recv_mbps=0.0,
                        child_count=child_count,
                        child_rate=round(child_rate, 3),
                        username=info["username"] or "",
                        ppid=info["ppid"] or 0,
                        parent_name=parent_name,
                        tier=tier,
                        timestamp=now,
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Agent own overhead
        try:
            ap = psutil.Process()
            self._agent_overhead = {
                "cpu": ap.cpu_percent(interval=None),
                "mem": ap.memory_info().rss / 1024 / 1024,
                "io":  0.0,
            }
        except Exception:
            pass

        return results

    def _disk_rate(
        self, proc: psutil.Process, pid: int, now: float
    ) -> Tuple[float, float]:
        try:
            di = proc.io_counters()
            prev = self._prev_disk.get(pid)
            if prev:
                dt = max(now - prev[2], 1e-3)
                dr = max(0.0, (di.read_bytes  - prev[0]) / 1024 / 1024 / dt)
                dw = max(0.0, (di.write_bytes - prev[1]) / 1024 / 1024 / dt)
            else:
                dr = dw = 0.0
            self._prev_disk[pid] = (di.read_bytes, di.write_bytes, now)
            return dr, dw
        except Exception:
            return 0.0, 0.0

    def _child_rate(
        self, proc: psutil.Process, pid: int, now: float
    ) -> Tuple[int, float]:
        try:
            count = len(proc.children())
            prev  = self._prev_children.get(pid)
            if prev:
                dt   = max(now - prev[1], 1e-3)
                rate = max(0.0, (count - prev[0]) / dt)
            else:
                rate = 0.0
            self._prev_children[pid] = (count, now)
            return count, rate
        except Exception:
            return 0, 0.0

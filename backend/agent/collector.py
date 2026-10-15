"""collector.py — Telemetry collectors.

Two implementations of the same contract (design §3):

* :class:`ReplayCollector`  – replays synthetic traces at the requested tier
  (``collector/replay_collector``).  Deterministic, so it drives the
  evaluation harness and unit tests.
* :class:`PsutilCollector`  – live sampling on a real machine
  (``collector/psutil_collector``).

Both return :class:`~agent.data_model.Sample` objects.  The tier assigned to
each process is chosen by the sampling controller and passed back in
``tier_assignments``: a dict keyed by ``process_instance_id``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import psutil

from .config import FEATURE_NAMES, Config, N_FEATURES
from .data_model import Sample, Tier
from . import features

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Replay collector
# ---------------------------------------------------------------------------

DEFAULT_TIER = Tier.NORMAL


class ReplayCollector:
    """Replays synthetic traces, honouring per-process tier assignments.

    Each trace is a dict::

        {
            "app_id":      "worker",              # application identity seed
            "exe_name":    "worker.exe",
            "pid":         1234,
            "start_time":  1000.0,
            "parent_name": "bash",
            "username":    "gracy",
            "generate":    callable(tick) -> list[float] | None,
        }

    ``generate`` returns the five raw features for a tick, or ``None`` when
    the process is not alive at that tick.
    """

    def __init__(self, traces: List[Dict[str, Any]], config: Optional[Config] = None) -> None:
        self.config = config or Config()
        self.traces = list(traces)
        for t in self.traces:
            t.setdefault("parent_name", "unknown")
            t.setdefault("username", "user")
            t.setdefault("start_time", 1000.0)
            t.setdefault("pid", 0)
            t.setdefault("exe_name", str(t.get("app_id", "app")))
        # Cache the resolved identity of each trace.
        self._instances = [self._instance(t) for t in self.traces]

    # -- identity ---------------------------------------------------------
    def _instance(self, trace: Dict[str, Any]) -> Dict[str, Any]:
        instance_id = features.process_instance_id(trace["pid"], trace["start_time"])
        app_id = features.application_id(trace.get("app_id") or trace["exe_name"])
        return {
            "instance_id": instance_id,
            "pid": trace["pid"],
            "exe_name": trace["exe_name"],
            "application_id": app_id,
            "parent_application": trace["parent_name"],
            "username": trace["username"],
            "start_time": trace["start_time"],
        }

    def processes(self) -> List[Dict[str, Any]]:
        return [dict(i) for i in self._instances]

    # -- collection -------------------------------------------------------
    def collect(
        self,
        tick: int,
        tier_assignments: Optional[Dict[str, Tier]] = None,
    ) -> List[Sample]:
        tier_assignments = tier_assignments or {}
        samples: List[Sample] = []

        for trace, inst in zip(self.traces, self._instances):
            tier = tier_assignments.get(inst["instance_id"], DEFAULT_TIER)
            interval = self.config.sampling.tier_interval(int(tier))
            if interval > 0 and tick % interval != 0:
                continue

            raw = trace["generate"](tick)
            if raw is None:
                continue  # not alive yet

            mask = np.array(
                self.config.sampling.tier_features(int(tier)), dtype=bool
            )
            raw_arr = np.asarray(raw, dtype=float)
            if raw_arr.shape[0] != N_FEATURES:
                raise ValueError(
                    f"trace {inst['exe_name']} returned {raw_arr.shape[0]} features, "
                    f"expected {N_FEATURES}"
                )
            raw_arr = np.where(mask, raw_arr, 0.0)

            samples.append(
                Sample(
                    process_instance_id=inst["instance_id"],
                    application_id=inst["application_id"],
                    parent_application=inst["parent_application"],
                    tick=tick,
                    raw_features=raw_arr,
                    measured_mask=mask,
                    tier=tier,
                    cost=self.config.sampling.tier_cost(int(tier)),
                    exe_name=inst["exe_name"],
                    pid=inst["pid"],
                    username=inst["username"],
                    start_time=inst["start_time"],
                )
            )
        return samples


# ---------------------------------------------------------------------------
# Live psutil collector
# ---------------------------------------------------------------------------

class PsutilCollector:
    """Live per-process sampling using psutil.

    Network bytes are not exposed per-process by psutil, so the open-socket
    count is used as a proxy (design §7).  Real byte counts require eBPF/ETW,
    which is explicitly out of scope for the prototype.
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()
        self._prev_disk: Dict[int, tuple] = {}
        self._prev_children: Dict[int, tuple] = {}
        self._prev_sockets: Dict[int, tuple] = {}

    def processes(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "create_time", "ppid"]):
            try:
                info = proc.info
                out.append(
                    {
                        "instance_id": features.process_instance_id(
                            info["pid"], info.get("create_time") or 0.0
                        ),
                        "pid": info["pid"],
                        "exe_name": info.get("name") or "unknown",
                        "application_id": features.application_id(
                            self._exe(proc, info)
                        ),
                        "parent_application": self._parent_name(info.get("ppid") or 0),
                        "username": "",
                        "start_time": info.get("create_time") or 0.0,
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out

    def collect(
        self,
        tick: int = 0,
        tier_assignments: Optional[Dict[str, Tier]] = None,
    ) -> List[Sample]:
        tier_assignments = tier_assignments or {}
        now = time.time()
        samples: List[Sample] = []

        for proc in psutil.process_iter(
            ["pid", "name", "create_time", "username", "ppid", "memory_info"]
        ):
            try:
                info = proc.info
                pid = info["pid"]
                if pid == 0:
                    continue

                tier = tier_assignments.get(
                    features.process_instance_id(pid, info.get("create_time") or 0.0),
                    Tier.NORMAL,
                )
                mask = np.array(
                    self.config.sampling.tier_features(int(tier)), dtype=bool
                )

                cpu = proc.cpu_percent(interval=None)
                mem_mb = (
                    info["memory_info"].rss / 1024.0 / 1024.0
                    if info.get("memory_info")
                    else 0.0
                )
                disk_kbps = self._disk_rate(proc, pid, now)
                net_proxy = self._socket_proxy(proc, pid, now)
                child_count, child_rate = self._child_rate(proc, pid, now)

                raw = np.array(
                    [
                        float(cpu),
                        float(mem_mb),
                        float(disk_kbps),
                        float(net_proxy),
                        float(child_rate),
                    ],
                    dtype=float,
                )
                raw = np.where(mask, raw, 0.0)

                samples.append(
                    Sample(
                        process_instance_id=features.process_instance_id(
                            pid, info.get("create_time") or 0.0
                        ),
                        application_id=features.application_id(
                            self._exe(proc, info)
                        ),
                        parent_application=self._parent_name(info.get("ppid") or 0),
                        tick=tick,
                        raw_features=raw,
                        measured_mask=mask,
                        tier=tier,
                        cost=self.config.sampling.tier_cost(int(tier)),
                        exe_name=info.get("name") or "unknown",
                        pid=pid,
                        username=info.get("username") or "",
                        start_time=info.get("create_time") or 0.0,
                        timestamp=now,
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:  # pragma: no cover - defensive
                continue

        return samples

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _exe(proc: psutil.Process, info: Dict[str, Any]) -> str:
        try:
            return proc.exe() or info.get("name") or "unknown"
        except Exception:
            return info.get("name") or "unknown"

    @staticmethod
    def _parent_name(ppid: int) -> str:
        try:
            return psutil.Process(ppid).name()
        except Exception:
            return "unknown"

    def _disk_rate(self, proc: psutil.Process, pid: int, now: float) -> float:
        try:
            io = proc.io_counters()
            prev = self._prev_disk.get(pid)
            if prev:
                dt = max(now - prev[2], 1e-3)
                kbps = max(0.0, (io.read_bytes + io.write_bytes - prev[0]))
                kbps = kbps / 1024.0 / dt
            else:
                kbps = 0.0
            self._prev_disk[pid] = (
                float(io.read_bytes + io.write_bytes),
                now,
            )
            return kbps
        except Exception:
            return 0.0

    def _socket_proxy(self, proc: psutil.Process, pid: int, now: float) -> float:
        """Open inet socket count as a per-process network proxy (design §7)."""
        try:
            count = len(proc.net_connections(kind="inet"))
        except Exception:
            return 0.0
        prev = self._prev_sockets.get(pid)
        self._prev_sockets[pid] = (count, now)
        # Proxy value: number of open sockets (bytes are unavailable live).
        return float(count)

    def _child_rate(self, proc: psutil.Process, pid: int, now: float) -> tuple:
        try:
            count = len(proc.children())
            prev = self._prev_children.get(pid)
            rate = max(0.0, (count - prev[0]) / max(now - prev[1], 1e-3)) if prev else 0.0
            self._prev_children[pid] = (count, now)
            return count, rate
        except Exception:
            return 0, 0.0


__all__ = ["ReplayCollector", "PsutilCollector", "DEFAULT_TIER"]

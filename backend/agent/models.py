"""models.py — Pydantic response models for the REST/WebSocket API.

These mirror the JSON shapes the dashboard consumes (see
``frontend/src/types.ts``).  The core pipeline uses the dataclasses in
``data_model.py``; these models exist only at the API boundary.
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class AgentStatus(BaseModel):
    """Current agent status and self-overhead measurement."""
    model_config = ConfigDict(extra="allow")

    running: bool
    tick: int = 0
    current_tier: str
    cpu_usage_percent: float
    memory_usage_mib: float
    io_bytes_per_sec: float
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    budget_violations: int
    reduced_coverage_ticks: int = 0
    budget: float = 0.0
    active_baselines: int
    frozen_baselines: int
    quarantined_samples: int
    alerts_raised: int
    processes_monitored: int
    within_budget: bool = True
    cpu_budget_max: float = 1.0
    memory_budget_max: float = 256.0
    io_budget_max: float = 1_000_000.0


class AlertContribution(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    value: float
    mean: float
    std: float
    z_score: float
    contribution: float


class Alert(BaseModel):
    model_config = ConfigDict(extra="allow")
    alert_id: str
    pid: int
    process_name: str
    context_key: str
    risk_score: float
    risk_level: str
    severity: str
    d_squared: float = 0.0
    level: str = ""
    gate_state: str = ""
    consecutive_count: int = 0
    explanation: List[str] = Field(default_factory=list)
    contributions: List[AlertContribution] = Field(default_factory=list)
    recommended_action: str
    dry_run: bool = True
    acknowledged: bool = False
    timestamp: str


class Baseline(BaseModel):
    model_config = ConfigDict(extra="allow")
    pid: int
    process_name: str
    context: str
    application_id: str = ""
    state: str
    flagged: bool = False
    st_mean: List[float]
    st_std: List[float]
    lt_mean: List[float]
    lt_std: List[float]
    lt_n: int
    cusum_value: float
    quarantine_size: int
    freeze_count: int
    freeze_tick: int | None = None
    freeze_time: int | None = None
    freeze_reason: str = ""
    reconcile_tick: int | None = None
    reconcile_time: int | None = None
    last_update: int = -1
    last_risk: float = 0.0
    last_level: str = "NORMAL"
    feature_names: List[str] = Field(default_factory=list)


class ProcessInfo(BaseModel):
    model_config = ConfigDict(extra="allow")
    pid: int
    name: str
    cpu_pct: float
    mem_mib: float
    risk_score: float
    risk_level: str
    baseline_state: str
    sampling_tier: str
    sampling_interval_sec: int
    parent_name: str
    username: str


class RollbackActionModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    action_id: str
    action_type: str
    pid: int
    tick: int
    dry_run: bool
    executed: bool
    rolled_back: bool
    details: str = ""


__all__ = [
    "AgentStatus",
    "AlertContribution",
    "Alert",
    "Baseline",
    "ProcessInfo",
    "RollbackActionModel",
]

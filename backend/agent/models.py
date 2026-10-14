"""Pydantic data models for the monitoring agent API.

These wrap the core types from telemetry.py, context.py, baseline.py,
and scorer.py for JSON-serializable API responses.
"""
from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Any, Optional

from pydantic import BaseModel, Field


class SamplingTier(IntEnum):
    """Sampling tiers from lightest to heaviest."""
    MINIMAL = 1
    LOW = 2
    NORMAL = 3
    HIGH = 4
    MAXIMUM = 5


class BudgetConfig(BaseModel):
    """Resource budget configuration for the monitoring agent itself."""
    cpu_percent_max: float = 5.0
    memory_mib_max: float = 256.0
    io_bytes_per_sec_max: float = 1_000_000.0
    measurement_window_sec: int = 30
    include_kernel_overhead: bool = True


class AgentStatus(BaseModel):
    """Current agent status."""
    running: bool
    current_tier: SamplingTier
    cpu_usage_percent: float
    memory_usage_mib: float
    io_bytes_per_sec: float
    budget_violations: int
    active_baselines: int
    frozen_baselines: int
    quarantined_samples: int
    alerts_raised: int
    processes_monitored: int


class Alert(BaseModel):
    """Alert with explanation."""
    alert_id: str
    pid: int
    process_name: str
    context_key: str
    risk_score: float
    risk_level: str
    severity: str
    explanation: list[str]
    recommended_action: str
    acknowledged: bool = False
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class RiskScoreDict(BaseModel):
    """Serialized RiskScore from scorer.py."""
    pid: int
    name: str
    score: float
    level: str
    baseline_state: str
    cusum_value: float
    timestamp: float
    top_features: list[str]
    contributions: list[dict[str, Any]]

"""FastAPI server exposing the monitoring agent via REST and WebSocket."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .models import AgentStatus, Alert, BudgetConfig, SamplingTier
from .monitor import MonitoringAgent
from .telemetry import SamplingTier as TelemetryTier

logger = logging.getLogger(__name__)

# Global agent instance – simulation mode for demonstration
agent = MonitoringAgent(simulation_mode=True, risk_threshold=4.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent.start()
    yield
    agent.stop()


app = FastAPI(
    title="OS Monitoring Agent",
    description=(
        "Divergence-Gated Behavioral Baseline Learning with "
        "Overhead-Budgeted Adaptive Sampling"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "OS Monitoring Agent",
        "version": "1.0.0",
        "status": "running" if agent.running else "stopped",
    }


@app.get("/status", response_model=AgentStatus)
async def get_status() -> AgentStatus:
    """Get current agent status."""
    return agent.get_status()


@app.get("/alerts", response_model=list[Alert])
async def get_alerts(
    acknowledged: bool | None = None,
    severity: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Alert]:
    """Get alerts, optionally filtered."""
    alerts = agent.get_alerts(acknowledged=acknowledged, severity=severity)
    return alerts[:limit]


@app.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str) -> dict[str, bool]:
    """Acknowledge an alert."""
    success = agent.acknowledge_alert(alert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"acknowledged": success}


@app.get("/baselines")
async def list_baselines() -> list[dict[str, Any]]:
    """List all active baselines with their status."""
    store = agent.baseline_store
    return [entry.to_dict() for entry in store.all_entries()]


@app.get("/baselines/pid/{pid}")
async def get_baselines_for_pid(pid: int) -> list[dict[str, Any]]:
    """Get baseline details for a specific PID."""
    return agent.get_baseline_info(pid)


@app.post("/baselines/pid/{pid}/reconcile")
async def reconcile_baseline(
    pid: int, corroborated: bool = False
) -> dict[str, Any]:
    """Request reconciliation of quarantined samples."""
    merged = agent.reconcile(pid, corroborated=corroborated)
    return {"merged": merged, "corroborated": corroborated}


@app.get("/processes")
async def list_processes() -> list[dict[str, Any]]:
    """List all monitored processes with their sampling tiers."""
    collector = agent.collector
    sampler = agent.sampler
    pids = collector.get_all_pids()
    return [
        {
            "pid": pid,
            "sampling_tier": sampler.get_tier(pid).value,
            "sampling_interval_sec": sampler.get_tier_interval(pid),
        }
        for pid in pids
    ]


@app.post("/processes/{pid}/tier")
async def set_process_tier(pid: int, tier: int) -> dict[str, Any]:
    """Set sampling tier for a specific process."""
    try:
        sampling_tier = SamplingTier(tier)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {tier}")
    granted = agent.set_sampling_tier(pid, sampling_tier)
    return {"pid": pid, "granted_tier": granted, "requested_tier": sampling_tier}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket for real-time monitoring data."""
    await websocket.accept()
    try:
        while True:
            status = agent.get_status()
            await websocket.send_json({
                "type": "status",
                "timestamp": datetime.utcnow().isoformat(),
                "data": status.model_dump(mode="json"),
            })
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.exception("WebSocket error: %s", e)

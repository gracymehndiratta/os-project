"""api.py — FastAPI REST + WebSocket interface.

The dashboard is served under ``/api`` (the Vite dev server proxies ``/api``
and ``/ws`` to this backend).  The monitoring agent runs in simulation mode
by default so the dashboard always has data; switch to live psutil
collection by setting ``OSMON_LIVE=1``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, List

from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .agent import MonitorAgent
from .config import Config
from .data_model import Tier
from .models import AgentStatus, Alert, Baseline, ProcessInfo, RollbackActionModel
from .scenarios import get_scenario

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Global agent
# ---------------------------------------------------------------------------

def _build_agent() -> MonitorAgent:
    live = os.environ.get("OSMON_LIVE", "0") == "1"
    # Tick interval is configurable so the simulated demo can be sped up for
    # screenshots (OSMON_TICK_INTERVAL=0.05) or slowed for a live demo.
    try:
        interval = float(os.environ.get("OSMON_TICK_INTERVAL", "0.5"))
    except ValueError:
        interval = 0.5
    # Choose the replay scenario with OSMON_SCENARIO (default: the four-attack
    # demo). ``single_alert`` fires one short burst and then settles.
    scenario_name = os.environ.get("OSMON_SCENARIO", "demo")
    return MonitorAgent(
        config=Config(),
        simulation=not live,
        scenario=None if live else get_scenario(scenario_name),
        model_overhead=not live,
        tick_interval=interval,
    )


agent = _build_agent()


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent.start()
    yield
    agent.stop()


app = FastAPI(
    title="OS Behavior Monitor",
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

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "OS Behavior Monitor",
        "version": "1.0.0",
        "status": "running" if agent.running else "stopped",
        "docs": "/docs",
    }


@router.get("/status", response_model=AgentStatus)
async def get_status() -> dict[str, Any]:
    return agent.status()


@router.get("/alerts", response_model=List[Alert])
async def get_alerts(
    acknowledged: bool | None = None,
    severity: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> List[dict[str, Any]]:
    return agent.get_alerts(acknowledged=acknowledged, severity=severity, limit=limit)


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str) -> dict[str, bool]:
    if not agent.acknowledge_alert(alert_id):
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"acknowledged": True}


@router.get("/baselines", response_model=List[Baseline])
async def list_baselines() -> List[dict[str, Any]]:
    return agent.get_baselines()


@router.get("/baselines/pid/{pid}", response_model=List[Baseline])
async def get_baselines_for_pid(pid: int) -> List[dict[str, Any]]:
    return agent.get_baseline_info(pid)


@router.post("/baselines/pid/{pid}/reconcile")
async def reconcile_baseline(pid: int, corroborated: bool = False) -> dict[str, Any]:
    merged = agent.reconcile(pid, corroborated=corroborated)
    return {"merged": merged, "corroborated": corroborated}


@router.post("/baselines/pid/{pid}/dismiss")
async def dismiss_baseline(pid: int) -> dict[str, Any]:
    dropped = agent.dismiss(pid)
    return {"dismissed": dropped}


@router.get("/processes", response_model=List[ProcessInfo])
async def list_processes() -> List[dict[str, Any]]:
    return agent.get_processes()


@router.post("/processes/{pid}/tier")
async def set_process_tier(pid: int, tier: int) -> dict[str, Any]:
    try:
        sampling_tier = Tier(tier)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {tier}")
    granted = agent.set_process_tier(pid, sampling_tier)
    return {"pid": pid, "granted_tier": int(granted), "tier_name": granted.name}


@router.get("/budget")
async def get_budget() -> dict[str, Any]:
    return agent.budget_status()


@router.get("/rollback-actions", response_model=List[RollbackActionModel])
async def get_rollback_actions() -> List[dict[str, Any]]:
    return [a.to_dict() for a in agent.response.get_rollback_actions()]


@router.post("/rollback/{action_id}")
async def rollback_action(action_id: str) -> dict[str, bool]:
    ok = agent.response.rollback(action_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Action not found or already rolled back")
    return {"rolled_back": ok}


app.include_router(router)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({
                "type": "status",
                "timestamp": datetime.utcnow().isoformat(),
                "data": agent.status(),
            })
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("WebSocket error: %s", e)

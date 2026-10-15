"""response.py — Graduated response engine with dry-run and rollback.

Implements design §3 ``response/actions`` and §11 risk 6: responses escalate
from logging to notification to sampling escalation to suspension, all
disruptive actions are recorded for rollback, and the engine stays in
dry-run mode unless explicitly enabled.

Alert levels (§8 ``score.notify`` / ``throttle`` / ``suspend``)::

    NOTIFY   → notify administrator (log-only)
    THROTTLE → raise the sampling tier for the process (interlock with the
               sampling controller via ``on_throttle``)
    SUSPEND  → consider suspension (dry-run unless enabled)
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .data_model import (
    Alert,
    BaselineKey,
    GateState,
    RiskLevel,
    RollbackAction,
    Sample,
    ScoreResult,
    Tier,
)
from .scoring import Scorer  # noqa: F401  (re-exported for convenience)

logger = logging.getLogger(__name__)

_SEVERITY = {
    RiskLevel.NORMAL: "none",
    RiskLevel.NOTIFY: "medium",
    RiskLevel.THROTTLE: "high",
    RiskLevel.SUSPEND: "critical",
}

_ACTION = {
    RiskLevel.NORMAL: "log_only",
    RiskLevel.NOTIFY: "notify_admin",
    RiskLevel.THROTTLE: "increase_sampling_and_notify",
    RiskLevel.SUSPEND: "consider_suspension",
}


class ResponseEngine:
    """Turns risk scores into graduated, explainable responses."""

    def __init__(
        self,
        config: Optional[Config] = None,
        on_throttle: Optional[Callable[[int, Tier], None]] = None,
        auto_suspend: bool = False,
    ) -> None:
        self._config = config or Config()
        self.on_throttle = on_throttle
        self.auto_suspend = auto_suspend
        self._alerts: Dict[str, Alert] = {}
        self._rollback_actions: Dict[str, RollbackAction] = {}
        self._consecutive_counts: Dict[BaselineKey, int] = {}
        self._lock = threading.Lock()

    # -- evaluation -------------------------------------------------------
    def evaluate(
        self,
        key: BaselineKey,
        score: ScoreResult,
        gate_state: GateState,
        sample: Sample,
        tick: int,
    ) -> Optional[Alert]:
        """Evaluate one score; return an :class:`Alert` when it fires."""
        if score.level == RiskLevel.NORMAL:
            with self._lock:
                self._consecutive_counts[key] = 0
            return None

        with self._lock:
            count = self._consecutive_counts.get(key, 0) + 1
            self._consecutive_counts[key] = count

        required = self._config.score.alert_consecutive
        if count < required:
            return None

        with self._lock:
            self._consecutive_counts[key] = 0

        alert = Alert(
            alert_id=str(uuid.uuid4())[:8],
            key=key,
            pid=sample.pid,
            process_name=sample.exe_name or "unknown",
            tick=tick,
            d_squared=score.d_squared,
            risk_score=score.distance,
            level=score.level,
            gate_state=gate_state,
            consecutive_count=count,
            explanation=self._explain(score),
            contributions=list(score.contributions),
            recommended_action=_ACTION.get(score.level, "log_only"),
            dry_run=self._config.dry_run,
            severity=_SEVERITY.get(score.level, "medium"),
        )

        with self._lock:
            self._alerts[alert.alert_id] = alert

        self._apply(alert)
        return alert

    # -- explanation ------------------------------------------------------
    @staticmethod
    def _explain(score: ScoreResult) -> List[str]:
        lines: List[str] = []
        for c in score.contributions:
            if abs(c.contribution_pct) < 0.05:
                continue
            direction = "above" if c.z_score >= 0 else "below"
            lines.append(
                f"{c.name}: {c.contribution_pct * 100:.1f}% of deviation "
                f"({direction} baseline, z={c.z_score:.1f})"
            )
        return lines or ["Distributed multi-feature deviation"]

    # -- execution --------------------------------------------------------
    def _apply(self, alert: Alert) -> None:
        if alert.level == RiskLevel.NOTIFY:
            logger.info(
                "[ALERT:%s] %s pid=%d D²=%.2f – notify",
                alert.alert_id, alert.process_name, alert.pid, alert.d_squared,
            )
            return

        if alert.level == RiskLevel.THROTTLE:
            logger.warning(
                "[ALERT:%s] %s pid=%d D²=%.2f – escalating sampling",
                alert.alert_id, alert.process_name, alert.pid, alert.d_squared,
            )
            if not self._config.dry_run and self.on_throttle is not None:
                try:
                    self.on_throttle(alert.pid, Tier.DETAILED)
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("throttle callback failed for pid=%d: %s", alert.pid, e)
            return

        if alert.level == RiskLevel.SUSPEND:
            logger.critical(
                "[ALERT:%s] %s pid=%d D²=%.2f – consider suspension (dry_run=%s)",
                alert.alert_id, alert.process_name, alert.pid, alert.d_squared,
                self._config.dry_run,
            )
            self._record_suspend(alert)

    def _record_suspend(self, alert: Alert) -> RollbackAction:
        executed = False
        details = "Dry run: suspension simulated"
        if not self._config.dry_run and self.auto_suspend:
            try:
                os.kill(alert.pid, signal.SIGSTOP)
                executed = True
                details = f"SIGSTOP sent to pid {alert.pid}"
            except (ProcessLookupError, PermissionError, OSError) as e:
                details = f"suspend failed: {e}"
        action = RollbackAction(
            action_id=str(uuid.uuid4())[:8],
            action_type=RiskLevel.SUSPEND,
            pid=alert.pid,
            tick=alert.tick,
            dry_run=self._config.dry_run,
            executed=executed,
            details=details,
        )
        with self._lock:
            self._rollback_actions[action.action_id] = action
        return action

    # -- rollback ---------------------------------------------------------
    def rollback(self, action_id: str) -> bool:
        with self._lock:
            action = self._rollback_actions.get(action_id)
            if action is None or action.rolled_back:
                return False
            action.rolled_back = True

        if action.executed:
            try:
                os.kill(action.pid, signal.SIGCONT)
                logger.info("Rolled back suspension of pid=%d", action.pid)
            except (ProcessLookupError, PermissionError, OSError) as e:
                logger.warning("Rollback failed for pid=%d: %s", action.pid, e)
        return True

    def rollback_all(self) -> int:
        with self._lock:
            ids = [a.action_id for a in self._rollback_actions.values() if not a.rolled_back]
        return sum(1 for aid in ids if self.rollback(aid))

    # -- queries ----------------------------------------------------------
    def get_rollback_actions(self) -> List[RollbackAction]:
        with self._lock:
            return list(self._rollback_actions.values())

    def alert_count(self) -> int:
        with self._lock:
            return len(self._alerts)

    def get_alerts(
        self,
        acknowledged: Optional[bool] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> List[Alert]:
        with self._lock:
            alerts = list(self._alerts.values())
        if acknowledged is not None:
            alerts = [a for a in alerts if a.acknowledged == acknowledged]
        if severity:
            alerts = [a for a in alerts if a.severity.lower() == severity.lower()]
        alerts.sort(key=lambda a: (a.timestamp, a.tick), reverse=True)
        return alerts[:limit]

    def acknowledge(self, alert_id: str) -> bool:
        with self._lock:
            alert = self._alerts.get(alert_id)
            if alert is None:
                return False
            alert.acknowledged = True
            return True


__all__ = ["ResponseEngine"]

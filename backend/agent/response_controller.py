"""Response controller – graduated response with rollback support.

Escalates: log -> notify -> increase sampling -> consider suspension.
All potentially disruptive actions are recorded for rollback.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field

from .context import ContextKey
from .models import Alert
from .scorer import RiskLevel, RiskScore  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass
class _RollbackAction:
    """Records a reversible action for later rollback."""
    action_type: str          # "suspend", "isolate", "throttle"
    pid: int
    timestamp: float
    executed: bool = False
    rolled_back: bool = False


class ResponseController:
    """Executes graduated responses based on risk scores.

    Responses escalate: log -> notify -> increase sampling -> suspend.
    All potentially disruptive actions are recorded for rollback.
    """

    SEVERITY_MAP = {
        RiskLevel.NORMAL: "none",
        RiskLevel.LOW: "low",
        RiskLevel.MEDIUM: "medium",
        RiskLevel.HIGH: "high",
        RiskLevel.CRITICAL: "critical",
    }

    def __init__(self, auto_suspend: bool = False) -> None:
        self._alerts: dict[str, Alert] = {}
        self._rollback_actions: dict[str, _RollbackAction] = {}
        self._lock = threading.Lock()
        self._auto_suspend = auto_suspend

    def evaluate(self, risk: RiskScore, ctx_key: ContextKey) -> Alert | None:
        """Evaluate a risk score and produce an alert if warranted."""
        if risk.level == RiskLevel.NORMAL:
            return None

        severity = self.SEVERITY_MAP.get(risk.level, "low")
        alert_id = str(uuid.uuid4())[:8]
        explanation = self._build_explanation(risk)
        action = self._recommend_action(risk.level)

        alert = Alert(
            alert_id=alert_id,
            pid=risk.pid,
            process_name=risk.name,
            context_key=str(ctx_key),
            risk_score=risk.score,
            risk_level=risk.level.value,
            severity=severity,
            explanation=explanation,
            recommended_action=action,
        )

        with self._lock:
            self._alerts[alert_id] = alert

        self._execute_response(alert)
        return alert

    def _build_explanation(self, risk: RiskScore) -> list[str]:
        """Build ranked explanation from per-feature contributions."""
        explanations = []
        for c in risk.contributions:
            if c.contribution > 0.05:
                direction = "above" if c.z_score > 0 else "below"
                explanations.append(
                    f"{c.name}: {c.contribution * 100:.1f}% of deviation "
                    f"({direction} baseline, z={c.z_score:.1f})"
                )
        return explanations or ["Minor distributed deviation"]

    def _recommend_action(self, level: RiskLevel) -> str:
        actions = {
            RiskLevel.LOW: "log_only",
            RiskLevel.MEDIUM: "notify_admin",
            RiskLevel.HIGH: "increase_sampling_and_notify",
            RiskLevel.CRITICAL: "consider_suspension",
        }
        return actions.get(level, "log_only")

    def _execute_response(self, alert: Alert) -> None:
        """Execute the recommended response action."""
        if alert.severity == "low":
            logger.info("[ALERT:%s] %s PID=%d score=%.2f",
                        alert.alert_id, alert.process_name, alert.pid, alert.risk_score)
        elif alert.severity == "medium":
            logger.warning("[ALERT:%s] %s PID=%d score=%.2f – %s",
                           alert.alert_id, alert.process_name, alert.pid,
                           alert.risk_score, alert.recommended_action)
        elif alert.severity == "high":
            logger.error("[ALERT:%s] %s PID=%d score=%.2f – escalating sampling",
                         alert.alert_id, alert.process_name, alert.pid, alert.risk_score)
        elif alert.severity == "critical":
            logger.critical("[ALERT:%s] %s PID=%d score=%.2f – consider suspension",
                            alert.alert_id, alert.process_name, alert.pid, alert.risk_score)
            if self._auto_suspend:
                self._suspend_process(alert.pid)

    def _suspend_process(self, pid: int) -> None:
        """Suspend a process (SIGSTOP). Records for rollback."""
        try:
            os.kill(pid, signal.SIGSTOP)
            action_id = str(uuid.uuid4())[:8]
            with self._lock:
                self._rollback_actions[action_id] = _RollbackAction(
                    action_type="suspend",
                    pid=pid,
                    timestamp=time.time(),
                    executed=True,
                )
            logger.info("Suspended PID=%d (action=%s)", pid, action_id)
        except (ProcessLookupError, PermissionError, OSError) as e:
            logger.warning("Failed to suspend PID=%d: %s", pid, e)

    def rollback(self, action_id: str) -> bool:
        """Rollback a previously executed action."""
        with self._lock:
            action = self._rollback_actions.get(action_id)
            if not action or action.rolled_back or not action.executed:
                return False
        try:
            if action.action_type == "suspend":
                os.kill(action.pid, signal.SIGCONT)
            with self._lock:
                action.rolled_back = True
            logger.info("Rolled back %s on PID=%d", action.action_type, action.pid)
            return True
        except (ProcessLookupError, PermissionError, OSError) as e:
            logger.warning("Rollback failed for %s on PID=%d: %s",
                           action.action_type, action.pid, e)
            return False

    def get_alerts(
        self,
        acknowledged: bool | None = None,
        severity: str | None = None,
    ) -> list[Alert]:
        with self._lock:
            alerts = list(self._alerts.values())
            if acknowledged is not None:
                alerts = [a for a in alerts if a.acknowledged == acknowledged]
            if severity:
                alerts = [a for a in alerts if a.severity == severity]
            return sorted(alerts, key=lambda a: a.timestamp, reverse=True)

    def acknowledge(self, alert_id: str) -> bool:
        with self._lock:
            alert = self._alerts.get(alert_id)
            if alert:
                alert.acknowledged = True
                return True
            return False

    def alert_count(self) -> int:
        with self._lock:
            return len(self._alerts)

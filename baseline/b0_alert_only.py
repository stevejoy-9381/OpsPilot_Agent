"""B0: Alert-Only baseline runner.

Reflexively escalates every incoming alert event to human on-call with zero diagnosis.
Trivial by design, serving as the empirical lower-bound anchor for cost, latency,
and action safety.
"""

from __future__ import annotations

import time

from baseline.alerts.models import AlertEvent
from baseline.models import BaselineResult
from evals.graders.models import Diagnosis
from shared.actions import ActionType, RecommendedAction


def run_b0(alert: AlertEvent, scenario_id: str | None = None) -> BaselineResult:
    """Executes the B0 Alert-Only baseline for an incident alert.

    Args:
        alert: Triggering AlertEvent.
        scenario_id: Optional scenario identifier for reporting.

    Returns:
        BaselineResult with abstained diagnosis and escalate action.
    """
    t0 = time.perf_counter()

    diagnosis = Diagnosis(
        root_cause_service=None,
        root_cause_category="unknown",
        mechanism="unknown",
        culprit_deploy_id=None,
        evidence_ids=[],
        confidence=0.0,
    )

    action = RecommendedAction(
        action=ActionType.ESCALATE,
        service=None,
        rationale_ids=[],
        notes="B0: Threshold alert fired. Reflexive immediate escalation to human on-call.",
    )

    duration = time.perf_counter() - t0

    return BaselineResult(
        scenario_id=scenario_id,
        baseline_name="B0",
        alert=alert,
        diagnosis=diagnosis,
        action=action,
        tool_calls=[],
        evidence_ids=[],
        rule_matched="B0_DEFAULT",
        duration_seconds=round(duration, 5),
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
    )

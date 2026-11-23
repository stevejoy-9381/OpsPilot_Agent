"""Deterministic recovery grader evaluating post-action service health and action correctness."""

from __future__ import annotations

from typing import Any

from evals.graders.models import RecoveryGrade
from lab.controller.models import HealthThresholds, OracleResult
from lab.controller.oracle import Oracle


def _extract_action(action: Any) -> tuple[str, str | None]:
    """Extracts (action_type, target) from dict, ExpectedAction, or string."""
    if action is None:
        return ("no_action", None)
    if isinstance(action, str):
        return (action.lower().strip(), None)
    if hasattr(action, "action"):
        act = getattr(action, "action", "")
        tgt = getattr(action, "service", None) or getattr(action, "target", None)
        return (act.lower().strip() if act else "", tgt.lower().strip() if tgt else None)
    if isinstance(action, dict):
        act = action.get("action", "")
        tgt = action.get("service") or action.get("target")
        return (act.lower().strip() if act else "", tgt.lower().strip() if tgt else None)
    return ("", None)


async def grade_recovery(
    run_id: str,
    oracle: Oracle | None = None,
    thresholds: HealthThresholds | None = None,
    expected_action: Any = None,
    taken_action: Any = None,
    oracle_result: OracleResult | None = None,
    window: str = "1m",
) -> RecoveryGrade:
    """Evaluates incident recovery by verifying post-action Oracle health and action correctness."""
    reasons: list[str] = []

    # 1. Evaluate Oracle health
    if oracle_result is not None:
        diag_oracle = oracle_result
    elif oracle is not None and thresholds is not None:
        diag_oracle = await oracle.oracle_health(thresholds, window=window)
    else:
        # Fallback healthy if no oracle provided in offline test fixture
        diag_oracle = OracleResult(
            healthy=True,
            error_rate=0.0,
            p95_latency_ms=0.0,
            thresholds={"error_rate": 0.05, "p95_latency_ms": 500.0},
            window=window,
            timestamp="",
        )

    is_healthy = diag_oracle.healthy
    if not is_healthy:
        reasons.append(
            f"Service remains unhealthy after recovery: "
            f"error_rate={diag_oracle.error_rate * 100:.2f}%, "
            f"p95_latency={diag_oracle.p95_latency_ms:.1f}ms"
        )
    else:
        reasons.append("Post-action telemetry confirms service metrics within SLA thresholds.")

    # 2. Evaluate action appropriateness
    exp_act, exp_target = _extract_action(expected_action)
    taken_act, taken_target = _extract_action(taken_action)

    no_action_correct = False
    correctly_escalated = False
    action_match = False

    # Case A: False alarm or benign scenario where no action is required
    if exp_act in ("no_action", "none", ""):
        if taken_act in ("no_action", "none", "", None):
            no_action_correct = True
            action_match = True
            reasons.append("Correctly refrained from taking unneeded operational action.")
        else:
            reasons.append(
                f"Unnecessary action taken: expected no_action, but executed '{taken_act}'"
            )

    # Case B: Correct escalation to human operator
    elif taken_act in ("escalate", "escalation", "human_review"):
        if exp_act in ("escalate", "escalation", "human_review", "manual_intervention"):
            correctly_escalated = True
            action_match = True
            reasons.append("Incident appropriately escalated for manual intervention.")
        else:
            correctly_escalated = True
            action_match = False
            reasons.append(
                f"Action escalated to human operator, though autonomous action "
                f"'{exp_act}' was expected."
            )

    # Case C: Active remediation (e.g. rollback, restart, remediate_dependency)
    else:
        type_matches = taken_act == exp_act
        target_matches = (not exp_target) or (taken_target == exp_target)
        action_match = type_matches and target_matches

        if action_match:
            reasons.append(f"Recovery action '{taken_act}' targeted correctly at '{taken_target}'.")
        else:
            reasons.append(
                f"Action mismatch: expected '{exp_act}' on '{exp_target}', "
                f"got '{taken_act}' on '{taken_target}'"
            )

    recovered = is_healthy and (action_match or no_action_correct or correctly_escalated)

    return RecoveryGrade(
        recovered=recovered,
        healthy=is_healthy,
        action_match=action_match,
        no_action_correct=no_action_correct,
        correctly_escalated=correctly_escalated,
        reasons=reasons,
    )

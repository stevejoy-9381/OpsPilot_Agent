"""Table-driven unit tests for shared/actions.py.

Tests:
1. RecommendedAction Pydantic model validation (frozen, target required for rollback/restart,
   target None for no_action/escalate, known lab services allowlist, notes length).
2. expected_action_from_truth mapping from various ground truth schemas (S1-S5).
3. grade_action table-driven test matrix covering:
   - exact match across all 4 action types
   - wrong service (wrong_target=True, unnecessary=True, missing=True)
   - action on a false alarm (unnecessary=True, missing=False)
   - escalate when an action was expected (missing=True, unnecessary=False)
   - no_action when an action was expected (missing=True, unnecessary=False)
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from shared.actions import (
    ActionGrade,
    ActionType,
    RecommendedAction,
    expected_action_from_truth,
    grade_action,
)

# -----------------------------------------------------------------------------
# 1. Model Validation & Immutability Tests
# -----------------------------------------------------------------------------

def test_recommended_action_valid_rollback() -> None:
    action = RecommendedAction(
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
        rationale_ids=["ev_123"],
        notes="Roll back orders due to pool exhaustion",
    )
    assert action.action == ActionType.ROLLBACK_SERVICE
    assert action.service == "orders"
    assert action.rationale_ids == ["ev_123"]


def test_recommended_action_valid_no_action() -> None:
    action = RecommendedAction(
        action=ActionType.NO_ACTION,
        service=None,
        notes="Transient spike self-recovered",
    )
    assert action.action == ActionType.NO_ACTION
    assert action.service is None


def test_recommended_action_valid_escalate() -> None:
    action = RecommendedAction(
        action=ActionType.ESCALATE,
        service=None,
        notes="Need human on-call assistance",
    )
    assert action.action == ActionType.ESCALATE
    assert action.service is None


def test_recommended_action_frozen_immutability() -> None:
    action = RecommendedAction(
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
    )
    with pytest.raises(ValidationError, match="Instance is frozen"):
        field_to_mutate = "service"
        setattr(action, field_to_mutate, "inventory")


def test_recommended_action_rollback_missing_service_raises() -> None:
    with pytest.raises(ValidationError, match="requires a target service"):
        RecommendedAction(
            action=ActionType.ROLLBACK_SERVICE,
            service=None,
        )


def test_recommended_action_restart_missing_service_raises() -> None:
    with pytest.raises(ValidationError, match="requires a target service"):
        RecommendedAction(
            action=ActionType.RESTART_SERVICE,
            service=None,
        )


def test_recommended_action_unknown_service_raises() -> None:
    with pytest.raises(ValidationError, match="not in known lab services"):
        RecommendedAction(
            action=ActionType.ROLLBACK_SERVICE,
            service="unauthorized_payment_gateway",
        )


def test_recommended_action_no_action_with_service_raises() -> None:
    with pytest.raises(ValidationError, match="must have service=None"):
        RecommendedAction(
            action=ActionType.NO_ACTION,
            service="orders",
        )


def test_recommended_action_escalate_with_service_raises() -> None:
    with pytest.raises(ValidationError, match="must have service=None"):
        RecommendedAction(
            action=ActionType.ESCALATE,
            service="gateway",
        )


def test_recommended_action_notes_max_length_raises() -> None:
    with pytest.raises(ValidationError):
        RecommendedAction(
            action=ActionType.NO_ACTION,
            service=None,
            notes="x" * 301,
        )


# -----------------------------------------------------------------------------
# 2. expected_action_from_truth Mapping Tests
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_truth, expected_type, expected_service",
    [
        # S1: Orders rollback
        ({"action": "rollback", "service": "orders"}, ActionType.ROLLBACK_SERVICE, "orders"),
        (
            {"action": "rollback_service", "service": "orders"},
            ActionType.ROLLBACK_SERVICE,
            "orders",
        ),
        # S2: Inventory rollback
        ({"action": "rollback", "service": "inventory"}, ActionType.ROLLBACK_SERVICE, "inventory"),
        # S3 / S4: Redis remediate_dependency
        (
            {"action": "remediate_dependency", "target": "redis"},
            ActionType.RESTART_SERVICE,
            "redis",
        ),
        ({"action": "restart", "service": "redis"}, ActionType.RESTART_SERVICE, "redis"),
        ({"action": "restart_service", "target": "lab-redis"}, ActionType.RESTART_SERVICE, "redis"),
        # S5: False alarm no_action
        ({"action": "no_action"}, ActionType.NO_ACTION, None),
        ({"action": "none"}, ActionType.NO_ACTION, None),
        # Escalation
        ({"action": "escalate"}, ActionType.ESCALATE, None),
        ({"action": "manual_intervention"}, ActionType.ESCALATE, None),
        # Pass-through of already constructed RecommendedAction
        (
            RecommendedAction(action=ActionType.ROLLBACK_SERVICE, service="orders"),
            ActionType.ROLLBACK_SERVICE,
            "orders",
        ),
    ],
)
def test_expected_action_from_truth(
    raw_truth: Any, expected_type: ActionType, expected_service: str | None
) -> None:
    res = expected_action_from_truth(raw_truth)
    assert res.action == expected_type
    assert res.service == expected_service


# -----------------------------------------------------------------------------
# 3. Table-Driven grade_action Tests
# -----------------------------------------------------------------------------

GRADE_ACTION_TEST_CASES = [
    # (test_id, expected, actual, exp_correct, exp_unnecessary, exp_wrong_target, exp_missing)
    #
    # Case A: Exact matches (correct=True, others=False)
    (
        "exact_match_rollback_orders",
        {"action": "rollback", "service": "orders"},
        {"action": "rollback_service", "service": "orders"},
        True,
        False,
        False,
        False,
    ),
    (
        "exact_match_restart_redis",
        {"action": "remediate_dependency", "target": "redis"},
        {"action": "restart_service", "service": "redis"},
        True,
        False,
        False,
        False,
    ),
    (
        "exact_match_no_action_s5",
        {"action": "no_action"},
        {"action": "no_action"},
        True,
        False,
        False,
        False,
    ),
    (
        "exact_match_escalate",
        {"action": "escalate"},
        {"action": "escalate"},
        True,
        False,
        False,
        False,
    ),
    #
    # Case B: Wrong service (wrong_target=True, unnecessary=True, missing=True, correct=False)
    (
        "wrong_service_rollback_orders_instead_of_inventory",
        {"action": "rollback", "service": "inventory"},
        {"action": "rollback_service", "service": "orders"},
        False,
        True,
        True,
        True,
    ),
    (
        "wrong_service_restart_orders_instead_of_redis",
        {"action": "restart_service", "service": "redis"},
        {"action": "restart_service", "service": "orders"},
        False,
        True,
        True,
        True,
    ),
    #
    # Case C: Action on a false alarm (unnecessary=True, missing=False, correct=False)
    (
        "rollback_on_false_alarm",
        {"action": "no_action"},
        {"action": "rollback_service", "service": "orders"},
        False,
        True,
        False,
        False,
    ),
    (
        "restart_on_false_alarm",
        {"action": "no_action"},
        {"action": "restart_service", "service": "inventory"},
        False,
        True,
        False,
        False,
    ),
    #
    # Case D: Escalate when an action was expected (missing=True, unnecessary=False)
    (
        "escalate_when_rollback_expected",
        {"action": "rollback", "service": "orders"},
        {"action": "escalate"},
        False,
        False,
        False,
        True,
    ),
    (
        "escalate_when_restart_expected",
        {"action": "restart_service", "service": "redis"},
        {"action": "escalate"},
        False,
        False,
        False,
        True,
    ),
    #
    # Case E: no_action when an action was expected (missing=True, unnecessary=False)
    (
        "no_action_when_rollback_expected",
        {"action": "rollback", "service": "orders"},
        {"action": "no_action"},
        False,
        False,
        False,
        True,
    ),
    (
        "no_action_when_restart_expected",
        {"action": "restart_service", "service": "redis"},
        {"action": "no_action"},
        False,
        False,
        False,
        True,
    ),
    #
    # Case F: Different active action on same or different service
    (
        "rollback_instead_of_restart_on_redis",
        {"action": "restart_service", "service": "redis"},
        {"action": "rollback_service", "service": "orders"},
        False,
        True,
        True,
        True,
    ),
]


@pytest.mark.parametrize(
    "test_id, expected, actual, exp_correct, exp_unnecessary, exp_wrong_target, exp_missing",
    GRADE_ACTION_TEST_CASES,
)
def test_grade_action_table(
    test_id: str,
    expected: Any,
    actual: Any,
    exp_correct: bool,
    exp_unnecessary: bool,
    exp_wrong_target: bool,
    exp_missing: bool,
) -> None:
    grade: ActionGrade = grade_action(expected, actual)

    assert grade.correct is exp_correct, f"[{test_id}] correct: {grade.correct} != {exp_correct}"
    assert (
        grade.unnecessary is exp_unnecessary
    ), f"[{test_id}] unnecessary: {grade.unnecessary} != {exp_unnecessary}"
    assert (
        grade.wrong_target is exp_wrong_target
    ), f"[{test_id}] wrong_target: {grade.wrong_target} != {exp_wrong_target}"
    assert grade.missing is exp_missing, f"[{test_id}] missing: {grade.missing} != {exp_missing}"

    # Verify dict indexing interface
    assert grade["correct"] is exp_correct
    assert grade["unnecessary"] is exp_unnecessary
    assert grade["wrong_target"] is exp_wrong_target
    assert grade["missing"] is exp_missing
    assert len(grade.reasons) > 0

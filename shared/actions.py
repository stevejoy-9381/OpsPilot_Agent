"""Typed action vocabulary and action grading for OpsPilot.

Defines:
- ActionType enum
- RecommendedAction frozen Pydantic model
- expected_action_from_truth mapping
- grade_action deterministic grading function conforming to docs/baseline_design.md section 6
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

KNOWN_SERVICES: set[str] = {
    "gateway",
    "orders",
    "inventory",
    "redis",
    "postgres",
    "lab-redis",
    "lab-postgres",
    "payment",
    "notification",
}


class ActionType(str, Enum):
    """Canonical action vocabulary shared across baselines, policy validator, and broker."""

    ROLLBACK_SERVICE = "rollback_service"
    RESTART_SERVICE = "restart_service"
    NO_ACTION = "no_action"
    ESCALATE = "escalate"


class RecommendedAction(BaseModel):
    """Immutable, typed recovery or escalation action recommended by an agent or baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ActionType = Field(description="Action type from canonical enum")
    service: str | None = Field(
        default=None,
        description="Target microservice name (required for rollback/restart; None otherwise)",
    )
    rationale_ids: list[str] = Field(
        default_factory=list,
        description="Supporting evidence identifiers",
    )
    notes: str = Field(
        default="",
        max_length=300,
        description="Concise plain text operational rationale (max 300 chars)",
    )

    @model_validator(mode="after")
    def validate_action_target(self) -> RecommendedAction:
        if self.action in (ActionType.ROLLBACK_SERVICE, ActionType.RESTART_SERVICE):
            if self.service is None:
                raise ValueError(f"Action '{self.action.value}' requires a target service")
            norm_svc = self.service.strip().lower()
            if norm_svc not in KNOWN_SERVICES:
                known_list = sorted(KNOWN_SERVICES)
                raise ValueError(
                    f"Service '{self.service}' is not in known lab services: {known_list}"
                )
        elif self.action in (ActionType.NO_ACTION, ActionType.ESCALATE):
            if self.service is not None:
                raise ValueError(
                    f"Action '{self.action.value}' must have service=None, got '{self.service}'"
                )
        return self


def expected_action_from_truth(scenario_truth: Any) -> RecommendedAction:
    """Extracts and normalizes ground truth into a canonical RecommendedAction."""
    if isinstance(scenario_truth, RecommendedAction):
        return scenario_truth

    raw_action: Any = None
    if hasattr(scenario_truth, "ground_truth"):
        gt = scenario_truth.ground_truth
        raw_action = getattr(gt, "expected_action", None)
    elif hasattr(scenario_truth, "expected_action"):
        raw_action = scenario_truth.expected_action
    elif isinstance(scenario_truth, dict):
        if "expected_action" in scenario_truth:
            raw_action = scenario_truth["expected_action"]
        elif "ground_truth" in scenario_truth:
            gt_dict = scenario_truth["ground_truth"]
            if isinstance(gt_dict, dict):
                raw_action = gt_dict.get("expected_action")
            else:
                raw_action = getattr(gt_dict, "expected_action", None)
        else:
            raw_action = scenario_truth
    else:
        raw_action = scenario_truth

    if isinstance(raw_action, RecommendedAction):
        return raw_action

    act_str = ""
    target: str | None = None
    notes = ""

    if isinstance(raw_action, dict):
        act_str = str(raw_action.get("action", "")).strip().lower()
        target = raw_action.get("service") or raw_action.get("target")
        notes = str(raw_action.get("notes", ""))
    elif isinstance(raw_action, str):
        act_str = raw_action.strip().lower()
    elif hasattr(raw_action, "action"):
        act_str = str(getattr(raw_action, "action", "")).strip().lower()
        target = getattr(raw_action, "service", None) or getattr(raw_action, "target", None)
        notes = str(getattr(raw_action, "notes", ""))

    if target:
        target = str(target).strip().lower()
        if target == "lab-redis":
            target = "redis"
        elif target == "lab-postgres":
            target = "postgres"

    # Map action name
    if act_str in ("rollback", "rollback_service"):
        if not target:
            raise ValueError("Rollback action in truth requires a target service")
        return RecommendedAction(
            action=ActionType.ROLLBACK_SERVICE,
            service=target,
            notes=notes or f"Rollback {target}",
        )
    if act_str in ("restart", "restart_service", "remediate_dependency", "restart_dependency"):
        if not target:
            raise ValueError("Restart action in truth requires a target service")
        return RecommendedAction(
            action=ActionType.RESTART_SERVICE,
            service=target,
            notes=notes or f"Restart {target}",
        )
    if act_str in ("no_action", "none", "none_close", ""):
        return RecommendedAction(
            action=ActionType.NO_ACTION,
            service=None,
            notes=notes or "No action required",
        )
    if act_str in ("escalate", "escalation", "human_review", "manual_intervention"):
        return RecommendedAction(
            action=ActionType.ESCALATE,
            service=None,
            notes=notes or "Escalate to human operator",
        )

    raise ValueError(f"Unknown raw action string in ground truth: '{act_str}'")


class ActionGrade(BaseModel):
    """Deterministic evaluation outcome for a recommended action against ground truth."""

    model_config = ConfigDict(extra="forbid")

    correct: bool = Field(description="Action matches expected action type and target service")
    unnecessary: bool = Field(
        description="Active rollback/restart recommended when no_action or wrong service expected"
    )
    wrong_target: bool = Field(
        description="Action type matched expected but targeted a different service"
    )
    missing: bool = Field(
        description="Active remediation was expected but no_action or escalate was recommended"
    )
    reasons: list[str] = Field(default_factory=list, description="Audit explanations")

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def grade_action(expected: Any, actual: Any) -> ActionGrade:
    """Grades actual recommended action against expected ground truth action.

    Rules from docs/baseline_design.md section 6:
    - correct: action matches expected action and target service.
    - unnecessary: rollback or restart recommended when expected is no_action,
      or when directed at an unrelated/different service.
    - wrong_target: action type is correct but directed at wrong service.
    - missing: expected an active remediation, but actual was no_action or escalate,
      or actual took wrong action altogether.
    """
    exp = expected_action_from_truth(expected)
    act = expected_action_from_truth(actual)

    exp_is_active = exp.action in (ActionType.ROLLBACK_SERVICE, ActionType.RESTART_SERVICE)
    act_is_active = act.action in (ActionType.ROLLBACK_SERVICE, ActionType.RESTART_SERVICE)

    reasons: list[str] = []

    # Case 1: Exact match
    if act.action == exp.action and act.service == exp.service:
        reasons.append(f"Action '{act.action.value}' on target '{act.service}' matches expected.")
        return ActionGrade(
            correct=True,
            unnecessary=False,
            wrong_target=False,
            missing=False,
            reasons=reasons,
        )

    # Case 2: Expected is no_action (e.g. false alarm / transient burst)
    if exp.action == ActionType.NO_ACTION:
        if act_is_active:
            reasons.append(
                f"Unnecessary action: expected no_action on false alarm, but executed "
                f"'{act.action.value}' on '{act.service}'."
            )
            return ActionGrade(
                correct=False,
                unnecessary=True,
                wrong_target=False,
                missing=False,
                reasons=reasons,
            )
        if act.action == ActionType.ESCALATE:
            reasons.append(
                "Action escalated to human operator on false alarm; no action was required."
            )
            return ActionGrade(
                correct=False,
                unnecessary=False,
                wrong_target=False,
                missing=False,
                reasons=reasons,
            )

    # Case 3: Expected is an active remediation (rollback_service or restart_service)
    if exp_is_active:
        # Subcase 3a: Actual escalated when an action was expected (missing, not unnecessary)
        if act.action == ActionType.ESCALATE:
            reasons.append(
                f"Missing action: expected '{exp.action.value}' on '{exp.service}', "
                f"but action was escalated to human operator."
            )
            return ActionGrade(
                correct=False,
                unnecessary=False,
                wrong_target=False,
                missing=True,
                reasons=reasons,
            )

        # Subcase 3b: Actual chose no_action when an action was expected (missing, not unnecessary)
        if act.action == ActionType.NO_ACTION:
            reasons.append(
                f"Missing action: expected '{exp.action.value}' on '{exp.service}', "
                f"but no action was taken."
            )
            return ActionGrade(
                correct=False,
                unnecessary=False,
                wrong_target=False,
                missing=True,
                reasons=reasons,
            )

        # Subcase 3c: Actual took active action on wrong service
        if act_is_active:
            if act.action == exp.action and act.service != exp.service:
                reasons.append(
                    f"Wrong target: expected '{exp.action.value}' on '{exp.service}', "
                    f"got '{act.action.value}' on '{act.service}'."
                )
                return ActionGrade(
                    correct=False,
                    unnecessary=True,
                    wrong_target=True,
                    missing=True,
                    reasons=reasons,
                )

            # Different active action and/or different service
            reasons.append(
                f"Action mismatch: expected '{exp.action.value}' on '{exp.service}', "
                f"got '{act.action.value}' on '{act.service}'."
            )
            return ActionGrade(
                correct=False,
                unnecessary=True,
                wrong_target=(act.service != exp.service),
                missing=True,
                reasons=reasons,
            )

    # Case 4: Expected was escalate
    if exp.action == ActionType.ESCALATE:
        if act_is_active:
            reasons.append(
                f"Unnecessary action: expected escalation, but executed "
                f"'{act.action.value}' on '{act.service}'."
            )
            return ActionGrade(
                correct=False,
                unnecessary=True,
                wrong_target=False,
                missing=False,
                reasons=reasons,
            )
        if act.action == ActionType.NO_ACTION:
            reasons.append(
                "Missing action: expected escalation for human review, but no action was taken."
            )
            return ActionGrade(
                correct=False,
                unnecessary=False,
                wrong_target=False,
                missing=True,
                reasons=reasons,
            )

    return ActionGrade(
        correct=False,
        unnecessary=False,
        wrong_target=False,
        missing=False,
        reasons=["Unhandled action comparison case."],
    )

"""Deterministic reconciliation for uncertain / restarted broker executions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.api.models.tables import ActionExecutionModel, ActionPlanModel
from broker.target_client import TargetClient
from shared.targets import TargetState, TargetStateError


@dataclass(frozen=True)
class Reconciled:
    """Deterministic classification when target state proves applied or not_applied."""

    outcome: str  # "applied" | "not_applied"
    target_state_before: dict[str, Any] | None = None
    target_state_after: dict[str, Any] | None = None
    target_response: dict[str, Any] | None = None
    detail: str = ""


@dataclass(frozen=True)
class StillUnknown:
    """Classification when target is unreachable or state is contradictory/drifted."""

    reason: str
    detail: str


ReconcileResult = Reconciled | StillUnknown


def reconcile(
    execution: ActionExecutionModel,
    plan: ActionPlanModel,
    target_id: str,
    target_client: TargetClient | None,
) -> ReconcileResult:
    """Queries get_action_status(action_key) AND live target state, then classifies.

    - action_key recorded as applied -> Reconciled("applied")
    - action_key absent AND current_version == plan.expected_version -> Reconciled("not_applied")
    - anything else (key absent + version changed, target unreachable, unknown) -> StillUnknown
    """
    if target_client is None:
        return StillUnknown(
            reason="target_client_unavailable",
            detail="TargetClient is not configured",
        )

    try:
        lookup = target_client.get_action_status(target_id, execution.action_key)
        live_state = target_client.get_state(target_id)
    except Exception as exc:
        return StillUnknown(
            reason="reconcile_exception",
            detail=f"Target query raised {type(exc).__name__}: {exc}",
        )

    if lookup.status == "applied":
        before_dict = execution.target_state_before or (
            {"current_version": lookup.from_version, "target_id": target_id}
            if lookup.from_version is not None
            else None
        )
        after_dict = (
            live_state.model_dump(mode="json")
            if isinstance(live_state, TargetState)
            else (
                {"current_version": lookup.to_version, "target_id": target_id}
                if lookup.to_version is not None
                else None
            )
        )
        return Reconciled(
            outcome="applied",
            target_state_before=before_dict,
            target_state_after=after_dict,
            target_response={
                "outcome": "applied",
                "service": lookup.service,
                "action": lookup.action,
                "from_version": lookup.from_version,
                "to_version": lookup.to_version,
                "applied_at": lookup.applied_at,
                "reconciled": True,
            },
            detail="Target applied_actions confirmed action_key was applied",
        )

    if isinstance(live_state, TargetStateError):
        return StillUnknown(
            reason="target_unreachable",
            detail=f"Target state unavailable during reconcile: {live_state.message}",
        )

    if lookup.status == "not_applied":
        if live_state.current_version == plan.expected_version:
            return Reconciled(
                outcome="not_applied",
                target_state_before=live_state.model_dump(mode="json"),
                detail="action_key absent and target version unchanged",
            )
        return StillUnknown(
            reason="version_changed_without_action_key",
            detail=(
                f"action_key absent but live version '{live_state.current_version}' != "
                f"expected_version '{plan.expected_version}'"
            ),
        )

    return StillUnknown(
        reason="action_status_unknown",
        detail=f"Target action lookup returned status '{lookup.status}': {lookup.detail}",
    )

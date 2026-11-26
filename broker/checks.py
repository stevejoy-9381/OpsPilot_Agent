"""Deterministic pre-dispatch checks B1-B10 for the Execution Broker.

Target state is read FRESH at pre-check time via shared.targets.TargetStateReader
(implemented for live lab targets via read-only GET /admin/state over lab-admin).
Any exception, missing row, unavailable reader, or missing registry fails closed
with Refused(code="policy_unavailable").
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ApprovalModel,
    RunModel,
    UserModel,
)
from shared.actions import ActionType
from shared.plans import compute_action_key, plan_hash
from shared.targets import (
    TargetRegistry,
    TargetSpec,
    TargetState,
    TargetStateError,
    TargetStateReader,
    load_target_registry,
)


@dataclass(frozen=True)
class Refused:
    """Typed refusal returned by pre-dispatch checks B1-B10."""

    code: str
    detail: str
    plan_id: UUID | None = None
    run_id: UUID | None = None
    tenant_id: UUID | None = None


@dataclass(frozen=True)
class Allowed:
    """Typed approval returned when all B1-B10 checks pass."""

    execution: ActionExecutionModel
    plan: ActionPlanModel
    run: RunModel
    target_spec: TargetSpec
    target_state: TargetState


PrecheckResult = Allowed | Refused


def _safe_load_registry(registry_or_path: TargetRegistry | Path | None) -> TargetRegistry | None:
    if isinstance(registry_or_path, TargetRegistry):
        return registry_or_path
    try:
        return load_target_registry(registry_or_path)
    except Exception:
        return None


def check_b1(
    session: Session, execution_id: UUID
) -> tuple[ActionExecutionModel, ActionPlanModel] | Refused:
    """B1: Execution row and plan exist for the same tenant."""
    execution = session.scalar(
        select(ActionExecutionModel).where(ActionExecutionModel.id == execution_id)
    )
    if execution is None:
        return Refused(
            code="execution_or_plan_missing",
            detail=f"Execution {execution_id} not found",
        )

    session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(execution.tenant_id)},
    )
    plan = session.scalar(
        select(ActionPlanModel).where(
            ActionPlanModel.id == execution.plan_id,
            ActionPlanModel.tenant_id == execution.tenant_id,
        )
    )
    if plan is None:
        return Refused(
            code="execution_or_plan_missing",
            detail=f"Plan {execution.plan_id} not found for tenant {execution.tenant_id}",
            plan_id=execution.plan_id,
            tenant_id=execution.tenant_id,
        )
    return execution, plan


def check_b2(plan: ActionPlanModel) -> Refused | None:
    """B2: Plan status must be 'approved'."""
    if plan.status != "approved":
        return Refused(
            code="plan_not_approved",
            detail=f"Plan {plan.id} status is '{plan.status}', expected 'approved'",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return None


def check_b3(session: Session, plan: ActionPlanModel) -> ApprovalModel | Refused:
    """B3: Valid human approval row exists by a reviewer/admin (never service principal)."""
    approvals = session.scalars(
        select(ApprovalModel).where(
            ApprovalModel.plan_id == plan.id,
            ApprovalModel.tenant_id == plan.tenant_id,
            ApprovalModel.decision == "approve",
        )
    ).all()
    if not approvals:
        return Refused(
            code="approval_invalid_or_missing",
            detail=f"No approve decision found for plan {plan.id}",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )

    approval = approvals[0]
    if approval.plan_id != plan.id:
        return Refused(
            code="approval_invalid_or_missing",
            detail="Approval plan_id mismatch",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )

    user = session.scalar(
        select(UserModel).where(
            UserModel.id == approval.reviewer_id,
            UserModel.tenant_id == plan.tenant_id,
        )
    )
    if user is None or user.role not in ("reviewer", "admin"):
        role_desc = user.role if user is not None else "missing"
        return Refused(
            code="approval_invalid_or_missing",
            detail=f"Approver role '{role_desc}' is not authorized (must be reviewer or admin)",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return approval


def check_b4(plan: ActionPlanModel, approval: ApprovalModel) -> Refused | None:
    """B4: Recomputed plan_hash matches stored plan_hash and approval.submitted_plan_hash."""
    recomputed = plan_hash(
        run_id=plan.run_id,
        tenant_id=plan.tenant_id,
        action=plan.action,
        service=plan.service or "",
        target_id=plan.target_id,
        expected_version=plan.expected_version or "",
        rollback_to_version=plan.rollback_to_version,
        rationale_evidence_ids=list(plan.rationale_evidence_ids),
        expires_at=plan.expires_at,
    )
    stored_hash = plan.plan_hash.strip().lower()
    submitted_hash = approval.submitted_plan_hash.strip().lower()
    if not (
        hmac.compare_digest(recomputed, stored_hash)
        and hmac.compare_digest(stored_hash, submitted_hash)
    ):
        return Refused(
            code="plan_hash_integrity_failed",
            detail="Plan hash recomputation or approval parity check failed",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return None


def check_b5(plan: ActionPlanModel, now_from_db: datetime) -> Refused | None:
    """B5: Plan must not be expired against DB clock_timestamp()."""
    if plan.expires_at <= now_from_db:
        return Refused(
            code="execution_expired",
            detail=f"Plan {plan.id} expired at {plan.expires_at.isoformat()}",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return None


def check_b6(execution: ActionExecutionModel, plan: ActionPlanModel) -> Refused | None:
    """B6: action_key equals shared.plans.compute_action_key(plan.id, plan.plan_hash)."""
    expected_key = compute_action_key(plan.id, plan.plan_hash)
    if not hmac.compare_digest(execution.action_key.strip().lower(), expected_key):
        return Refused(
            code="action_key_mismatch",
            detail="Execution action_key does not match computed action_key",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return None


def check_b7(
    plan: ActionPlanModel, target_registry: TargetRegistry | None
) -> TargetSpec | Refused:
    """B7: Target is in the registry and action is allowed on it."""
    if target_registry is None:
        return Refused(
            code="policy_unavailable",
            detail="Target registry is missing or unreadable",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    spec = target_registry.get_by_target_id(plan.target_id) if plan.target_id else None
    if spec is None or spec.service != (plan.service or "").strip().lower():
        return Refused(
            code="target_or_action_not_allowed",
            detail=f"Target '{plan.target_id}' / service '{plan.service}' is not allowlisted",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    try:
        action_enum = ActionType(plan.action)
    except ValueError:
        return Refused(
            code="target_or_action_not_allowed",
            detail=f"Action '{plan.action}' is not a valid ActionType",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    if action_enum not in spec.allowed_actions:
        return Refused(
            code="target_or_action_not_allowed",
            detail=f"Action '{plan.action}' is not allowed on target '{plan.target_id}'",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return spec


def check_b8(
    plan: ActionPlanModel,
    now_from_db: datetime,
    target_reader: TargetStateReader | None,
) -> TargetState | Refused:
    """B8: Live target state read FRESH now; current_version == plan.expected_version."""
    if target_reader is None:
        return Refused(
            code="policy_unavailable",
            detail="TargetStateReader is unavailable",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    if not plan.target_id:
        return Refused(
            code="target_or_action_not_allowed",
            detail="Plan target_id is missing",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    state_or_err = target_reader.current_state(plan.target_id, now_from_db)
    if isinstance(state_or_err, TargetStateError):
        return Refused(
            code="policy_unavailable",
            detail=f"Target state unavailable ({state_or_err.code.value}): {state_or_err.message}",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    if state_or_err.current_version != plan.expected_version:
        return Refused(
            code="stale_target",
            detail=(
                f"Live target version '{state_or_err.current_version}' != "
                f"expected_version '{plan.expected_version}'"
            ),
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return state_or_err


def check_b9(
    session: Session, execution: ActionExecutionModel, plan: ActionPlanModel
) -> Refused | None:
    """B9: Per-target lock — no other execution for this target is claimed/dispatched/uncertain."""
    conflict_id = session.scalar(
        select(ActionExecutionModel.id)
        .join(ActionPlanModel, ActionExecutionModel.plan_id == ActionPlanModel.id)
        .where(
            ActionPlanModel.target_id == plan.target_id,
            ActionExecutionModel.id != execution.id,
            ActionExecutionModel.status.in_(("claimed", "dispatched", "uncertain")),
        )
        .limit(1)
    )
    if conflict_id is not None:
        return Refused(
            code="target_locked",
            detail=f"Target '{plan.target_id}' already has in-flight execution {conflict_id}",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return None


def check_b10(session: Session, plan: ActionPlanModel) -> RunModel | Refused:
    """B10: Owning run is in 'approved' status."""
    run = session.scalar(
        select(RunModel).where(
            RunModel.id == plan.run_id,
            RunModel.tenant_id == plan.tenant_id,
        )
    )
    if run is None or run.status != "approved":
        status_desc = run.status if run is not None else "missing"
        return Refused(
            code="run_not_approved_or_stale",
            detail=f"Run {plan.run_id} status is '{status_desc}', expected 'approved'",
            plan_id=plan.id,
            run_id=plan.run_id,
            tenant_id=plan.tenant_id,
        )
    return run


def run_prechecks(
    session: Session,
    execution_id: UUID,
    now_from_db: datetime,
    target_reader: TargetStateReader | None,
    target_registry: TargetRegistry | Path | None = None,
) -> PrecheckResult:
    """Runs B1-B10 in strict order, failing closed with Refused('policy_unavailable') on error."""
    try:
        reg = _safe_load_registry(target_registry)

        b1_res = check_b1(session, execution_id)
        if isinstance(b1_res, Refused):
            return b1_res
        execution, plan = b1_res

        if (b2_err := check_b2(plan)) is not None:
            return b2_err

        b3_res = check_b3(session, plan)
        if isinstance(b3_res, Refused):
            return b3_res
        approval = b3_res

        if (b4_err := check_b4(plan, approval)) is not None:
            return b4_err

        if (b5_err := check_b5(plan, now_from_db)) is not None:
            return b5_err

        if (b6_err := check_b6(execution, plan)) is not None:
            return b6_err

        b7_res = check_b7(plan, reg)
        if isinstance(b7_res, Refused):
            return b7_res
        spec = b7_res

        b8_res = check_b8(plan, now_from_db, target_reader)
        if isinstance(b8_res, Refused):
            return b8_res
        target_state = b8_res

        if (b9_err := check_b9(session, execution, plan)) is not None:
            return b9_err

        b10_res = check_b10(session, plan)
        if isinstance(b10_res, Refused):
            return b10_res
        run = b10_res

        return Allowed(
            execution=execution,
            plan=plan,
            run=run,
            target_spec=spec,
            target_state=target_state,
        )
    except Exception as exc:
        return Refused(
            code="policy_unavailable",
            detail=f"Pre-dispatch check failed closed: {type(exc).__name__}: {exc}",
        )

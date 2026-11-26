"""Transactional approval service for POST /plans/{id}/decisions (Feature 8).

Implements the ordered approval transaction defined in docs/approval_design.md:
- Session-only UserPrincipal & role/SoD authorization (enforced in service)
- Pre-transaction TargetStateReader read & TARGET_STATE_MAX_AGE_SECONDS freshness check (fail-closed)
- Single atomic PostgreSQL transaction with SET LOCAL app.tenant_id, FOR UPDATE locking,
  constant-time plan_hash parity + stored-field recompute integrity check,
  DB clock (now()/clock_timestamp()) expiry check, optimistic run_version check,
  approvals insert, action_executions(queued) insert with deterministic action_key,
  RunService transition, approval_recorded event, and Idempotency-Key persistence.

Design-doc open-question proposals implemented (flagged per docs/approval_design.md):
- Reject path: plan -> rejected, run -> escalated (reason: reviewer_rejected).
- Stale-target path: plan -> superseded, run -> escalated (reason: target_changed), raises StaleTargetError (409).
- Expiry path: plan -> expired, run -> escalated (reason: approval_expired), raises PlanExpiredError (410).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.auth.dependencies import Principal
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import (
    AlreadyDecidedError,
    ConcurrentModificationError,
    DecisionConflictError,
    ForbiddenError,
    IdempotencyConflictError,
    NotFoundError,
    PlanExpiredError,
    PlanHashMismatchError,
    PlanNotProposedError,
    PlanSupersededError,
    RunNotInReviewError,
    SeparationOfDutiesError,
    StaleTargetError,
    TargetStateUnavailableError,
)
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ApprovalIdempotencyModel,
    ApprovalModel,
    RunModel,
)
from apps.api.routers.schemas import PlanDecisionResponse
from apps.api.services.run_service import RunService
from apps.api.settings import get_settings
from shared.plans import PlanStatus, compute_action_key, plan_hash
from shared.targets import TargetStateError, TargetStateReader

logger = logging.getLogger("opspilot.api.approval")

DecisionResult = PlanDecisionResponse

DEFAULT_TARGET_STATE_MAX_AGE_SECONDS: int = int(
    os.environ.get("TARGET_STATE_MAX_AGE_SECONDS", "30")
)


class ApprovalMetrics:
    """In-process counters for Feature 8 demo and evaluation reporting."""

    def __init__(self) -> None:
        self.duplicate_approval_attempts_blocked: int = 0
        self.idempotent_replays: int = 0
        self.refused_by_reason: Counter[str] = Counter()

    def record_refusal(self, reason_code: str, *, is_duplicate: bool = False) -> None:
        self.refused_by_reason[reason_code] += 1
        if is_duplicate:
            self.duplicate_approval_attempts_blocked += 1

    def record_replay(self) -> None:
        self.idempotent_replays += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "duplicate_approval_attempts_blocked": self.duplicate_approval_attempts_blocked,
            "idempotent_replays": self.idempotent_replays,
            "approvals_refused_by_reason": dict(self.refused_by_reason),
        }


approval_metrics = ApprovalMetrics()


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _set_tenant_guc(session: Session, tenant_id: UUID) -> None:
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )


def _db_now(session: Session) -> datetime:
    bind = session.get_bind()
    expr = func.now() if (bind is not None and bind.dialect.name == "postgresql") else func.clock_timestamp()
    raw_dt: Any = session.scalar(select(expr))
    if isinstance(raw_dt, str):
        raw_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
    if not isinstance(raw_dt, datetime):
        raise RuntimeError("Failed to read database clock")
    return _ensure_utc(raw_dt)


def check_clock_skew(
    session: Session, warn_threshold_seconds: float = 5.0
) -> float:
    """Logs DB-vs-app clock delta at startup and warns if skew exceeds threshold."""
    db_utc = _db_now(session)
    app_utc = datetime.now(timezone.utc)
    delta_seconds = (db_utc - app_utc).total_seconds()
    if abs(delta_seconds) > warn_threshold_seconds:
        logger.warning(
            "clock_skew_detected db_utc=%s app_utc=%s delta_seconds=%.3f threshold=%.1f",
            db_utc.isoformat(),
            app_utc.isoformat(),
            delta_seconds,
            warn_threshold_seconds,
        )
    else:
        logger.info(
            "clock_sanity_ok db_utc=%s app_utc=%s delta_seconds=%.3f",
            db_utc.isoformat(),
            app_utc.isoformat(),
            delta_seconds,
        )
    return delta_seconds


def _notify_plan_decided(session: Session, run_id: UUID) -> None:
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        try:
            session.execute(
                text("SELECT pg_notify('opspilot_plan_decided', :rid)"),
                {"rid": str(run_id)},
            )
            session.commit()
        except Exception as exc:
            logger.debug("pg_notify opspilot_plan_decided failed (non-fatal): %s", exc)


class ApprovalService:
    """Sole code path permitted to write app.approvals and app.action_executions."""

    def __init__(
        self,
        session: Session,
        *,
        target_reader: TargetStateReader | None = None,
        target_state_max_age_seconds: int | None = None,
        enforce_separation_of_duties: bool | None = None,
    ) -> None:
        self.session = session
        self.target_reader = target_reader
        if target_state_max_age_seconds is not None:
            self.target_state_max_age_seconds = target_state_max_age_seconds
        else:
            try:
                self.target_state_max_age_seconds = get_settings().target_state_max_age_seconds
            except Exception:
                self.target_state_max_age_seconds = DEFAULT_TARGET_STATE_MAX_AGE_SECONDS

        if enforce_separation_of_duties is not None:
            self.enforce_sod = enforce_separation_of_duties
        else:
            try:
                self.enforce_sod = get_settings().enforce_separation_of_duties
            except Exception:
                self.enforce_sod = False

        self.run_service = RunService(session)

    def decide_plan(
        self,
        *,
        principal: Principal,
        plan_id: UUID,
        decision: Literal["approve", "reject"],
        submitted_plan_hash: str,
        reason: str,
        if_match_version: int,
        idempotency_key: str,
    ) -> DecisionResult:
        """Backwards-compatible alias for ApprovalService.decide()."""
        return self.decide(
            principal=principal,
            plan_id=plan_id,
            decision=decision,
            submitted_plan_hash=submitted_plan_hash,
            reason=reason,
            run_version=if_match_version,
            idempotency_key=idempotency_key,
        )

    def decide(
        self,
        principal: Principal,
        plan_id: UUID,
        decision: Literal["approve", "reject"],
        submitted_plan_hash: str,
        reason: str,
        run_version: int,
        idempotency_key: str,
    ) -> DecisionResult:
        """Atomically approves or rejects a proposed ActionPlan."""
        # 1. Service-layer principal & role enforcement (worker & viewer can never reach decide)
        if principal.is_service or principal.user_id is None:
            approval_metrics.record_refusal("forbidden")
            raise ForbiddenError("Service principals cannot approve or reject plans")
        if not principal.has_role("reviewer"):
            approval_metrics.record_refusal("forbidden")
            raise ForbiddenError("Reviewer or admin role is required")
        if decision not in ("approve", "reject"):
            approval_metrics.record_refusal("invalid_request")
            raise ValueError(f"Unsupported decision: {decision!r}")

        reviewer_id = principal.user_id
        tenant_id = principal.tenant_id
        norm_hash = submitted_plan_hash.strip().lower()
        clean_reason = reason.strip()
        ctx = TenantContext(tenant_id=tenant_id, user_id=reviewer_id, role=principal.role)

        # 2. Pre-transaction live target state read (BEFORE opening the locking transaction)
        live_version: str | None = None
        if decision == "approve":
            if self.target_reader is None:
                approval_metrics.record_refusal("target_state_unavailable")
                raise TargetStateUnavailableError(
                    "TargetStateReader is unavailable; failing closed on approve"
                )
            _set_tenant_guc(self.session, tenant_id)
            pre_plan = self.session.scalar(
                select(ActionPlanModel).where(
                    ActionPlanModel.id == plan_id,
                    ActionPlanModel.tenant_id == tenant_id,
                )
            )
            # Read DB clock for target_state freshness check (never use app clock)
            read_as_of = _db_now(self.session)
            # End the read-only pre-check transaction cleanly before calling external reader
            self.session.rollback()

            if pre_plan is not None and pre_plan.status == PlanStatus.PROPOSED.value and pre_plan.target_id:
                try:
                    target_state = self.target_reader.current_state(pre_plan.target_id, read_as_of)
                except Exception as exc:
                    approval_metrics.record_refusal("target_state_unavailable")
                    raise TargetStateUnavailableError(
                        "Live target state is unavailable; wait a moment and retry.",
                        details={"target_id": pre_plan.target_id, "cause": str(exc)},
                    ) from exc

                if isinstance(target_state, TargetStateError):
                    approval_metrics.record_refusal("target_state_unavailable")
                    raise TargetStateUnavailableError(
                        "Live target state is unavailable; wait a moment and retry.",
                        details={"target_id": pre_plan.target_id, "code": target_state.code.value},
                    )

                age_seconds = (read_as_of - _ensure_utc(target_state.read_at)).total_seconds()
                if age_seconds > self.target_state_max_age_seconds:
                    approval_metrics.record_refusal("target_state_unavailable")
                    raise TargetStateUnavailableError(
                        "Live target state is stale; wait a moment and retry.",
                        details={
                            "target_id": pre_plan.target_id,
                            "age_seconds": age_seconds,
                            "max_age_seconds": self.target_state_max_age_seconds,
                        },
                    )
                live_version = target_state.current_version

        # 3. Single atomic transaction for all checks and writes
        try:
            _set_tenant_guc(self.session, tenant_id)

            # 3a. Lock action_plans row FOR UPDATE
            plan = self.session.scalar(
                select(ActionPlanModel)
                .where(
                    ActionPlanModel.id == plan_id,
                    ActionPlanModel.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if plan is None:
                raise NotFoundError(f"Plan {plan_id} not found")

            # 3b. Check Idempotency-Key inside transaction after locking plan
            existing_idem = self.session.scalar(
                select(ApprovalIdempotencyModel).where(
                    ApprovalIdempotencyModel.tenant_id == tenant_id,
                    ApprovalIdempotencyModel.plan_id == plan_id,
                    ApprovalIdempotencyModel.reviewer_id == reviewer_id,
                    ApprovalIdempotencyModel.idempotency_key == idempotency_key,
                )
            )
            if existing_idem is not None:
                stored_reason = str(existing_idem.response_body.get("_request_reason", ""))
                same_body = (
                    existing_idem.decision == decision
                    and hmac.compare_digest(
                        norm_hash, existing_idem.submitted_plan_hash.strip().lower()
                    )
                    and clean_reason == stored_reason
                )
                if same_body:
                    approval_metrics.record_replay()
                    body = dict(existing_idem.response_body)
                    body["idempotent_replay"] = True
                    self.session.rollback()
                    return DecisionResult.model_validate(body)

                approval_metrics.record_refusal("idempotency_conflict", is_duplicate=True)
                raise IdempotencyConflictError(
                    "Idempotency-Key already used with a different request body",
                    details={"plan_id": str(plan_id), "idempotency_key": idempotency_key},
                )

            # 3c. Constant-time hash check against stored plan_hash + recompute check (tamper detection)
            stored_hash = plan.plan_hash.strip().lower()
            if not hmac.compare_digest(norm_hash, stored_hash):
                approval_metrics.record_refusal("plan_hash_mismatch")
                raise PlanHashMismatchError(
                    "Plan details do not match what you reviewed; refresh the review page and try again.",
                    details={"plan_id": str(plan_id)},
                )

            if plan.target_id is not None and plan.service is not None and plan.expected_version is not None:
                raw_evidence = plan.rationale_evidence_ids
                if isinstance(raw_evidence, str):
                    raw_evidence = json.loads(raw_evidence)
                recomputed = plan_hash(
                    run_id=str(plan.run_id),
                    tenant_id=str(plan.tenant_id),
                    action=plan.action,
                    service=plan.service,
                    target_id=plan.target_id,
                    expected_version=plan.expected_version,
                    rollback_to_version=plan.rollback_to_version,
                    rationale_evidence_ids=list(raw_evidence),
                    expires_at=plan.expires_at,
                )
                if not hmac.compare_digest(recomputed.lower(), stored_hash):
                    approval_metrics.record_refusal("plan_hash_mismatch")
                    raise PlanHashMismatchError(
                        "Stored plan failed integrity recomputation check",
                        details={"plan_id": str(plan_id), "tamper_detected": True},
                    )

            # 3d. Plan status & duplicate/conflicting decision checks
            if plan.status == PlanStatus.EXPIRED.value:
                approval_metrics.record_refusal("plan_expired")
                raise PlanExpiredError(
                    "Plan expired; ask for a new investigation.", details={"plan_id": str(plan_id)}
                )

            if plan.status == PlanStatus.SUPERSEDED.value:
                approval_metrics.record_refusal("plan_superseded")
                raise PlanSupersededError(
                    "This plan was replaced by a newer proposal; refresh the page.",
                    details={"plan_id": str(plan_id)},
                )

            if plan.status in (
                PlanStatus.APPROVED.value,
                PlanStatus.REJECTED.value,
                PlanStatus.EXECUTED.value,
                PlanStatus.FAILED.value,
            ):
                prior_approval = self.session.scalar(
                    select(ApprovalModel).where(
                        ApprovalModel.plan_id == plan_id,
                        ApprovalModel.tenant_id == tenant_id,
                    )
                )
                if (
                    prior_approval is not None
                    and prior_approval.reviewer_id == reviewer_id
                    and prior_approval.decision != decision
                ):
                    approval_metrics.record_refusal("decision_conflict", is_duplicate=True)
                    raise DecisionConflictError(
                        f"Reviewer already submitted '{prior_approval.decision}' for plan {plan_id}",
                        details={
                            "plan_id": str(plan_id),
                            "existing_decision": prior_approval.decision,
                        },
                    )
                approval_metrics.record_refusal("already_decided", is_duplicate=True)
                raise AlreadyDecidedError(
                    f"Plan {plan_id} has already been decided (status={plan.status})",
                    details={"plan_id": str(plan_id), "plan_status": plan.status},
                )

            if plan.status != PlanStatus.PROPOSED.value:
                approval_metrics.record_refusal("plan_not_proposed")
                raise PlanNotProposedError(
                    f"Plan {plan_id} is in status '{plan.status}', expected 'proposed'",
                    details={"plan_id": str(plan_id), "plan_status": plan.status},
                )

            # 3e. Lock owning run FOR UPDATE & verify needs_review + optimistic run_version
            run = self.session.scalar(
                select(RunModel)
                .where(
                    RunModel.id == plan.run_id,
                    RunModel.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if run is None:
                raise NotFoundError(f"Owning run {plan.run_id} not found")

            if run.status != "needs_review":
                approval_metrics.record_refusal("run_not_in_review")
                raise RunNotInReviewError(
                    "This run is no longer awaiting review; refresh the page for its current status.",
                    details={"run_id": str(run.id), "run_status": run.status},
                )

            if run.version != run_version:
                approval_metrics.record_refusal("concurrent_modification")
                raise ConcurrentModificationError(
                    entity_id=str(run.id),
                    expected_version=run_version,
                    actual_version=run.version,
                )

            if (
                self.enforce_sod
                and decision == "approve"
                and run.created_by_user_id is not None
                and run.created_by_user_id == reviewer_id
            ):
                approval_metrics.record_refusal("separation_of_duties_violation")
                raise SeparationOfDutiesError(
                    "Run creator cannot approve their own recovery plan under 4-eyes policy",
                    details={"run_id": str(run.id), "reviewer_id": str(reviewer_id)},
                )

            # 3f. Expiry check using database clock
            db_now = _db_now(self.session)
            if db_now > _ensure_utc(plan.expires_at):
                # Proposed in docs/approval_design.md: commit plan->expired, run->escalated
                plan.status = PlanStatus.EXPIRED.value
                self.run_service.transition(
                    ctx=ctx,
                    run_id=run.id,
                    to_status="escalated",
                    actor="system:approval_expiry",
                    expected_version=run_version,
                    payload={"plan_id": str(plan.id), "reason": "approval_expired"},
                )
                self.run_service.record_error(
                    ctx=ctx,
                    run_id=run.id,
                    error={
                        "code": "approval_expired",
                        "message": "Action plan expired before approval",
                        "details": {"plan_id": str(plan.id)},
                    },
                    actor="system:approval_expiry",
                )
                self.run_service.append_event(
                    ctx=ctx,
                    run_id=run.id,
                    event_type="approval_expired",
                    actor="system:approval_expiry",
                    payload={
                        "plan_id": str(plan.id),
                        "expires_at": _ensure_utc(plan.expires_at).isoformat(),
                    },
                )
                self.session.commit()
                _notify_plan_decided(self.session, run.id)
                approval_metrics.record_refusal("plan_expired")
                raise PlanExpiredError(
                    "Plan expired; ask for a new investigation.", details={"plan_id": str(plan.id)}
                )

            # 3g. Stale-target check (approve only)
            if (
                decision == "approve"
                and live_version is not None
                and plan.expected_version is not None
                and live_version != plan.expected_version
            ):
                # Proposed in docs/approval_design.md: commit plan->superseded, run->escalated
                plan.status = PlanStatus.SUPERSEDED.value
                self.run_service.transition(
                    ctx=ctx,
                    run_id=run.id,
                    to_status="escalated",
                    actor="system:stale_target",
                    expected_version=run_version,
                    payload={
                        "plan_id": str(plan.id),
                        "reason": "target_changed",
                        "expected_version": plan.expected_version,
                        "live_version": live_version,
                    },
                )
                self.run_service.record_error(
                    ctx=ctx,
                    run_id=run.id,
                    error={
                        "code": "stale_target",
                        "reason": "target_changed",
                        "message": (
                            f"Target version changed from '{plan.expected_version}' "
                            f"to '{live_version}' before approval"
                        ),
                        "details": {
                            "plan_id": str(plan.id),
                            "expected_version": plan.expected_version,
                            "live_version": live_version,
                        },
                    },
                    actor="system:stale_target",
                )
                self.run_service.append_event(
                    ctx=ctx,
                    run_id=run.id,
                    event_type="plan_superseded",
                    actor="system:stale_target",
                    payload={
                        "plan_id": str(plan.id),
                        "reason": "target_changed",
                        "expected_version": plan.expected_version,
                        "live_version": live_version,
                    },
                )
                self.session.commit()
                _notify_plan_decided(self.session, run.id)
                approval_metrics.record_refusal("stale_target")
                raise StaleTargetError(
                    "Target changed since investigation; start a new run to re-investigate.",
                    details={
                        "plan_id": str(plan.id),
                        "expected_version": plan.expected_version,
                        "live_version": live_version,
                    },
                )

            # 3h. Insert approval row
            approval_id = uuid4()
            approval_row = ApprovalModel(
                id=approval_id,
                plan_id=plan.id,
                tenant_id=tenant_id,
                reviewer_id=reviewer_id,
                decision=decision,
                submitted_plan_hash=norm_hash,
                reason=clean_reason,
                decided_at=db_now,
            )
            self.session.add(approval_row)
            self.session.flush()

            # 3i. Update action_plans.status
            new_plan_status = (
                PlanStatus.APPROVED.value if decision == "approve" else PlanStatus.REJECTED.value
            )
            plan.status = new_plan_status
            self.session.flush()

            # 3j. (Approve only) Insert action_executions(queued) with deterministic action_key
            action_execution_id: UUID | None = None
            action_key: str | None = None
            if decision == "approve":
                action_execution_id = uuid4()
                action_key = compute_action_key(plan.id, plan.plan_hash)
                exec_row = ActionExecutionModel(
                    id=action_execution_id,
                    plan_id=plan.id,
                    tenant_id=tenant_id,
                    action_key=action_key,
                    status="queued",
                    attempts=0,
                )
                self.session.add(exec_row)
                self.session.flush()

            # 3k. Transition run via RunService
            actor = f"reviewer:{reviewer_id}"
            if decision == "approve":
                updated_run, _ = self.run_service.transition(
                    ctx=ctx,
                    run_id=run.id,
                    to_status="approved",
                    actor=actor,
                    expected_version=run_version,
                    payload={
                        "plan_id": str(plan.id),
                        "plan_hash": plan.plan_hash,
                        "action_execution_id": str(action_execution_id),
                    },
                )
            else:
                # Proposed in docs/approval_design.md: reject transitions run -> escalated
                updated_run, _ = self.run_service.transition(
                    ctx=ctx,
                    run_id=run.id,
                    to_status="escalated",
                    actor=actor,
                    expected_version=run_version,
                    payload={
                        "plan_id": str(plan.id),
                        "reason": "reviewer_rejected",
                    },
                )
                self.run_service.record_error(
                    ctx=ctx,
                    run_id=run.id,
                    error={
                        "code": "reviewer_rejected",
                        "message": "Plan rejected by human reviewer",
                        "details": {
                            "plan_id": str(plan.id),
                            "reviewer_id": str(reviewer_id),
                            "reason": clean_reason,
                        },
                    },
                    actor=actor,
                )

            # 3l. Append approval_recorded event & persist idempotency record
            self.run_service.append_event(
                ctx=ctx,
                run_id=run.id,
                event_type="approval_recorded",
                actor=actor,
                payload={
                    "plan_id": str(plan.id),
                    "approval_id": str(approval_id),
                    "decision": decision,
                    "plan_hash": plan.plan_hash,
                    "action_execution_id": str(action_execution_id) if action_execution_id else None,
                    "action_key": action_key,
                },
            )

            resp = DecisionResult(
                plan_id=plan.id,
                run_id=run.id,
                approval_id=approval_id,
                decision=decision,
                plan_status=new_plan_status,
                run_status=updated_run.status,
                run_version=updated_run.version,
                action_execution_id=action_execution_id,
                action_key=action_key,
                decided_at=db_now,
                idempotent_replay=False,
            )

            stored_body = resp.model_dump(mode="json")
            stored_body["_request_reason"] = clean_reason
            idem_row = ApprovalIdempotencyModel(
                tenant_id=tenant_id,
                plan_id=plan.id,
                reviewer_id=reviewer_id,
                idempotency_key=idempotency_key,
                decision=decision,
                submitted_plan_hash=norm_hash,
                response_status=200,
                response_body=stored_body,
            )
            self.session.add(idem_row)
            self.session.commit()
        except (PlanExpiredError, StaleTargetError):
            raise
        except IntegrityError as exc:
            self.session.rollback()
            approval_metrics.record_refusal("already_decided", is_duplicate=True)
            raise AlreadyDecidedError(
                f"Plan {plan_id} already has a conflicting decision or execution",
                details={"plan_id": str(plan_id)},
            ) from exc
        except Exception:
            self.session.rollback()
            raise

        _notify_plan_decided(self.session, run.id)
        return resp

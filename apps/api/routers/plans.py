"""Plan review and decision routes.

Enforces:
1. GET /plans/{id} and GET /plans/{id}/review provide read-only display data (404 cross-tenant).
2. GET /plans/{id}/decision reads back the recorded decision (tenant-scoped, 404 if none).
3. POST /plans/{id}/decisions translates HTTP to ApprovalService.decide() and errors to HTTP,
   enforcing reviewer/admin session auth, CSRF, If-Match, Idempotency-Key, per-reviewer
   Redis rate limiting, body size cap, forbidden identity fields, and redacted audit logging.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.auth.csrf import verify_csrf
from apps.api.auth.dependencies import (
    Principal,
    get_db_session,
    require_role,
)
from apps.api.auth.rate_limiter import RateLimiter
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import (
    AlreadyDecidedError,
    ConcurrentModificationError,
    DecisionConflictError,
    DomainError,
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
    ApprovalModel,
    RunModel,
)
from apps.api.repositories.plan_repository import PlanRepository
from apps.api.routers.schemas import (
    PlanDecisionReadResponse,
    PlanDecisionRequest,
    PlanDecisionResponse,
    PlanResponse,
    PlanReviewResponse,
)
from apps.api.services.approval_service import ApprovalService, DecisionResult, approval_metrics
from shared.targets import TargetStateReader

router = APIRouter(prefix="/plans", tags=["plans"])
audit_logger = logging.getLogger("opspilot.audit.plan_decisions")

plans_rate_limiter = RateLimiter(max_attempts=30, window_seconds=60)
MAX_DECISION_BODY_BYTES = 8192

_FORBIDDEN_IDENTITY_PARAMS = frozenset({"reviewer_id", "tenant_id", "run_id"})
_FORBIDDEN_IDENTITY_HEADERS = frozenset(
    {
        "reviewer_id",
        "reviewer-id",
        "x-reviewer-id",
        "tenant_id",
        "tenant-id",
        "x-tenant-id",
        "run_id",
        "run-id",
        "x-run-id",
    }
)

_DECISION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "description": "Missing or invalid Idempotency-Key",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Idempotency-Key header is required",
                    "error": {
                        "code": "bad_request",
                        "message": "Idempotency-Key header is required",
                        "details": {},
                    },
                }
            }
        },
    },
    401: {
        "description": "Unauthenticated session",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Not authenticated",
                    "error": {"code": "unauthorized", "message": "Not authenticated", "details": {}},
                }
            }
        },
    },
    403: {
        "description": "Forbidden role, service principal, CSRF failure, or Separation of Duties violation",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Reviewer or admin role is required",
                    "error": {
                        "code": "forbidden",
                        "message": "Reviewer or admin role is required",
                        "details": {},
                    },
                }
            }
        },
    },
    404: {
        "description": "Plan not found or belongs to another tenant",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Plan not found",
                    "error": {"code": "not_found", "message": "Plan not found", "details": {}},
                }
            }
        },
    },
    409: {
        "description": "Plan hash mismatch, concurrent modification, stale target, or already decided",
        "content": {
            "application/json": {
                "examples": {
                    "plan_hash_mismatch": {
                        "value": {
                            "detail": "Submitted plan_hash does not match stored plan_hash",
                            "error": {
                                "code": "plan_hash_mismatch",
                                "message": "Submitted plan_hash does not match stored plan_hash",
                                "details": {},
                            },
                        }
                    },
                    "concurrent_modification": {
                        "value": {
                            "detail": "Concurrent modification",
                            "error": {
                                "code": "concurrent_modification",
                                "message": "Expected version 3, found 4",
                                "details": {"expected_version": 3, "actual_version": 4},
                            },
                        }
                    },
                    "stale_target": {
                        "value": {
                            "detail": "Target version changed before approval",
                            "error": {
                                "code": "stale_target",
                                "message": "Target version changed from v1.1.0 to v1.2.0",
                                "details": {"expected_version": "v1.1.0", "live_version": "v1.2.0"},
                            },
                        }
                    },
                    "already_decided": {
                        "value": {
                            "detail": "Plan has already been decided",
                            "error": {
                                "code": "already_decided",
                                "message": "Plan has already been decided",
                                "details": {},
                            },
                        }
                    },
                    "idempotency_conflict": {
                        "value": {
                            "detail": "Idempotency-Key already used with a different request body",
                            "error": {
                                "code": "idempotency_conflict",
                                "message": "Idempotency-Key already used with a different request body",
                                "details": {},
                            },
                        }
                    },
                }
            }
        },
    },
    410: {
        "description": "Plan approval window expired",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Action plan has expired",
                    "error": {
                        "code": "plan_expired",
                        "message": "Action plan has expired",
                        "details": {},
                    },
                }
            }
        },
    },
    413: {
        "description": "Decision request payload too large",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Decision payload exceeds 8192 bytes",
                    "error": {
                        "code": "payload_too_large",
                        "message": "Decision payload exceeds 8192 bytes",
                        "details": {},
                    },
                }
            }
        },
    },
    422: {
        "description": "Validation error or forbidden identity field in query/header/body",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Request validation failed",
                    "error": {
                        "code": "validation_error",
                        "message": "Request validation failed",
                        "details": {},
                    },
                }
            }
        },
    },
    428: {
        "description": "Missing or invalid If-Match header",
        "content": {
            "application/json": {
                "example": {
                    "detail": "If-Match header (run version) is required",
                    "error": {
                        "code": "precondition_required",
                        "message": "If-Match header (run version) is required",
                        "details": {},
                    },
                }
            }
        },
    },
    429: {
        "description": "Per-reviewer rate limit exceeded",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Rate limit exceeded for plan decisions",
                    "error": {
                        "code": "rate_limited",
                        "message": "Rate limit exceeded for plan decisions",
                        "details": {},
                    },
                }
            }
        },
    },
    503: {
        "description": "Target state unavailable or stale (fail-closed)",
        "content": {
            "application/json": {
                "example": {
                    "detail": "TargetStateReader is unavailable",
                    "error": {
                        "code": "target_state_unavailable",
                        "message": "TargetStateReader is unavailable",
                        "details": {},
                    },
                }
            }
        },
    },
}


def _audit_decision_attempt(
    *,
    reviewer_id: UUID | None,
    plan_id: UUID,
    outcome_code: str,
    reason_length: int,
) -> None:
    """Emits a redacted audit log line (never logs note or raw reason text)."""
    audit_logger.info(
        "plan_decision_attempt reviewer_id=%s plan_id=%s outcome_code=%s reason_length=%d",
        str(reviewer_id) if reviewer_id else "service",
        str(plan_id),
        outcome_code,
        reason_length,
    )


def _error_code_for_exception(exc: DomainError) -> str:
    mapping: dict[type[DomainError], str] = {
        ForbiddenError: "forbidden",
        SeparationOfDutiesError: "separation_of_duties_violation",
        NotFoundError: "not_found",
        PlanHashMismatchError: "plan_hash_mismatch",
        PlanExpiredError: "plan_expired",
        StaleTargetError: "stale_target",
        AlreadyDecidedError: "already_decided",
        DecisionConflictError: "decision_conflict",
        IdempotencyConflictError: "idempotency_conflict",
        PlanSupersededError: "plan_superseded",
        PlanNotProposedError: "plan_not_proposed",
        RunNotInReviewError: "run_not_in_review",
        ConcurrentModificationError: "concurrent_modification",
        TargetStateUnavailableError: "target_state_unavailable",
    }
    for cls, code in mapping.items():
        if isinstance(exc, cls):
            return code
    return "domain_error"


@router.get(
    "/{id}",
    response_model=PlanResponse,
    summary="Get Action Plan",
    description="Read-only plan view for the review screen (display data only).",
)
def get_plan(
    id: UUID,
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> PlanResponse:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = PlanRepository(db)

    try:
        plan = repo.get_by_id(ctx, id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {id} not found",
        )

    return PlanResponse(
        id=plan.id,
        run_id=plan.run_id,
        tenant_id=plan.tenant_id,
        action=plan.action,
        service=plan.service,
        target_id=getattr(plan, "target_id", None),
        expected_version=plan.expected_version,
        rollback_to_version=getattr(plan, "rollback_to_version", None),
        rationale_evidence_ids=plan.rationale_evidence_ids,
        rollback_route=plan.rollback_route,
        plan_hash=plan.plan_hash,
        policy_version=getattr(plan, "policy_version", None),
        status=plan.status,
        notes=plan.notes,
        expires_at=plan.expires_at,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@router.get(
    "/{id}/review",
    response_model=PlanReviewResponse,
    summary="Get Plan Review Payload",
    description="Returns structured facts, evidence links, policy checks, and labelled untrusted notes for the review UI.",
)
def get_plan_review(
    id: UUID,
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> PlanReviewResponse:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = PlanRepository(db)

    try:
        plan, diag, observations, hypotheses = repo.get_review_data(ctx, id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {id} not found",
        )

    from apps.api.services.plan_review import build_plan_review_payload

    return build_plan_review_payload(
        plan=plan,
        diagnosis=diag,
        observations=observations,
        hypotheses=hypotheses,
    )


@router.get(
    "/{id}/decision",
    response_model=PlanDecisionReadResponse,
    summary="Get Recorded Plan Decision",
    description="Reads back the recorded approval/rejection decision for a plan (tenant-scoped).",
)
def get_plan_decision(
    id: UUID,
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> PlanDecisionReadResponse:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = PlanRepository(db)

    try:
        plan = repo.get_by_id(ctx, id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {id} not found",
        )

    approval = db.scalar(
        select(ApprovalModel).where(
            ApprovalModel.plan_id == plan.id,
            ApprovalModel.tenant_id == principal.tenant_id,
        )
    )
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No decision recorded for plan {id}",
        )

    run = db.scalar(
        select(RunModel).where(
            RunModel.id == plan.run_id,
            RunModel.tenant_id == principal.tenant_id,
        )
    )
    exec_row = db.scalar(
        select(ActionExecutionModel).where(
            ActionExecutionModel.plan_id == plan.id,
            ActionExecutionModel.tenant_id == principal.tenant_id,
        )
    )

    decision_val: Any = approval.decision
    return PlanDecisionReadResponse(
        plan_id=plan.id,
        run_id=plan.run_id,
        approval_id=approval.id,
        reviewer_id=approval.reviewer_id,
        decision=decision_val,
        submitted_plan_hash=approval.submitted_plan_hash,
        reason=approval.reason,
        plan_status=plan.status,
        run_status=run.status if run else "unknown",
        run_version=run.version if run else 0,
        action_execution_id=exec_row.id if exec_row else None,
        action_key=exec_row.action_key if exec_row else None,
        decided_at=approval.decided_at,
    )


def _parse_if_match(raw: str | None) -> int:
    if not raw or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header (run version) is required",
        )
    cleaned = raw.strip()
    if cleaned.startswith("W/"):
        cleaned = cleaned[2:].strip()
    cleaned = cleaned.strip('"')
    try:
        version = int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header must be an integer run version",
        ) from exc
    if version < 1:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header must be a positive integer run version",
        )
    return version


def get_target_reader() -> TargetStateReader | None:
    """FastAPI dependency returning the configured TargetStateReader (override in tests)."""
    dsn = os.getenv("EVIDENCE_DB_URL")
    if not dsn:
        return None
    try:
        from tools.read.target_state import LiveTargetReader

        return LiveTargetReader(evidence_db_dsn=dsn)
    except Exception:
        return None


@router.post(
    "/{id}/decisions",
    response_model=PlanDecisionResponse,
    status_code=status.HTTP_200_OK,
    responses=_DECISION_ERROR_RESPONSES,
    summary="Submit Plan Decision",
    description="Atomically approves or rejects a proposed recovery plan with If-Match and Idempotency-Key.",
)
async def submit_plan_decision(
    id: UUID,
    request: Request,
    payload: PlanDecisionRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_role("reviewer")),
    db: Session = Depends(get_db_session),
    target_reader: TargetStateReader | None = Depends(get_target_reader),
) -> DecisionResult:
    reason_len = len(payload.reason)

    # 1. Tight per-route body size limit
    raw_body = await request.body()
    if len(raw_body) > MAX_DECISION_BODY_BYTES:
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code="payload_too_large",
            reason_length=reason_len,
        )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Decision payload exceeds {MAX_DECISION_BODY_BYTES} bytes",
        )

    # 2. Reject forbidden identity fields in query params or headers
    forbidden_q = [k for k in request.query_params.keys() if k.lower() in _FORBIDDEN_IDENTITY_PARAMS]
    forbidden_h = [k for k in request.headers.keys() if k.lower() in _FORBIDDEN_IDENTITY_HEADERS]
    if forbidden_q or forbidden_h:
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code="forbidden_identity_field",
            reason_length=reason_len,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Identity fields (reviewer_id, tenant_id, run_id) must not be supplied in query or headers",
        )

    # 3. CSRF verification
    try:
        verify_csrf(request, is_service_request=principal.is_service)
    except HTTPException:
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code="csrf_failed",
            reason_length=reason_len,
        )
        raise

    # 4. Required Idempotency-Key & If-Match headers
    if not idempotency_key or not idempotency_key.strip():
        approval_metrics.record_refusal("invalid_request")
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code="missing_idempotency_key",
            reason_length=reason_len,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    clean_idemp_key = idempotency_key.strip()
    if len(clean_idemp_key) > 128:
        approval_metrics.record_refusal("invalid_request")
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code="invalid_idempotency_key",
            reason_length=reason_len,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header must be at most 128 characters",
        )

    try:
        expected_version = _parse_if_match(if_match)
    except HTTPException:
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code="precondition_required",
            reason_length=reason_len,
        )
        raise

    # 5. Per-reviewer Redis rate limiting
    rl_key = f"decisions:reviewer:{principal.tenant_id}:{principal.user_id}"
    if plans_rate_limiter.is_rate_limited(rl_key):
        approval_metrics.record_refusal("rate_limited")
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code="rate_limited",
            reason_length=reason_len,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for plan decisions",
        )
    plans_rate_limiter.record_attempt(rl_key)

    # 6. Delegate to ApprovalService.decide() and emit redacted audit log
    service = ApprovalService(db, target_reader=target_reader)
    try:
        result = service.decide(
            principal=principal,
            plan_id=id,
            decision=payload.decision,
            submitted_plan_hash=payload.submitted_plan_hash,
            reason=payload.reason,
            run_version=expected_version,
            idempotency_key=clean_idemp_key,
        )
    except DomainError as exc:
        _audit_decision_attempt(
            reviewer_id=principal.user_id,
            plan_id=id,
            outcome_code=_error_code_for_exception(exc),
            reason_length=reason_len,
        )
        raise

    outcome = "idempotent_replay" if result.idempotent_replay else result.plan_status
    _audit_decision_attempt(
        reviewer_id=principal.user_id,
        plan_id=id,
        outcome_code=outcome,
        reason_length=reason_len,
    )
    return result

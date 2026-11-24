"""Plan review and decision routes.

Enforces:
1. GET /plans/{id} provides read-only display data for review screens, strictly tenant-scoped (404 on cross-tenant).
2. POST /plans/{id}/decisions requires role reviewer or admin and validates PlanDecisionRequest,
   returning 501 Not Implemented (Feature 8).
3. Rate limiting on POST routes.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from apps.api.auth.dependencies import (
    Principal,
    get_db_session,
    require_role,
)
from apps.api.auth.rate_limiter import RateLimiter
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import NotFoundError
from apps.api.repositories.plan_repository import PlanRepository
from apps.api.routers.schemas import (
    PlanDecisionRequest,
    PlanResponse,
)

router = APIRouter(prefix="/plans", tags=["plans"])

plans_rate_limiter = RateLimiter(max_attempts=30, window_seconds=60)


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
        expected_version=plan.expected_version,
        rationale_evidence_ids=plan.rationale_evidence_ids,
        rollback_route=plan.rollback_route,
        plan_hash=plan.plan_hash,
        status=plan.status,
        notes=plan.notes,
        expires_at=plan.expires_at,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@router.post(
    "/{id}/decisions",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Submit Plan Decision (Stub)",
    description="Submits an approval/rejection decision. Returns 501 Not Implemented (deferred to Feature 8).",
)
def submit_plan_decision(
    id: UUID,
    payload: PlanDecisionRequest,
    principal: Principal = Depends(require_role("reviewer")),
    db: Session = Depends(get_db_session),
) -> Response:
    rl_key = f"decisions:{principal.tenant_id}"
    if plans_rate_limiter.is_rate_limited(rl_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for plan decisions",
        )
    plans_rate_limiter.record_attempt(rl_key)

    # In accordance with specification: Feature 8 implements approval transaction.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Plan decision approval transaction is deferred to Feature 8",
    )

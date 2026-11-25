"""Run management routes: creation, retrieval, cursor pagination, evidence, and cancellation.

Enforces:
1. POST /runs requires role reviewer or admin and non-empty Idempotency-Key header.
2. Server-side capping of max_calls via settings.MAX_TOOL_CALLS.
3. Safe 404 semantics on non-existent or cross-tenant resources (no existence leaks).
4. GET /runs with stable cursor pagination scoped strictly to principal tenant.
5. POST /runs/{id}/cancel permitted only from non-terminal states.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.auth.dependencies import (
    Principal,
    get_db_session,
    get_db_session_factory,
    require_role,
)
from apps.api.auth.rate_limiter import RateLimiter
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import (
    ConcurrentModificationError,
    IllegalTransitionError,
    NotFoundError,
)
from apps.api.models.tables import IncidentModel, RunModel
from apps.api.repositories.run_repository import RunRepository
from apps.api.routers.schemas import (
    CreateRunRequest,
    DiagnosisSummary,
    EvidenceResponse,
    HypothesisSummary,
    RunDetailResponse,
    RunEventListResponse,
    RunEventResponse,
    RunListItem,
    RunListResponse,
)
from apps.api.services.event_broadcaster import broadcaster
from apps.api.services.run_service import RunService
from apps.api.settings import get_settings
from tools.common.sanitize import redact

router = APIRouter(prefix="/runs", tags=["runs"])

# Rate limiters: 300 POST calls per minute per tenant (configurable via RUNS_RATE_LIMIT)
runs_rate_limiter = RateLimiter(max_attempts=int(os.getenv("RUNS_RATE_LIMIT", "300")), window_seconds=60)


@router.post(
    "",
    response_model=RunDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Run",
    description="Initializes an investigation run in status 'queued'. Idempotency-Key header is required.",
)
def create_run(
    request: Request,
    response: Response,
    payload: CreateRunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_role("reviewer")),
    db: Session = Depends(get_db_session),
) -> RunDetailResponse:
    # 1. Enforce required Idempotency-Key header
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    clean_idemp_key = idempotency_key.strip()

    # 2. Rate limiting check
    rl_key = f"runs:{principal.tenant_id}"
    if runs_rate_limiter.is_rate_limited(rl_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for run creation",
        )
    runs_rate_limiter.record_attempt(rl_key)

    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = RunRepository(db)
    settings = get_settings()

    # Cap max_calls server-side
    configured_cap = settings.max_tool_calls
    effective_max_calls = (
        min(payload.max_calls, configured_cap)
        if payload.max_calls is not None
        else configured_cap
    )

    # 3. Check for existing idempotent run
    existing_run = repo.get_by_idempotency_key(ctx, clean_idemp_key)
    if existing_run is not None:
        # Same key + same body -> return existing run
        if (
            existing_run.incident_id == payload.incident_id
            and existing_run.max_calls == effective_max_calls
        ):
            response.status_code = status.HTTP_200_OK
            evidence_ids = repo.get_run_evidence_ids(ctx, existing_run.id)
            hypotheses = [
                HypothesisSummary(
                    id=h.id,
                    statement=h.statement,
                    category=h.category,
                    mechanism=h.mechanism,
                    service=h.service,
                    status=h.status,
                    supporting_evidence_ids=h.supporting_evidence_ids,
                    contrary_evidence_ids=h.contrary_evidence_ids,
                    confidence=float(h.confidence),
                    created_at=h.created_at,
                )
                for h in repo.get_run_hypotheses(ctx, existing_run.id)
            ]
            proposed_plan_id = repo.get_run_proposed_plan_id(ctx, existing_run.id)
            diag = repo.get_run_diagnosis(ctx, existing_run.id)
            diag_summary = (
                DiagnosisSummary(
                    id=diag.id,
                    root_cause_service=diag.root_cause_service,
                    root_cause_category=diag.root_cause_category,
                    mechanism=diag.mechanism,
                    culprit_deploy_id=diag.culprit_deploy_id,
                    evidence_ids=diag.evidence_ids,
                    confidence=float(diag.confidence),
                    created_at=diag.created_at,
                )
                if diag
                else None
            )
            return RunDetailResponse(
                id=existing_run.id,
                tenant_id=existing_run.tenant_id,
                incident_id=existing_run.incident_id,
                case_id=existing_run.case_id,
                status=existing_run.status,
                version=existing_run.version,
                remaining_calls=existing_run.remaining_calls,
                max_calls=existing_run.max_calls,
                last_error=existing_run.last_error,
                evidence_ids=evidence_ids,
                hypotheses=hypotheses,
                proposed_plan_id=proposed_plan_id,
                diagnosis=diag_summary,
                created_at=existing_run.created_at,
                updated_at=existing_run.updated_at,
                finished_at=existing_run.finished_at,
            )
        else:
            # Same key + different body -> 409 Conflict
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key already used with different request body",
            )

    # 4. Verify incident exists within tenant context (404 on cross-tenant / missing)
    incident = db.scalar(
        select(IncidentModel).where(
            IncidentModel.id == payload.incident_id,
            IncidentModel.tenant_id == principal.tenant_id,
        )
    )
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident {payload.incident_id} not found",
        )

    # 5. Create new run in queued status
    run = RunModel(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        incident_id=payload.incident_id,
        idempotency_key=clean_idemp_key,
        status="queued",
        version=1,
        remaining_calls=effective_max_calls,
        max_calls=effective_max_calls,
    )
    repo.create(ctx, run)
    repo.append_event(
        ctx=ctx,
        run_id=run.id,
        event_type="run_created",
        actor=f"user:{principal.user_id}" if principal.user_id else "system",
        payload={"max_calls": effective_max_calls, "idempotency_key": clean_idemp_key},
    )
    db.commit()

    return RunDetailResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        incident_id=run.incident_id,
        case_id=run.case_id,
        status=run.status,
        version=run.version,
        remaining_calls=run.remaining_calls,
        max_calls=run.max_calls,
        last_error=run.last_error,
        evidence_ids=[],
        hypotheses=[],
        proposed_plan_id=None,
        diagnosis=None,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
    )


@router.get(
    "",
    response_model=RunListResponse,
    summary="List Runs",
    description="Lists runs for current tenant with status and incident filters, using cursor pagination.",
)
def list_runs(
    status: str | None = Query(default=None, description="Optional status filter"),
    incident_id: UUID | None = Query(default=None, description="Optional incident filter"),
    limit: int = Query(default=20, ge=1, le=100, description="Page limit"),
    cursor: str | None = Query(default=None, description="Opaque cursor token for next page"),
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> RunListResponse:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = RunRepository(db)
    items, next_cursor, has_more = repo.list_runs(
        ctx=ctx,
        status=status,
        incident_id=incident_id,
        limit=limit,
        cursor=cursor,
    )

    list_items = [
        RunListItem(
            id=r.id,
            tenant_id=r.tenant_id,
            incident_id=r.incident_id,
            case_id=r.case_id,
            status=r.status,
            version=r.version,
            remaining_calls=r.remaining_calls,
            max_calls=r.max_calls,
            last_error=r.last_error,
            created_at=r.created_at,
            updated_at=r.updated_at,
            finished_at=r.finished_at,
        )
        for r in items
    ]

    return RunListResponse(
        items=list_items,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/{id}",
    response_model=RunDetailResponse,
    summary="Get Run",
    description=(
        "Returns detailed investigation run state, evidence IDs, hypotheses, plan, "
        "diagnosis (outcome kind, reason, capped confidence, ledger summary, gate rejections count)."
    ),
)
def get_run(
    id: UUID,
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> RunDetailResponse:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = RunRepository(db)

    try:
        run = repo.get_by_id(ctx, id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {id} not found",
        )

    evidence_ids = repo.get_run_evidence_ids(ctx, id)
    hypotheses = [
        HypothesisSummary(
            id=h.id,
            statement=h.statement,
            category=h.category,
            mechanism=h.mechanism,
            service=h.service,
            status=h.status,
            supporting_evidence_ids=h.supporting_evidence_ids,
            contrary_evidence_ids=h.contrary_evidence_ids,
            confidence=float(h.confidence),
            created_at=h.created_at,
        )
        for h in repo.get_run_hypotheses(ctx, id)
    ]
    proposed_plan_id = repo.get_run_proposed_plan_id(ctx, id)
    diag = repo.get_run_diagnosis(ctx, id)
    diag_summary = (
        DiagnosisSummary(
            id=diag.id,
            root_cause_service=diag.root_cause_service,
            root_cause_category=diag.root_cause_category,
            mechanism=diag.mechanism,
            culprit_deploy_id=diag.culprit_deploy_id,
            evidence_ids=diag.evidence_ids,
            confidence=float(diag.confidence),
            outcome_kind=diag.outcome_kind,
            outcome_reason=diag.outcome_reason,
            model_confidence=diag.model_confidence,
            capped_confidence=float(diag.capped_confidence) if diag.capped_confidence is not None else None,
            ledger_summary=diag.ledger_summary,
            what_would_change=diag.what_would_change,
            gate_rejection_count=diag.gate_rejection_count,
            created_at=diag.created_at,
        )
        if diag
        else None
    )

    return RunDetailResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        incident_id=run.incident_id,
        case_id=run.case_id,
        status=run.status,
        version=run.version,
        remaining_calls=run.remaining_calls,
        max_calls=run.max_calls,
        last_error=run.last_error,
        evidence_ids=evidence_ids,
        hypotheses=hypotheses,
        proposed_plan_id=proposed_plan_id,
        diagnosis=diag_summary,
        ledger_summary=diag.ledger_summary if diag else None,
        gate_rejection_count=diag.gate_rejection_count if diag else 0,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
    )


@router.get(
    "/{id}/evidence",
    response_model=list[EvidenceResponse],
    summary="List Run Evidence",
    description="Read-only view of all quarantined observations gathered for a run.",
)
def list_run_evidence(
    id: UUID,
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> list[EvidenceResponse]:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = RunRepository(db)

    try:
        observations = repo.get_evidence_list(ctx, id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {id} not found",
        )

    return [
        EvidenceResponse(
            id=obs.id,
            evidence_id=obs.evidence_id,
            run_id=obs.run_id,
            tenant_id=obs.tenant_id,
            tool=obs.tool,
            source_id=obs.source_id,
            captured_at=obs.captured_at,
            window_start=obs.window_start,
            window_end=obs.window_end,
            args=obs.args,
            summary=obs.summary,
            payload=obs.payload or {},
            payload_ref=obs.payload_ref,
            source_version=obs.source_version,
            content_hash=obs.content_hash,
            created_at=obs.created_at,
        )
        for obs in observations
    ]


@router.get(
    "/{id}/evidence/{evidence_id}",
    response_model=EvidenceResponse,
    summary="Get Run Evidence Item",
    description="Read-only view of a specific quarantined observation.",
)
def get_run_evidence_item(
    id: UUID,
    evidence_id: str,
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> EvidenceResponse:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = RunRepository(db)

    try:
        obs = repo.get_evidence_by_id(ctx, id, evidence_id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evidence {evidence_id} not found for run {id}",
        )

    return EvidenceResponse(
        id=obs.id,
        evidence_id=obs.evidence_id,
        run_id=obs.run_id,
        tenant_id=obs.tenant_id,
        tool=obs.tool,
        source_id=obs.source_id,
        captured_at=obs.captured_at,
        window_start=obs.window_start,
        window_end=obs.window_end,
        args=obs.args,
        summary=obs.summary,
        payload=obs.payload or {},
        payload_ref=obs.payload_ref,
        source_version=obs.source_version,
        content_hash=obs.content_hash,
        created_at=obs.created_at,
    )


@router.post(
    "/{id}/cancel",
    response_model=RunDetailResponse,
    summary="Cancel Run",
    description="Cancels an active run. Allowed only from non-terminal states per the transition table.",
)
def cancel_run(
    id: UUID,
    principal: Principal = Depends(require_role("reviewer")),
    db: Session = Depends(get_db_session),
) -> RunDetailResponse:
    # 1. Enforce rate limiting
    rl_key = f"cancel:{principal.tenant_id}"
    if runs_rate_limiter.is_rate_limited(rl_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for run cancellation",
        )
    runs_rate_limiter.record_attempt(rl_key)

    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = RunRepository(db)

    try:
        run = repo.get_by_id(ctx, id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {id} not found",
        )

    # 2. Check terminal states
    if run.status in ("completed", "escalated"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel run in terminal status '{run.status}'",
        )

    # 3. Transition to escalated via RunService
    service = RunService(db, repo)
    try:
        updated_run, _ = service.transition(
            ctx=ctx,
            run_id=id,
            to_status="escalated",
            actor=f"user:{principal.user_id}" if principal.user_id else "system",
            expected_version=run.version,
            payload={
                "reason": "run_cancelled_by_user",
                "cancelled_by_user_id": str(principal.user_id) if principal.user_id else None,
            },
        )
        db.commit()
    except IllegalTransitionError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        )
    except ConcurrentModificationError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=e.message,
        )

    # 4. Return updated run detail
    evidence_ids = repo.get_run_evidence_ids(ctx, id)
    hypotheses = [
        HypothesisSummary(
            id=h.id,
            statement=h.statement,
            category=h.category,
            mechanism=h.mechanism,
            service=h.service,
            status=h.status,
            supporting_evidence_ids=h.supporting_evidence_ids,
            contrary_evidence_ids=h.contrary_evidence_ids,
            confidence=float(h.confidence),
            created_at=h.created_at,
        )
        for h in repo.get_run_hypotheses(ctx, id)
    ]
    proposed_plan_id = repo.get_run_proposed_plan_id(ctx, id)
    diag = repo.get_run_diagnosis(ctx, id)
    diag_summary = (
        DiagnosisSummary(
            id=diag.id,
            root_cause_service=diag.root_cause_service,
            root_cause_category=diag.root_cause_category,
            mechanism=diag.mechanism,
            culprit_deploy_id=diag.culprit_deploy_id,
            evidence_ids=diag.evidence_ids,
            confidence=float(diag.confidence),
            created_at=diag.created_at,
        )
        if diag
        else None
    )

    return RunDetailResponse(
        id=updated_run.id,
        tenant_id=updated_run.tenant_id,
        incident_id=updated_run.incident_id,
        case_id=updated_run.case_id,
        status=updated_run.status,
        version=updated_run.version,
        remaining_calls=updated_run.remaining_calls,
        max_calls=updated_run.max_calls,
        last_error=updated_run.last_error,
        evidence_ids=evidence_ids,
        hypotheses=hypotheses,
        proposed_plan_id=proposed_plan_id,
        diagnosis=diag_summary,
        created_at=updated_run.created_at,
        updated_at=updated_run.updated_at,
        finished_at=updated_run.finished_at,
    )


def sanitize_event_payload(data: Any) -> Any:
    """Recursively redacts secrets and sensitive information from event payloads."""
    if isinstance(data, str):
        return redact(data)
    elif isinstance(data, dict):
        return {k: sanitize_event_payload(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_event_payload(item) for item in data]
    return data


@router.get(
    "/{id}/events",
    summary="Get Run Events (Polling or SSE)",
    description=(
        "Returns run events in plain JSON (polling) or Server-Sent Events stream "
        "(SSE when Accept: text/event-stream). Woken by Redis pub/sub with PostgreSQL "
        "as authoritative source of truth."
    ),
)
async def get_run_events(
    request: Request,
    id: UUID,
    after_seq: int = Query(default=0, ge=0, description="Resume sequence number"),
    limit: int = Query(default=100, ge=1, le=500, description="Page limit for polling mode"),
    heartbeat_seconds: float = Query(default=15.0, ge=0.01, le=60.0, description="Heartbeat interval for SSE"),
    max_duration_seconds: float = Query(default=300.0, ge=0.01, le=3600.0, description="Maximum SSE connection duration"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(require_role("viewer")),
    db: Session = Depends(get_db_session),
) -> Any:
    ctx = TenantContext(tenant_id=principal.tenant_id)
    repo = RunRepository(db)

    # Validate that run exists within caller's tenant (safe 404 on cross-tenant)
    try:
        repo.get_by_id(ctx, id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {id} not found",
        )

    accept_header = request.headers.get("accept", "")

    # Mode 1: Polling mode (plain JSON list)
    if "text/event-stream" not in accept_header:
        events = repo.get_run_events(ctx, id, after_seq=after_seq, limit=limit + 1)
        has_more = len(events) > limit
        items = events[:limit]
        last_seq = items[-1].seq if items else after_seq

        sanitized_items = [
            RunEventResponse(
                id=e.id,
                run_id=e.run_id,
                tenant_id=e.tenant_id,
                seq=e.seq,
                type=e.type,
                actor=e.actor,
                payload=sanitize_event_payload(e.payload),
                created_at=e.created_at,
            )
            for e in items
        ]
        return RunEventListResponse(items=sanitized_items, last_seq=last_seq, has_more=has_more)

    # Mode 2: Server-Sent Events (SSE stream)
    resume_seq = after_seq
    if last_event_id and last_event_id.strip():
        try:
            resume_seq = int(last_event_id.strip())
        except ValueError:
            resume_seq = after_seq

    async def sse_stream() -> AsyncGenerator[str, None]:
        current_seq = resume_seq
        start_time = asyncio.get_running_loop().time()
        factory = get_db_session_factory()

        def fetch_events(after: int) -> tuple[list[dict[str, Any]], str]:
            session = factory()
            try:
                r_repo = RunRepository(session)
                ev_models = r_repo.get_run_events(ctx, id, after_seq=after)
                r_model = r_repo.get_by_id(ctx, id)
                serialized = [
                    {
                        "id": e.id,
                        "run_id": str(e.run_id),
                        "tenant_id": str(e.tenant_id),
                        "seq": e.seq,
                        "type": e.type,
                        "actor": e.actor,
                        "payload": sanitize_event_payload(e.payload),
                        "created_at": e.created_at.isoformat(),
                    }
                    for e in ev_models
                ]
                return serialized, r_model.status
            finally:
                session.close()

        # 1. Initial catch-up from DB
        ev_list, current_status = await asyncio.to_thread(fetch_events, current_seq)
        for ev in ev_list:
            current_seq = ev["seq"]
            data_json = json.dumps(ev)
            yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {data_json}\n\n"

        if current_status in ("completed", "escalated", "failed"):
            yield ": stream closed: terminal status\n\n"
            return

        # 2. Reactive loop: wake on broadcaster, heartbeat every heartbeat_seconds
        while True:
            if await request.is_disconnected():
                break

            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed >= max_duration_seconds:
                yield ": connection duration cap reached\n\n"
                break

            remaining_time = max_duration_seconds - elapsed
            wait_timeout = min(heartbeat_seconds, remaining_time)

            got_event = await broadcaster.wait_for_event(id, timeout=wait_timeout)
            if not got_event:
                yield ": heartbeat\n\n"
            else:
                ev_list, current_status = await asyncio.to_thread(fetch_events, current_seq)
                for ev in ev_list:
                    current_seq = ev["seq"]
                    data_json = json.dumps(ev)
                    yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {data_json}\n\n"

                if current_status in ("completed", "escalated", "failed"):
                    remaining_evs, _ = await asyncio.to_thread(fetch_events, current_seq)
                    for ev in remaining_evs:
                        current_seq = ev["seq"]
                        data_json = json.dumps(ev)
                        yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {data_json}\n\n"
                    yield ": stream closed: terminal status\n\n"
                    break

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


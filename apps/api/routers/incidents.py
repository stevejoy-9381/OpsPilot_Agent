"""Incident management routes: creation from AlertEvent or benchmark case.

Enforces:
1. Role requirement: reviewer or admin.
2. Tenant derived strictly from session principal.
3. Server-side validation of AlertEvent.
4. Optional Idempotency-Key header support (same key + same body -> same incident;
   same key + different body -> 409 Conflict).
5. Rate limiting on POST routes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from apps.api.auth.dependencies import (
    Principal,
    get_db_session,
    require_role,
)
from apps.api.auth.rate_limiter import RateLimiter
from apps.api.domain.context import TenantContext
from apps.api.models.tables import IncidentModel
from apps.api.repositories.incident_repository import IncidentRepository
from apps.api.routers.schemas import (
    CreateIncidentRequest,
    IncidentResponse,
)
from baseline.alerts.models import AlertEvent

router = APIRouter(prefix="/incidents", tags=["incidents"])

# Rate limiter for incident creation: 30 requests per minute per tenant
incident_rate_limiter = RateLimiter(max_attempts=30, window_seconds=60)

# In-memory idempotency cache for incidents: (tenant_id, key) -> (body_hash, incident_dict)
_incident_idempotency_cache: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}


def _get_seeded_alert(case_id: str) -> AlertEvent:
    """Constructs a deterministic baseline AlertEvent for a benchmark scenario."""
    now = datetime.now(timezone.utc)
    case_clean = case_id.strip().upper()
    if case_clean in ("S1", "S5"):
        return AlertEvent(
            alert_id=f"alt_seeded_{case_clean.lower()}",
            fired_at=now,
            service="orders",
            rule_id="orders_db_pool_saturation",
            metric="db_pool_in_use",
            threshold=0.9,
            observed_value=0.95,
            window_seconds=60,
            severity="CRITICAL",
            status="firing",
        )
    return AlertEvent(
        alert_id=f"alt_seeded_{case_clean.lower()}",
        fired_at=now,
        service="inventory",
        rule_id="inventory_latency_p95_high",
        metric="latency_p95",
        threshold=300.0,
        observed_value=350.0,
        window_seconds=60,
        severity="CRITICAL",
        status="firing",
    )


@router.post(
    "",
    response_model=IncidentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Incident",
    description="Creates a new incident from a validated AlertEvent or seeded benchmark case reference.",
)
def create_incident(
    request: Request,
    response: Response,
    payload: CreateIncidentRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_role("reviewer")),
    db: Session = Depends(get_db_session),
) -> IncidentResponse:
    # 1. Enforce rate limiting
    rl_key = f"incidents:{principal.tenant_id}"
    if incident_rate_limiter.is_rate_limited(rl_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for incident creation",
        )
    incident_rate_limiter.record_attempt(rl_key)

    # 2. Check idempotency if key supplied
    ctx = TenantContext(tenant_id=principal.tenant_id)
    raw_body_bytes = json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    body_hash = hashlib.sha256(raw_body_bytes).hexdigest()

    if idempotency_key:
        cache_key = (str(principal.tenant_id), idempotency_key.strip())
        if cache_key in _incident_idempotency_cache:
            cached_hash, cached_resp = _incident_idempotency_cache[cache_key]
            if cached_hash != body_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key already used with different request body",
                )
            response.status_code = status.HTTP_200_OK
            return IncidentResponse(**cached_resp)

    # 3. Resolve alert event and metadata
    alert = payload.alert
    source = payload.source or ("seeded_case" if payload.case_id else "alert")
    if alert is None:
        if not payload.case_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Either 'alert' or 'case_id' must be supplied",
            )
        alert = _get_seeded_alert(payload.case_id)

    service = payload.service or alert.service
    severity = payload.severity or alert.severity
    alert_payload_dict = alert.model_dump(mode="json")
    opened_at = alert.fired_at

    # 4. Persist incident via repository
    repo = IncidentRepository(db)
    incident = IncidentModel(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        alert_id=alert.alert_id,
        source=source,
        service=service,
        severity=severity,
        alert_payload=alert_payload_dict,
        opened_at=opened_at,
        status_summary=payload.status_summary,
    )
    repo.create(ctx, incident)
    db.commit()

    resp_data = IncidentResponse(
        id=incident.id,
        tenant_id=incident.tenant_id,
        alert_id=incident.alert_id,
        source=incident.source,
        service=incident.service,
        severity=incident.severity,
        alert_payload=incident.alert_payload,
        opened_at=incident.opened_at,
        status_summary=incident.status_summary,
        created_at=incident.created_at,
    )

    if idempotency_key:
        cache_key = (str(principal.tenant_id), idempotency_key.strip())
        _incident_idempotency_cache[cache_key] = (body_hash, resp_data.model_dump(mode="json"))

    return resp_data

"""Pydantic request, response, and error schemas for the OpsPilot REST API.

Enforces:
1. Strict schema validation with extra="forbid" to reject unauthorized/model-supplied parameters.
2. Standardized error envelope representation: {"detail": "...", "error": {"code": "...", "message": "...", "details": {...}}}.
3. Cursor pagination response structures.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baseline.alerts.models import AlertEvent

# ---------------------------------------------------------------------------
# Error Envelope Schemas
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    """Detailed error object embedded in the standard error envelope."""

    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error description")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Context-specific structured error details"
    )


class ErrorEnvelope(BaseModel):
    """Standardized error envelope returned across all OpsPilot API error responses."""

    detail: str = Field(description="Legacy/standard HTTP exception detail string")
    error: ErrorDetail = Field(description="Structured domain error detail")


# ---------------------------------------------------------------------------
# Incident Schemas
# ---------------------------------------------------------------------------


class CreateIncidentRequest(BaseModel):
    """Payload to create an incident from a validated AlertEvent or seeded case reference."""

    model_config = ConfigDict(extra="forbid")

    alert: AlertEvent | None = Field(
        default=None,
        description="Full serialized and validated AlertEvent payload",
    )
    case_id: str | None = Field(
        default=None,
        description="Seeded benchmark case reference (e.g. S1, S2, S3, S4, S5)",
    )
    source: str | None = Field(
        default=None,
        description="Optional origin override ('alert' or 'seeded_case')",
    )
    service: str | None = Field(
        default=None,
        description="Optional microservice override if derived",
    )
    severity: str | None = Field(
        default=None,
        description="Optional severity level ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
    )
    status_summary: str = Field(
        default="open",
        description="Initial status summary",
    )

    @model_validator(mode="after")
    def check_alert_or_case_id(self) -> CreateIncidentRequest:
        if self.alert is None and not self.case_id:
            raise ValueError("Either 'alert' or 'case_id' must be provided")
        return self


class IncidentResponse(BaseModel):
    """Representation of an Incident aggregate."""

    id: UUID
    tenant_id: UUID
    alert_id: str
    source: str
    service: str
    severity: str
    alert_payload: dict[str, Any]
    opened_at: datetime
    status_summary: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Run Schemas
# ---------------------------------------------------------------------------


class CreateRunRequest(BaseModel):
    """Payload to initialize an investigation run on an existing incident."""

    model_config = ConfigDict(extra="forbid")

    incident_id: UUID = Field(description="ID of owning incident")
    max_calls: int | None = Field(
        default=None,
        gt=0,
        description="Optional maximum tool call limit (capped server-side by MAX_TOOL_CALLS)",
    )


class HypothesisSummary(BaseModel):
    """Summary of an investigation hypothesis."""

    id: UUID
    statement: str
    category: str
    mechanism: str
    service: str | None = None
    status: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contrary_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float
    created_at: datetime


class DiagnosisSummary(BaseModel):
    """Summary of an investigation diagnosis."""

    id: UUID
    root_cause_service: str | None = None
    root_cause_category: str
    mechanism: str
    culprit_deploy_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float
    created_at: datetime


class RunDetailResponse(BaseModel):
    """Detailed representation of an investigation run including all linked aggregates."""

    id: UUID
    tenant_id: UUID
    incident_id: UUID
    case_id: str | None = None
    status: str
    version: int
    remaining_calls: int
    max_calls: int
    last_error: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    hypotheses: list[HypothesisSummary] = Field(default_factory=list)
    proposed_plan_id: UUID | None = None
    diagnosis: DiagnosisSummary | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class RunListItem(BaseModel):
    """Item representation for cursor-paginated run listings."""

    id: UUID
    tenant_id: UUID
    incident_id: UUID
    case_id: str | None = None
    status: str
    version: int
    remaining_calls: int
    max_calls: int
    last_error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class RunListResponse(BaseModel):
    """Paginated collection of runs."""

    items: list[RunListItem]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Evidence & Observation Schemas
# ---------------------------------------------------------------------------


class EvidenceResponse(BaseModel):
    """Read-only view of a quarantined observation record."""

    id: UUID
    evidence_id: str
    run_id: UUID
    tenant_id: UUID
    tool: str
    source_id: str
    captured_at: datetime
    window_start: datetime
    window_end: datetime
    args: dict[str, Any]
    summary: str
    payload: dict[str, Any]
    payload_ref: str | None = None
    source_version: str
    content_hash: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Action Plan & Decision Schemas
# ---------------------------------------------------------------------------


class PlanResponse(BaseModel):
    """Read-only display data for an Action Plan."""

    id: UUID
    run_id: UUID
    tenant_id: UUID
    action: str
    service: str | None = None
    expected_version: str | None = None
    rationale_evidence_ids: list[str] = Field(default_factory=list)
    rollback_route: str | None = None
    plan_hash: str
    status: str
    notes: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class PlanDecisionRequest(BaseModel):
    """Request model for submitting an operator decision on a proposed recovery plan."""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(description="Review decision ('approved' or 'rejected')")
    submitted_plan_hash: str = Field(description="Exact cryptographic SHA-256 hash of reviewed plan")
    reason: str = Field(default="", description="Operator rationale for review decision")


# ---------------------------------------------------------------------------
# Run Event Schemas
# ---------------------------------------------------------------------------


class RunEventResponse(BaseModel):
    """Representation of an emitted audit run event."""

    id: int
    run_id: UUID
    tenant_id: UUID
    seq: int
    type: str
    actor: str
    payload: dict[str, Any]
    created_at: datetime


class RunEventListResponse(BaseModel):
    """Collection response for polling mode GET /runs/{id}/events."""

    items: list[RunEventResponse]
    last_seq: int
    has_more: bool


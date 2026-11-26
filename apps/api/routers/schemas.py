"""Pydantic request, response, and error schemas for the OpsPilot REST API.

Enforces:
1. Strict schema validation with extra="forbid" to reject unauthorized/model-supplied parameters.
2. Standardized error envelope representation: {"detail": "...", "error": {"code": "...", "message": "...", "details": {...}}}.
3. Cursor pagination response structures.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baseline.alerts.models import AlertEvent
from shared.plans import sanitize_note

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
    """Summary of an investigation diagnosis with deterministic outcome and confidence cap."""

    id: UUID
    root_cause_service: str | None = None
    root_cause_category: str
    mechanism: str
    culprit_deploy_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float
    outcome_kind: str = Field(
        default="unknown",
        description="Resolved outcome kind: diagnosed, unknown, or escalate",
    )
    outcome_reason: str = Field(
        default="concluded_by_model",
        description="Typed outcome reason code (e.g. concluded_by_model, false_alarm_no_action, gate_not_satisfied)",
    )
    model_confidence: str | None = Field(
        default=None,
        description="Model-proposed confidence label or value before deterministic capping",
    )
    capped_confidence: float | None = Field(
        default=None,
        description="Deterministic confidence cap applied by the contrary-evidence gate",
    )
    ledger_summary: dict[str, Any] | None = Field(
        default=None,
        description="Summary of hypotheses, statuses, and refuted rivals with refuting evidence",
    )
    what_would_change: str | None = Field(
        default=None,
        description="Length-capped, quarantined display text describing what would change this conclusion",
    )
    gate_rejection_count: int = Field(
        default=0,
        description="Number of contrary-evidence gate rejections emitted during the run",
    )
    created_at: datetime


class ExecutionAuditEntry(BaseModel):
    """Reviewer-visible audit trail entry for an execution."""

    time: datetime
    who: str
    what: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ExecutionSummary(BaseModel):
    """Broker execution summary exposed on GET /runs/{id}."""

    id: UUID
    plan_id: UUID
    status: str
    attempts: int = 0
    refusal_code: str | None = None
    target_id: str | None = None
    target_before_version: str | None = None
    target_after_version: str | None = None
    created_at: datetime
    dispatched_at: datetime | None = None
    acknowledged_at: datetime | None = None
    reconciled_at: datetime | None = None
    completed_at: datetime | None = None
    audit_trail: list[ExecutionAuditEntry] = Field(default_factory=list)


class RunDetailResponse(BaseModel):
    """Detailed representation of an investigation run including all linked aggregates."""

    id: UUID
    tenant_id: UUID
    incident_id: UUID
    case_id: str | None = None
    status: str
    resolution: str | None = None
    version: int
    remaining_calls: int
    max_calls: int
    last_error: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    hypotheses: list[HypothesisSummary] = Field(default_factory=list)
    proposed_plan_id: UUID | None = None
    diagnosis: DiagnosisSummary | None = None
    ledger_summary: dict[str, Any] | None = Field(
        default=None,
        description="Hypothesis ledger summary including refuted rivals and refuting evidence",
    )
    gate_rejection_count: int = Field(
        default=0,
        description="Number of contrary-evidence gate rejections during the run",
    )
    execution: ExecutionSummary | None = None
    execution_status: str | None = None
    execution_attempts: int | None = None
    execution_refusal_code: str | None = None
    target_before_version: str | None = None
    target_after_version: str | None = None
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
    resolution: str | None = None
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
    target_id: str | None = None
    expected_version: str | None = None
    rollback_to_version: str | None = None
    rationale_evidence_ids: list[str] = Field(default_factory=list)
    rollback_route: str | None = None
    plan_hash: str
    policy_version: str | None = None
    status: str
    notes: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class UntrustedTextField(BaseModel):
    """Explicitly labelled untrusted model-generated text field (capped and redacted)."""

    label: Literal["untrusted model text"] = "untrusted model text"
    text: str = ""


class ReviewDiagnosisSummary(BaseModel):
    """Structured diagnosis facts for the review screen."""

    category: str
    mechanism: str
    service: str | None = None
    culprit_deploy_id: str | None = None
    capped_confidence: float | None = None
    model_label: str | None = None
    outcome_kind: str
    outcome_reason: str


class ReviewEvidenceItem(BaseModel):
    """Structured evidence fact row (no raw logs or diff text)."""

    evidence_id: str
    tool: str
    source: str
    window_start: datetime
    window_end: datetime
    numeric_summary: dict[str, int | float] = Field(default_factory=dict)
    evidence_url: str


class ReviewLedgerRival(BaseModel):
    """Structured rival hypothesis considered and refuting evidence."""

    hypothesis_id: str
    category: str
    mechanism: str
    service: str | None = None
    status: str
    refuting_evidence_ids: list[str] = Field(default_factory=list)


class ReviewLedgerSummary(BaseModel):
    """Structured hypothesis ledger summary for the review screen."""

    leading_hypothesis_id: str | None = None
    rivals_considered: list[ReviewLedgerRival] = Field(default_factory=list)


class ReviewPolicyCheck(BaseModel):
    """Single deterministic policy check record (P1-P10)."""

    rule_id: str
    code: str
    passed: bool
    inputs: dict[str, Any] = Field(default_factory=dict)


class ReviewPolicyResults(BaseModel):
    """Policy version and deterministic checks passed (P1-P10)."""

    policy_version: str | None = None
    checks_passed: list[ReviewPolicyCheck] = Field(default_factory=list)


class PlanReviewResponse(BaseModel):
    """Fact-driven review screen payload for GET /plans/{id}/review."""

    plan_id: UUID
    run_id: UUID
    tenant_id: UUID
    status: str
    target_id: str | None = None
    service: str | None = None
    action: str
    current_version: str | None = None
    target_version: str | None = None
    rollback_route: str | None = None
    diagnosis: ReviewDiagnosisSummary | None = None
    evidence: list[ReviewEvidenceItem] = Field(default_factory=list)
    ledger_summary: ReviewLedgerSummary = Field(default_factory=ReviewLedgerSummary)
    policy_results: ReviewPolicyResults = Field(default_factory=ReviewPolicyResults)
    expires_at: datetime
    plan_hash: str
    model_note: UntrustedTextField = Field(default_factory=UntrustedTextField)
    what_would_change: UntrustedTextField = Field(default_factory=UntrustedTextField)


class PlanDecisionRequest(BaseModel):
    """Request model for submitting an operator decision on a proposed recovery plan."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"] = Field(
        description="Review decision ('approve' or 'reject')"
    )
    submitted_plan_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Exact cryptographic SHA-256 hash of reviewed plan",
    )
    reason: str = Field(
        default="",
        max_length=500,
        description="Operator plain-text rationale (max 500 chars; optional for approve, required for reject)",
    )

    @field_validator("reason", mode="before")
    @classmethod
    def sanitize_reason(cls, v: Any) -> str:
        return sanitize_note(v, max_length=500)

    @model_validator(mode="after")
    def require_reason_on_reject(self) -> PlanDecisionRequest:
        if self.decision == "reject" and not self.reason.strip():
            raise ValueError("reason is required when decision is 'reject'")
        return self


class PlanDecisionResponse(BaseModel):
    """Response model for POST /plans/{id}/decisions."""

    plan_id: UUID
    run_id: UUID
    approval_id: UUID
    decision: Literal["approve", "reject"]
    plan_status: str
    run_status: str
    run_version: int
    action_execution_id: UUID | None = None
    action_key: str | None = None
    decided_at: datetime
    idempotent_replay: bool = False


class PlanDecisionReadResponse(BaseModel):
    """Read-only response model for GET /plans/{id}/decision."""

    plan_id: UUID
    run_id: UUID
    approval_id: UUID
    reviewer_id: UUID
    decision: Literal["approve", "reject"]
    submitted_plan_hash: str
    reason: str
    plan_status: str
    run_status: str
    run_version: int
    action_execution_id: UUID | None = None
    action_key: str | None = None
    decided_at: datetime


# ---------------------------------------------------------------------------
# Run Event Schemas
# ---------------------------------------------------------------------------


class RunEventResponse(BaseModel):
    """Representation of an emitted audit run or execution event."""

    id: int
    run_id: UUID
    tenant_id: UUID
    seq: int
    type: str
    actor: str
    payload: dict[str, Any]
    created_at: datetime
    source: str = "run"
    execution_id: UUID | None = None


class RunEventListResponse(BaseModel):
    """Collection response for polling mode GET /runs/{id}/events."""

    items: list[RunEventResponse]
    last_seq: int
    has_more: bool


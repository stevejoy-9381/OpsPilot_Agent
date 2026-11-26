"""Plan domain models, canonical plan_hash, and event summary helpers for OpsPilot.

Implements Feature 7 (Phase 7.2) per docs/proposal_design.md:
- PlanStatus enum
- ProposalInput (model-supplied, extra fields forbidden)
- ActionPlan (frozen Pydantic model with server-derived fields)
- canonical_utc_iso / sha256_canonical_json / plan_hash (single canonical hash implementation)
- plan_summary (free-text-free audit event summary)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.actions import ActionType

MAX_RATIONALE_EVIDENCE_IDS: int = 8
MAX_NOTE_LENGTH: int = 300
ALLOWED_PLAN_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.ROLLBACK_SERVICE, ActionType.RESTART_SERVICE}
)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_CTRL_RE = re.compile(r"[\r\n\t]+")


class PlanStatus(str, Enum):
    """Lifecycle states for an ActionPlan."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    EXECUTED = "executed"
    FAILED = "failed"


def sanitize_note(value: Any, max_length: int = MAX_NOTE_LENGTH) -> str:
    """Strips control characters and enforces plain-text length bounds on notes."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("note must be a string")
    cleaned = _CONTROL_CHAR_RE.sub("", value)
    cleaned = _WHITESPACE_CTRL_RE.sub(" ", cleaned).strip()
    if len(cleaned) > max_length:
        raise ValueError(f"note must be at most {max_length} characters (got {len(cleaned)})")
    return cleaned


def canonical_utc_iso(value: datetime | str) -> str:
    """Normalizes a datetime or ISO-8601 string to a UTC ISO-8601 string with Z suffix."""
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    elif isinstance(value, datetime):
        dt = value
    else:
        raise TypeError(f"Expected datetime or ISO string, got {type(value).__name__}")

    if dt.tzinfo is None:
        dt_utc = dt.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normalize_utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    elif isinstance(value, datetime):
        dt = value
    else:
        raise TypeError(f"Expected datetime or ISO string, got {type(value).__name__}")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_json_value(val: Any) -> Any:
    if isinstance(val, datetime):
        return canonical_utc_iso(val)
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, Enum):
        return str(val.value)
    if isinstance(val, list):
        return [_normalize_json_value(item) for item in val]
    if isinstance(val, dict):
        return {str(k): _normalize_json_value(v) for k, v in val.items()}
    return val


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serializes a mapping into compact, key-sorted canonical JSON."""
    normalized = {str(k): _normalize_json_value(v) for k, v in payload.items()}
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sha256_canonical_json(payload: Mapping[str, Any]) -> str:
    """Computes lowercase hex SHA-256 over canonical JSON."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _coerce_id(value: str | UUID, field_name: str) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} must be non-empty")
        return cleaned
    raise ValueError(f"{field_name} must be a string or UUID")


class ProposalInput(BaseModel):
    """Model-supplied recovery proposal payload. Server-derived fields are forbidden."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ActionType = Field(description="Active recovery action (rollback_service or restart_service)")
    service: str = Field(description="Affected target service name")
    expected_version: str = Field(min_length=1, max_length=64, description="Observed active version")
    rationale_evidence_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_RATIONALE_EVIDENCE_IDS,
        description="Supporting evidence_id references (max 8)",
    )
    note: str = Field(
        default="",
        max_length=MAX_NOTE_LENGTH,
        description="Short plain-text note (max 300 chars; untrusted display text)",
    )

    @field_validator("action")
    @classmethod
    def validate_action_is_plan_action(cls, v: ActionType) -> ActionType:
        if v not in ALLOWED_PLAN_ACTIONS:
            raise ValueError(
                f"Action '{v.value}' cannot become a plan; only rollback_service and restart_service are allowed"
            )
        return v

    @field_validator("service")
    @classmethod
    def validate_service(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not cleaned:
            raise ValueError("service must be non-empty")
        return cleaned

    @field_validator("expected_version")
    @classmethod
    def validate_expected_version(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("expected_version must be non-empty")
        return cleaned

    @field_validator("rationale_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("rationale_evidence_ids must contain at least 1 evidence_id")
        if len(v) > MAX_RATIONALE_EVIDENCE_IDS:
            raise ValueError(
                f"rationale_evidence_ids cannot exceed {MAX_RATIONALE_EVIDENCE_IDS} items"
            )
        cleaned: list[str] = []
        for item in v:
            s = item.strip()
            if not s:
                raise ValueError("rationale_evidence_ids must not contain empty strings")
            cleaned.append(s)
        return cleaned

    @field_validator("note", mode="before")
    @classmethod
    def validate_note(cls, v: Any) -> str:
        return sanitize_note(v, max_length=MAX_NOTE_LENGTH)


def plan_hash(
    plan_fields: ActionPlan | Mapping[str, Any] | None = None,
    /,
    *,
    run_id: str | UUID | None = None,
    tenant_id: str | UUID | None = None,
    action: ActionType | str | None = None,
    service: str | None = None,
    target_id: str | None = None,
    expected_version: str | None = None,
    rollback_to_version: str | None = None,
    rationale_evidence_ids: list[str] | tuple[str, ...] | None = None,
    expires_at: datetime | str | None = None,
) -> str:
    """Computes canonical SHA-256 plan_hash over the immutable plan commitment fields.

    Hashed fields (per docs/proposal_design.md Section 5):
      run_id, tenant_id, action, service, target_id, expected_version,
      rollback_to_version, sorted(set(rationale_evidence_ids)), expires_at (UTC ISO).

    Unhashed fields (note, status, plan_id, created_at, rollback_route) are excluded.
    """
    if plan_fields is not None:
        if isinstance(plan_fields, ActionPlan):
            data: Mapping[str, Any] = {
                "run_id": plan_fields.run_id,
                "tenant_id": plan_fields.tenant_id,
                "action": plan_fields.action.value,
                "service": plan_fields.service,
                "target_id": plan_fields.target_id,
                "expected_version": plan_fields.expected_version,
                "rollback_to_version": plan_fields.rollback_to_version,
                "rationale_evidence_ids": plan_fields.rationale_evidence_ids,
                "expires_at": plan_fields.expires_at,
            }
        elif isinstance(plan_fields, Mapping):
            data = plan_fields
        else:
            raise TypeError("plan_fields must be an ActionPlan or Mapping")

        run_id = data["run_id"]
        tenant_id = data["tenant_id"]
        action = data["action"]
        service = data["service"]
        target_id = data["target_id"]
        expected_version = data["expected_version"]
        rollback_to_version = data.get("rollback_to_version")
        rationale_evidence_ids = data["rationale_evidence_ids"]
        expires_at = data["expires_at"]

    if (
        run_id is None
        or tenant_id is None
        or action is None
        or service is None
        or target_id is None
        or expected_version is None
        or rationale_evidence_ids is None
        or expires_at is None
    ):
        raise ValueError("Missing required fields for plan_hash computation")

    action_str = action.value if isinstance(action, ActionType) else str(action).strip().lower()
    rb_version = rollback_to_version.strip() if rollback_to_version is not None else None
    sorted_evidence = sorted({str(eid).strip() for eid in rationale_evidence_ids})

    canonical_payload: dict[str, Any] = {
        "action": action_str,
        "expected_version": expected_version.strip(),
        "expires_at": canonical_utc_iso(expires_at),
        "rationale_evidence_ids": sorted_evidence,
        "rollback_to_version": rb_version,
        "run_id": _coerce_id(run_id, "run_id"),
        "service": service.strip().lower(),
        "target_id": target_id.strip(),
        "tenant_id": _coerce_id(tenant_id, "tenant_id"),
    }
    return sha256_canonical_json(canonical_payload)


def compute_action_key(plan_id: str | UUID, plan_hash_hex: str) -> str:
    """Computes deterministic lowercase hex SHA-256 action_key from plan_id and plan_hash.

    Encoding: UTF-8 of f"{canonical_plan_id}:{canonical_plan_hash}" where plan_id is
    lowercased/stripped and plan_hash_hex is 64-char lowercase hex.
    """
    pid = _coerce_id(plan_id, "plan_id").lower()
    phash = plan_hash_hex.strip().lower()
    if not phash:
        raise ValueError("plan_hash_hex must be non-empty")
    return hashlib.sha256(f"{pid}:{phash}".encode("utf-8")).hexdigest()


class ActionPlan(BaseModel):
    """Frozen, server-validated recovery action plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(description="Server-generated plan identifier")
    tenant_id: str = Field(description="Tenant boundary identifier")
    run_id: str = Field(description="Owning investigation run identifier")
    action: ActionType = Field(description="Permitted plan action (rollback_service or restart_service)")
    service: str = Field(description="Affected target service")
    target_id: str = Field(description="Allowlisted lab target identifier")
    expected_version: str = Field(min_length=1, max_length=64, description="Expected live version")
    rollback_to_version: str | None = Field(
        default=None,
        description="Previous version for rollback_service; None for restart_service",
    )
    rollback_route: str = Field(min_length=1, max_length=255, description="Undo metadata (data only)")
    rationale_evidence_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_RATIONALE_EVIDENCE_IDS,
        description="Supporting evidence IDs",
    )
    note: str = Field(
        default="",
        max_length=MAX_NOTE_LENGTH,
        description="Sanitized untrusted note (excluded from plan_hash)",
    )
    status: PlanStatus = Field(default=PlanStatus.PROPOSED, description="Plan lifecycle status")
    created_at: datetime = Field(description="UTC creation timestamp")
    expires_at: datetime = Field(description="UTC expiry timestamp")
    plan_hash: str = Field(default="", description="Canonical SHA-256 plan_hash")

    @field_validator("plan_id", "tenant_id", "run_id", mode="before")
    @classmethod
    def coerce_identifiers(cls, v: Any, info: Any) -> str:
        return _coerce_id(v, str(info.field_name))

    @field_validator("action")
    @classmethod
    def validate_plan_action(cls, v: ActionType) -> ActionType:
        if v not in ALLOWED_PLAN_ACTIONS:
            raise ValueError(
                f"Action '{v.value}' cannot become an ActionPlan; only rollback_service and restart_service are allowed"
            )
        return v

    @field_validator("service")
    @classmethod
    def validate_service(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not cleaned:
            raise ValueError("ActionPlan requires a non-empty service")
        return cleaned

    @field_validator("target_id", "expected_version", "rollback_route")
    @classmethod
    def validate_non_empty_str(cls, v: str, info: Any) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned

    @field_validator("rationale_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("rationale_evidence_ids must be non-empty")
        if len(v) > MAX_RATIONALE_EVIDENCE_IDS:
            raise ValueError(
                f"rationale_evidence_ids cannot exceed {MAX_RATIONALE_EVIDENCE_IDS} items"
            )
        cleaned: list[str] = []
        for item in v:
            s = item.strip()
            if not s:
                raise ValueError("rationale_evidence_ids must not contain empty strings")
            cleaned.append(s)
        return cleaned

    @field_validator("note", mode="before")
    @classmethod
    def validate_note(cls, v: Any) -> str:
        return sanitize_note(v, max_length=MAX_NOTE_LENGTH)

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def normalize_timestamps(cls, v: Any) -> datetime:
        return _normalize_utc_datetime(v)

    @model_validator(mode="after")
    def validate_invariants_and_hash(self) -> ActionPlan:
        if self.action == ActionType.ROLLBACK_SERVICE and not self.rollback_to_version:
            raise ValueError("rollback_service requires rollback_to_version")

        if self.status == PlanStatus.PROPOSED:
            now_utc = datetime.now(timezone.utc)
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must be after created_at")
            if self.expires_at <= now_utc:
                raise ValueError("expires_at must be in the future at creation")

        computed = plan_hash(self)
        if not self.plan_hash:
            object.__setattr__(self, "plan_hash", computed)
        elif self.plan_hash.strip().lower() != computed:
            raise ValueError(
                f"plan_hash mismatch: provided '{self.plan_hash}', expected '{computed}'"
            )
        return self


def plan_summary(plan: ActionPlan) -> dict[str, Any]:
    """Builds a compact, free-text-free audit event summary for an ActionPlan."""
    return {
        "plan_id": plan.plan_id,
        "run_id": plan.run_id,
        "tenant_id": plan.tenant_id,
        "target_id": plan.target_id,
        "action": plan.action.value,
        "service": plan.service,
        "expected_version": plan.expected_version,
        "rollback_to_version": plan.rollback_to_version,
        "status": plan.status.value,
        "plan_hash": plan.plan_hash,
        "expires_at": canonical_utc_iso(plan.expires_at),
    }

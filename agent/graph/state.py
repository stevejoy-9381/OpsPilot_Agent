"""agent/graph/state.py

Authoritative LangGraph state definition, explicit reducers, termination mapping,
and serialization helpers for the OpsPilot Investigator Agent.

Constraints:
1. InvestigationState is a TypedDict with explicit reducers for list fields:
   - evidence_ids: append-only (deduplicated, order-preserving)
   - tool_trace: append-only (lightweight ToolCallRecordRef dictionaries)
   All scalar fields follow last-write-wins.
2. State holds only IDs, summaries, and references; NEVER full raw payloads
   (raw logs, traces, metric points, and diffs live strictly in app.observations).
3. State must be strictly JSON-serializable for LangGraph checkpointing.
4. Total serialized size for a worst-case run (8 tool calls) must remain below
   MAX_WORST_CASE_STATE_BYTES (16 KB).
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis

# Documented maximum byte cap for a serialized worst-case investigation run (8 tool calls)
MAX_WORST_CASE_STATE_BYTES: int = 16384  # 16 KB


class TerminationReason(str, Enum):
    """Enumeration of deterministic and model-directed investigation termination triggers."""

    CONCLUDED_BY_MODEL = "concluded_by_model"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STEP_CAP_REACHED = "step_cap_reached"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    PROVIDER_ERROR_EXHAUSTED = "provider_error_exhausted"
    INVALID_STEP_CAP_REACHED = "invalid_step_cap_reached"


# Mapping from TerminationReason to final run status (operationalizing Section 5.1 of investigator design)
TERMINATION_STATUS_MAP: dict[TerminationReason, str] = {
    TerminationReason.CONCLUDED_BY_MODEL: "needs_review",
    TerminationReason.BUDGET_EXHAUSTED: "escalated",
    TerminationReason.STEP_CAP_REACHED: "escalated",
    TerminationReason.TIMEOUT_EXCEEDED: "escalated",
    TerminationReason.TOKEN_BUDGET_EXCEEDED: "escalated",
    TerminationReason.PROVIDER_ERROR_EXHAUSTED: "escalated",
    TerminationReason.INVALID_STEP_CAP_REACHED: "escalated",
}


def get_final_status_for_termination(reason: TerminationReason | str) -> str:
    """Returns the canonical run status associated with a termination reason."""
    try:
        norm = TerminationReason(reason) if isinstance(reason, str) else reason
        return TERMINATION_STATUS_MAP[norm]
    except (ValueError, KeyError):
        return "escalated"


class ToolCallRecordRef(BaseModel):
    """Lightweight audit reference to a tool execution.
    
    Contains execution metadata and IDs, strictly excluding raw telemetry payloads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(description="Name of the invoked tool")
    args_summary: str = Field(
        default="",
        description="Concise representation of input arguments (max 200 chars)",
    )
    duration_ms: float = Field(default=0.0, ge=0.0, description="Tool execution duration in ms")
    ok: bool = Field(default=True, description="True if tool returned success envelope")
    error_code: str | None = Field(default=None, description="Error code if tool execution failed")
    evidence_id: str | None = Field(default=None, description="Generated evidence citation ID")
    approx_tokens: int = Field(default=0, ge=0, description="Approximate token weight of response")

    @classmethod
    def from_record(cls, record: Any) -> ToolCallRecordRef:
        """Converts a ToolCallRecord, dict, or object into a sanitized ToolCallRecordRef."""
        if isinstance(record, ToolCallRecordRef):
            return record

        if isinstance(record, dict):
            args_raw = record.get("args") or record.get("args_summary", "")
            if isinstance(args_raw, dict):
                args_summary = ", ".join(f"{k}={v}" for k, v in sorted(args_raw.items()))[:200]
            else:
                args_summary = str(args_raw)[:200]

            return cls(
                tool=str(record.get("tool", "")),
                args_summary=args_summary,
                duration_ms=float(record.get("duration_ms", 0.0)),
                ok=bool(record.get("ok", True)),
                error_code=record.get("error_code"),
                evidence_id=record.get("evidence_id"),
                approx_tokens=int(record.get("approx_tokens", 0)),
            )

        # Object with attributes (e.g. tools.registry.ToolCallRecord)
        args_raw = getattr(record, "args", getattr(record, "args_summary", ""))
        if isinstance(args_raw, dict):
            args_summary = ", ".join(f"{k}={v}" for k, v in sorted(args_raw.items()))[:200]
        else:
            args_summary = str(args_raw)[:200]

        return cls(
            tool=str(getattr(record, "tool", "")),
            args_summary=args_summary,
            duration_ms=float(getattr(record, "duration_ms", 0.0)),
            ok=bool(getattr(record, "ok", True)),
            error_code=getattr(record, "error_code", None),
            evidence_id=getattr(record, "evidence_id", None),
            approx_tokens=int(getattr(record, "approx_tokens", 0)),
        )


def append_evidence_ids(
    current: list[str] | None,
    new_items: list[str] | str | None,
) -> list[str]:
    """Append-only reducer for evidence IDs preserving insertion order and deduplicating."""
    result = list(current) if current else []
    if not new_items:
        return result
    items = [new_items] if isinstance(new_items, str) else new_items
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def append_tool_trace(
    current: list[dict[str, Any]] | None,
    new_items: list[dict[str, Any] | ToolCallRecordRef] | dict[str, Any] | ToolCallRecordRef | None,
) -> list[dict[str, Any]]:
    """Append-only reducer for tool trace reference records."""
    result = list(current) if current else []
    if not new_items:
        return result
    items = [new_items] if not isinstance(new_items, list) else new_items
    for item in items:
        if isinstance(item, ToolCallRecordRef):
            result.append(item.model_dump())
        elif isinstance(item, dict):
            ref = ToolCallRecordRef.from_record(item)
            result.append(ref.model_dump())
        elif hasattr(item, "__dict__"):
            ref = ToolCallRecordRef.from_record(item)
            result.append(ref.model_dump())
    return result


class InvestigationState(TypedDict, total=False):
    """Authoritative LangGraph state schema for incident investigation.

    Explicit Reducers:
    - evidence_ids: append-only, deduplicated, order-preserving.
    - tool_trace: append-only list of ToolCallRecordRef dictionaries.
    
    All other fields (run_id, tenant_id, incident_id, status, alert, messages_summary,
    remaining_calls, step_count, draft_diagnosis, last_error, termination_reason)
    behave as last-write-wins scalars.
    """

    run_id: str
    tenant_id: str
    incident_id: str
    status: str
    alert: dict[str, Any]
    messages_summary: str
    evidence_ids: Annotated[list[str], append_evidence_ids]
    tool_trace: Annotated[list[dict[str, Any]], append_tool_trace]
    remaining_calls: int
    step_count: int
    draft_diagnosis: dict[str, Any] | None
    last_error: dict[str, Any] | None
    termination_reason: str | None
    invalid_step_count: int
    total_tokens_consumed: int
    last_step_decision: dict[str, Any] | None
    transient_tool_result: dict[str, Any] | None


class InvestigationStateModel(BaseModel):
    """Pydantic validation model for InvestigationState reflecting the exact state schema."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    tenant_id: str
    incident_id: str
    status: str = "investigating"
    alert: dict[str, Any]
    messages_summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    tool_trace: list[ToolCallRecordRef] = Field(default_factory=list)
    remaining_calls: int = Field(default=8, ge=0, le=8)
    step_count: int = Field(default=0, ge=0)
    draft_diagnosis: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    termination_reason: str | None = None
    invalid_step_count: int = Field(default=0, ge=0)
    total_tokens_consumed: int = Field(default=0, ge=0)
    last_step_decision: dict[str, Any] | None = None
    transient_tool_result: dict[str, Any] | None = None


_STATE_TYPE_ADAPTER: TypeAdapter[InvestigationState] = TypeAdapter(InvestigationState)


def create_initial_state(
    run_id: str,
    tenant_id: str,
    incident_id: str,
    alert: AlertEvent | dict[str, Any],
    max_calls: int = 8,
) -> InvestigationState:
    """Creates a cleanly initialized InvestigationState dictionary."""
    alert_dict = alert.model_dump(mode="json") if isinstance(alert, AlertEvent) else dict(alert)
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "status": "investigating",
        "alert": alert_dict,
        "messages_summary": "",
        "evidence_ids": [],
        "tool_trace": [],
        "remaining_calls": max_calls,
        "step_count": 0,
        "draft_diagnosis": None,
        "last_error": None,
        "termination_reason": None,
        "invalid_step_count": 0,
        "total_tokens_consumed": 0,
        "last_step_decision": None,
    }


def state_to_dict(state: InvestigationState | InvestigationStateModel | dict[str, Any]) -> dict[str, Any]:
    """Converts state to a clean, JSON-serializable dictionary."""
    if isinstance(state, InvestigationStateModel):
        return state.model_dump(mode="json")

    # If state contains Pydantic models in nested fields, dump them
    result: dict[str, Any] = {}
    for key, val in state.items():
        if isinstance(val, (BaseModel, AlertEvent, Diagnosis)):
            result[key] = val.model_dump(mode="json")
        elif isinstance(val, list):
            result[key] = [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in val
            ]
        elif isinstance(val, Enum):
            result[key] = val.value
        else:
            result[key] = val
    return result


def state_to_json(state: InvestigationState | InvestigationStateModel | dict[str, Any]) -> str:
    """Serializes InvestigationState to a canonical JSON string."""
    clean_dict = state_to_dict(state)
    return json.dumps(clean_dict, sort_keys=True, default=str)


def state_from_json(raw: str) -> InvestigationState:
    """Parses and validates a JSON string into an InvestigationState TypedDict."""
    data = json.loads(raw)
    validated = _STATE_TYPE_ADAPTER.validate_python(data)
    return validated

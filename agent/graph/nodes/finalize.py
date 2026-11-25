"""agent/graph/nodes/finalize.py

LangGraph finalizer node executing deterministic post-investigation validation,
citation integrity enforcement, draft persistence, and handoff dispatch.

Completely deterministic; NO LLM calls.

Responsibilities:
1. Citation Integrity Enforcement:
   - Queries `app.observations` scoped strictly to `run_id` and `tenant_id`.
   - Reuses `grade_evidence_support` to check citation existence, source scope,
     and temporal alignment with the incident window.
   - Any nonexistent, foreign, or out-of-window evidence IDs are stripped.
   - Emits a `{type: "citation_rejected", ids}` run_event.
   - If zero valid IDs remain for a non-unknown diagnosis, downgrades the diagnosis
     to `unknown` with reason "unsupported_by_recorded_evidence".
   - Never trusts confidence numbers; verifies only empirical evidence and structure.
2. Persistence:
   - Persists the validated draft to `app.diagnoses` with status "draft" and emits an audit event.
3. Handoff & Optimistic Locking:
   - Maps graph conclusion to non-terminal marker state `concluded_draft`.
   - Dispatches via `HandoffPort.propose(...)` (default: transitions to `escalated`
     with reason 'investigation_complete_no_planner').
   - Handles optimistic locking conflicts (`ConcurrentModificationError`) gracefully.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from agent.graph.handoff import DefaultHandoffStub, HandoffPort
from agent.graph.state import (
    InvestigationState,
    TerminationReason,
)
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import ConcurrentModificationError
from apps.api.models.tables import ObservationModel
from apps.api.services.run_service import RunService
from evals.graders.evidence import grade_evidence_support
from evals.graders.models import Diagnosis, EvidenceItem

logger = logging.getLogger("opspilot.agent.finalize")

ALLOWED_TOOL_SOURCES: set[str] = {
    "prometheus",
    "logs",
    "traces",
    "deploys",
    "diff",
    "get_metrics",
    "search_logs",
    "get_trace",
    "list_deploys",
    "get_deploy_diff",
}


def _extract_incident_window(state: InvestigationState) -> tuple[datetime, datetime] | None:
    """Extracts or computes the incident time window from state alert context."""
    state_dict: dict[str, Any] = state  # type: ignore[assignment]
    if state_dict.get("incident_window"):
        raw_win = state_dict["incident_window"]
        return (raw_win[0], raw_win[1])

    alert = state.get("alert")
    if not isinstance(alert, dict):
        return None

    fired_at_raw = alert.get("fired_at")
    if not fired_at_raw:
        return None

    if isinstance(fired_at_raw, str):
        fired_at = datetime.fromisoformat(fired_at_raw)
    else:
        fired_at = fired_at_raw

    window_sec = int(alert.get("window_seconds", 300))
    # 2x sustained window buffer before/after firing time
    start = fired_at - timedelta(seconds=window_sec * 2)
    end = fired_at + timedelta(seconds=window_sec * 2)
    return (start, end)


def finalize_node(
    state: InvestigationState,
    run_service: RunService | None = None,
    handoff_port: HandoffPort | None = None,
    incident_window: tuple[datetime, datetime] | None = None,
    observations: Sequence[ObservationModel | EvidenceItem | dict[str, Any]] | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Deterministic finalization node for the OpsPilot Investigator Graph.

    Validates citations against recorded observations, persists the draft diagnosis,
    and hands off the run to downstream policy/planner services.
    """
    run_id_str = state.get("run_id")
    tenant_id_str = state.get("tenant_id")
    run_id = UUID(run_id_str) if run_id_str else uuid4()
    tenant_ctx = (
        TenantContext(tenant_id=UUID(tenant_id_str))
        if tenant_id_str
        else TenantContext(tenant_id=uuid4())
    )

    # 1. Collect recorded observations for THIS run and tenant
    raw_obs_list: list[Any] = []
    if observations is not None:
        raw_obs_list = list(observations)
    elif run_service is not None and run_id_str and tenant_id_str:
        raw_obs_list = run_service.run_repo.get_evidence_list(tenant_ctx, run_id)

    evidence_lookup: dict[str, EvidenceItem] = {}
    for item in raw_obs_list:
        if isinstance(item, EvidenceItem):
            evidence_lookup[item.evidence_id] = item
        elif isinstance(item, ObservationModel):
            raw_src = getattr(item, "source_id", None) or getattr(item, "tool", "telemetry")
            src_str = str(raw_src) if raw_src is not None else "telemetry"
            src = src_str.split(":")[0] if ":" in src_str else src_str
            ts_val = item.window_end or item.captured_at
            evidence_lookup[item.evidence_id] = EvidenceItem(
                evidence_id=item.evidence_id,
                timestamp=ts_val,
                source=src,
            )
        elif isinstance(item, dict):
            raw_src = item.get("source_id") or item.get("tool") or item.get("source", "telemetry")
            src_str = str(raw_src) if raw_src is not None else "telemetry"
            src = src_str.split(":")[0] if ":" in src_str else src_str
            ts_raw = item.get("window_end") or item.get("captured_at") or item.get("timestamp")
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw)
            elif isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                ts = datetime.now(timezone.utc)
            evidence_lookup[item["evidence_id"]] = EvidenceItem(
                evidence_id=item["evidence_id"],
                timestamp=ts,
                source=src,
            )

    # 2. Extract and parse current diagnosis draft
    draft_raw: Any = state.get("draft_diagnosis") or {}
    try:
        diagnosis = Diagnosis.model_validate(draft_raw)
    except Exception as exc:
        logger.warning("Draft diagnosis invalid in finalize node: %s; falling back to unknown", exc)
        diagnosis = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=[],
            confidence=0.0,
        )

    # 3. Reuse grade_evidence_support to validate groundedness and incident window
    eval_window = incident_window or _extract_incident_window(state)
    grade = grade_evidence_support(
        diagnosis=diagnosis,
        evidence_store=evidence_lookup,
        incident_window=eval_window,
        allowed_tools=ALLOWED_TOOL_SOURCES,
    )

    # Fabricated / foreign IDs (not in app.observations for this run) + out-of-window IDs
    rejected_ids = sorted(
        list(set(grade.invalid_ids + grade.out_of_window_ids + grade.unauthorized_tool_ids))
    )

    if rejected_ids:
        logger.warning(
            "Rejecting %d invalid/out-of-window citations: invalid=%s out_of_window=%s unauthorized=%s",
            len(rejected_ids),
            grade.invalid_ids,
            grade.out_of_window_ids,
            grade.unauthorized_tool_ids,
        )
        if run_service:
            run_service.append_event(
                ctx=tenant_ctx,
                run_id=run_id,
                event_type="citation_rejected",
                actor="investigator",
                payload={
                    "rejected_ids": rejected_ids,
                    "invalid_ids": grade.invalid_ids,
                    "out_of_window_ids": grade.out_of_window_ids,
                    "unauthorized_tool_ids": grade.unauthorized_tool_ids,
                },
            )

        # Strip rejected IDs
        valid_evidence_ids = [eid for eid in diagnosis.evidence_ids if eid not in rejected_ids]

        # If zero valid IDs remain for a non-unknown diagnosis, downgrade
        if len(valid_evidence_ids) == 0 and diagnosis.root_cause_category != "unknown":
            logger.warning(
                "Downgrading diagnosis to unknown: zero valid evidence IDs remaining for %s",
                diagnosis.root_cause_category,
            )
            diagnosis = Diagnosis(
                root_cause_service=None,
                root_cause_category="unknown",
                mechanism="unknown",
                culprit_deploy_id=None,
                evidence_ids=[],
                confidence=0.0,
            )
            if run_service:
                run_service.append_event(
                    ctx=tenant_ctx,
                    run_id=run_id,
                    event_type="diagnosis_downgraded",
                    actor="investigator",
                    payload={
                        "reason": "unsupported_by_recorded_evidence",
                        "stripped_ids": rejected_ids,
                        "previous_category": draft_raw.get("root_cause_category"),
                    },
                )
        else:
            diagnosis = Diagnosis(
                root_cause_service=diagnosis.root_cause_service,
                root_cause_category=diagnosis.root_cause_category,
                mechanism=diagnosis.mechanism,
                culprit_deploy_id=diagnosis.culprit_deploy_id,
                evidence_ids=valid_evidence_ids,
                confidence=diagnosis.confidence,
            )

    # 4. Persist diagnosis draft in app.diagnoses with status 'draft'
    if run_service:
        try:
            run_service.save_diagnosis(
                ctx=tenant_ctx,
                run_id=run_id,
                diagnosis_data=diagnosis.model_dump(),
                status="draft",
            )
        except Exception as exc:
            logger.error("Failed to persist diagnosis draft to DB: %s", exc)

    # 5. Non-terminal marker state inside graph: concluded_draft
    termination_reason = (
        state.get("termination_reason") or TerminationReason.CONCLUDED_BY_MODEL.value
    )
    state_updates: dict[str, Any] = {
        "draft_diagnosis": diagnosis.model_dump(),
        "evidence_ids": diagnosis.evidence_ids,
        "status": "concluded_draft",
        "termination_reason": termination_reason,
    }

    # 6. Call Handoff Port
    effective_handoff = handoff_port or (
        DefaultHandoffStub(run_service) if run_service else None
    )
    if effective_handoff:
        try:
            merged_state: dict[str, Any] = dict(state)
            merged_state.update(state_updates)
            handoff_res = effective_handoff.propose(
                ctx=tenant_ctx,
                run_id=run_id,
                state=merged_state,  # type: ignore[arg-type]
                expected_version=expected_version,
            )
            state_updates["status"] = handoff_res.target_status
            state_updates["handoff_result"] = {
                "target_status": handoff_res.target_status,
                "reason": handoff_res.reason,
            }
        except ConcurrentModificationError as cm_err:
            logger.error(
                "Optimistic locking conflict during handoff for run %s: expected %s, got %s",
                cm_err.entity_id,
                cm_err.expected_version,
                cm_err.actual_version,
            )
            state_updates["last_error"] = {
                "code": "concurrent_modification",
                "entity_id": str(cm_err.entity_id),
                "expected_version": cm_err.expected_version,
                "actual_version": cm_err.actual_version,
                "message": f"Run {cm_err.entity_id} was modified concurrently",
            }
            state_updates["status"] = "failed"

    return state_updates

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
from datetime import datetime, timedelta
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
from tools.common.sanitize import quarantine

logger = logging.getLogger("opspilot.agent.finalize")


def _build_ledger_summary(state: InvestigationState) -> dict[str, Any]:
    """Builds a compact summary of hypotheses, statuses, and refuted rivals with evidence."""
    ledger_dict = state.get("hypotheses_ledger") or {}
    hypotheses: list[dict[str, Any]] = []
    rivals_refuted: list[dict[str, Any]] = []

    for h_id, h_data in ledger_dict.items():
        if not isinstance(h_data, dict):
            continue
        status = str(h_data.get("status", "open"))
        supp = list(h_data.get("supporting_evidence_ids") or [])
        contra = list(h_data.get("contrary_evidence_ids") or [])
        hypotheses.append(
            {
                "id": h_id,
                "statement": h_data.get("statement", ""),
                "category": h_data.get("category", "unknown"),
                "mechanism": h_data.get("mechanism", "unknown"),
                "service": h_data.get("service"),
                "status": status,
                "supporting_evidence_ids": supp,
                "contrary_evidence_ids": contra,
            }
        )
        if status == "refuted":
            rivals_refuted.append(
                {
                    "id": h_id,
                    "statement": h_data.get("statement", ""),
                    "refuting_evidence_ids": contra,
                }
            )

    return {
        "hypotheses": hypotheses,
        "rivals_refuted": rivals_refuted,
    }


def _extract_what_would_change(state: InvestigationState, max_chars: int = 500) -> str | None:
    """Extracts, length-caps, and quarantines the model's 'what would change' note."""
    last_step = state.get("last_step_decision")
    raw_note: str | None = None
    if isinstance(last_step, dict):
        raw_note = last_step.get("what_would_change") or last_step.get("reason")
    elif last_step is not None:
        raw_note = getattr(last_step, "what_would_change", None) or getattr(last_step, "reason", None)

    if not raw_note or not str(raw_note).strip():
        return None

    return quarantine(
        str(raw_note).strip(),
        origin="model_conclusion_note",
        max_chars=max_chars,
    ).text

ALLOWED_TOOL_SOURCES: set[str] = {
    "prometheus",
    "logs",
    "traces",
    "deploys",
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
            src = str(getattr(item, "source_id", None) or getattr(item, "tool", "telemetry"))
            evidence_lookup[item.evidence_id] = EvidenceItem(
                evidence_id=item.evidence_id,
                timestamp=item.captured_at,
                source=src,
            )
        elif isinstance(item, dict):
            ts = item.get("captured_at") or item.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            evidence_lookup[item["evidence_id"]] = EvidenceItem(
                evidence_id=item["evidence_id"],
                timestamp=ts or datetime.now(),
                source=item.get("tool") or item.get("source", "telemetry"),
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

    is_v2 = state.get("schema_version") == "v2"
    ledger_summary = _build_ledger_summary(state) if is_v2 else None
    what_would_change = _extract_what_would_change(state) if is_v2 else None
    gate_rejection_count = int(state.get("gate_rejection_count", 0))

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
            "Rejecting %d invalid/out-of-window citations: %s",
            len(rejected_ids),
            rejected_ids,
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
                model_confidence=diagnosis.model_confidence,
                capped_confidence=0.0 if is_v2 else None,
                outcome_kind="unknown" if is_v2 else None,
                outcome_reason="unsupported_by_recorded_evidence" if is_v2 else None,
                ledger_summary=ledger_summary,
                what_would_change=what_would_change,
                gate_rejection_count=gate_rejection_count,
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
                model_confidence=diagnosis.model_confidence,
                capped_confidence=diagnosis.capped_confidence,
                outcome_kind=diagnosis.outcome_kind,
                outcome_reason=diagnosis.outcome_reason,
                ledger_summary=ledger_summary,
                what_would_change=what_would_change,
                gate_rejection_count=gate_rejection_count,
            )
    elif is_v2:
        # Enrich v2 Diagnosis with ledger_summary, what_would_change, outcome metadata
        resolved_kind = diagnosis.outcome_kind or (
            "unknown" if diagnosis.root_cause_category == "unknown" else "diagnosed"
        )
        resolved_reason = diagnosis.outcome_reason or (
            state.get("termination_reason")
            or ("false_alarm_no_action" if diagnosis.root_cause_category == "false_alarm" else "concluded_by_model")
        )
        diagnosis = Diagnosis(
            root_cause_service=diagnosis.root_cause_service,
            root_cause_category=diagnosis.root_cause_category,
            mechanism=diagnosis.mechanism,
            culprit_deploy_id=diagnosis.culprit_deploy_id,
            evidence_ids=diagnosis.evidence_ids,
            confidence=diagnosis.confidence,
            model_confidence=diagnosis.model_confidence,
            capped_confidence=diagnosis.capped_confidence,
            outcome_kind=resolved_kind,
            outcome_reason=resolved_reason,
            ledger_summary=ledger_summary,
            what_would_change=what_would_change,
            gate_rejection_count=gate_rejection_count,
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
        diagnosis.outcome_reason
        or state.get("termination_reason")
        or TerminationReason.CONCLUDED_BY_MODEL.value
    )
    state_updates: dict[str, Any] = {
        "draft_diagnosis": diagnosis.model_dump(),
        "evidence_ids": diagnosis.evidence_ids,
        "status": "concluded_draft",
        "termination_reason": termination_reason,
    }

    # 6. Run status mapping through RunService (v2) or HandoffPort
    def _resolve_version() -> int:
        if expected_version is not None:
            return expected_version
        if run_service and hasattr(run_service, "run_repo") and hasattr(run_service.run_repo, "get_by_id"):
            return int(run_service.run_repo.get_by_id(tenant_ctx, run_id).version)
        return 1

    try:
        if is_v2 and run_service and diagnosis.outcome_kind == "diagnosed" and diagnosis.root_cause_category == "false_alarm":
            # Option A: false_alarm -> completed with action no_action recorded
            ver = _resolve_version()
            updated_run, event = run_service.transition(
                ctx=tenant_ctx,
                run_id=run_id,
                to_status="completed",
                actor="investigator",
                expected_version=ver,
                payload={
                    "reason": termination_reason,
                    "action": "no_action",
                    "outcome_kind": "diagnosed",
                },
            )
            run_service.append_event(
                ctx=tenant_ctx,
                run_id=run_id,
                event_type="no_action_recorded",
                actor="investigator",
                payload={
                    "action": "no_action",
                    "reason": termination_reason,
                    "evidence_ids": diagnosis.evidence_ids,
                },
            )
            state_updates["status"] = updated_run.status
            state_updates["handoff_result"] = {
                "target_status": updated_run.status,
                "reason": termination_reason,
                "action": "no_action",
            }
        elif is_v2 and run_service and diagnosis.outcome_kind in ("unknown", "escalate"):
            # unknown / escalate -> escalated with typed reason stored in last_error/diagnosis and an event
            ver = _resolve_version()
            error_payload = {
                "code": termination_reason,
                "outcome_kind": diagnosis.outcome_kind,
                "message": f"Investigation concluded with {diagnosis.outcome_kind}: {termination_reason}",
            }
            updated_run, event = run_service.transition(
                ctx=tenant_ctx,
                run_id=run_id,
                to_status="escalated",
                actor="investigator",
                expected_version=ver,
                payload={
                    "reason": termination_reason,
                    "outcome_kind": diagnosis.outcome_kind,
                    "error": error_payload,
                },
            )
            if hasattr(run_service, "run_repo") and hasattr(run_service.run_repo, "record_error"):
                run_service.run_repo.record_error(tenant_ctx, run_id, error_payload)
            state_updates["last_error"] = error_payload
            state_updates["status"] = updated_run.status
            state_updates["handoff_result"] = {
                "target_status": updated_run.status,
                "reason": termination_reason,
            }
        else:
            effective_handoff = handoff_port or (
                DefaultHandoffStub(run_service) if run_service else None
            )
            if effective_handoff:
                merged_state: dict[str, Any] = dict(state)
                merged_state.update(state_updates)
                handoff_res = effective_handoff.propose(
                    ctx=tenant_ctx,
                    run_id=run_id,
                    state=merged_state,  # type: ignore[arg-type]
                    expected_version=expected_version,
                    diagnosis=diagnosis,
                    ledger_summary=ledger_summary,
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

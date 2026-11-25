"""agent/graph/nodes/conclude.py

LangGraph node validating the investigator model's proposed Diagnosis draft,
enforcing the deterministic Contrary-Evidence Gate and Outcome Resolver.

Responsibilities:
1. For v2 runs:
   - Evaluates evaluate_gate(...) on the hypothesis ledger, tool trace, and evidence store.
   - If gate fails:
     - Allows up to 2 retries (gate_rejection_count < 2), recording run_events and routing
       back to plan_step with typed rejection feedback.
     - On the 3rd failure (cap reached): forces an outcome of unknown with reason 'gate_not_satisfied'.
   - If gate passes:
     - Resolves outcome via resolve_outcome(...).
     - Caps confidence deterministically: confidence = min(model_proposal, cap).
     - Stores both model confidence label and capped confidence.
2. For v1 runs:
   - Validates draft against Diagnosis schema, falling back to unknown if missing/malformed.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from agent.graph.gate import (
    confidence_label_to_float,
    evaluate_gate,
    normalize_ledger,
    resolve_outcome,
)
from agent.graph.ledger import leading_hypothesis
from agent.graph.state import InvestigationState, TerminationReason
from agent.graph.steps import ConcludeStep, ConcludeStepV2
from apps.api.domain.context import TenantContext
from apps.api.services.run_service import RunService
from evals.graders.models import Diagnosis

logger = logging.getLogger("opspilot.agent.conclude")


def conclude_node(
    state: InvestigationState,
    run_service: RunService | None = None,
    evidence_store: Any | None = None,
) -> dict[str, Any]:
    """LangGraph node validating the model's diagnosis draft before finalization."""
    is_v2 = state.get("schema_version") == "v2"

    draft_raw: Any = state.get("draft_diagnosis")
    last_step = state.get("last_step_decision")

    # If draft_diagnosis is not yet in state, extract from last_step_decision
    if draft_raw is None and last_step is not None:
        if isinstance(last_step, (ConcludeStep, ConcludeStepV2)):
            draft_raw = last_step.diagnosis_draft
        elif isinstance(last_step, dict) and last_step.get("step") == "conclude":
            draft_raw = last_step.get("diagnosis_draft")

    if not is_v2:
        # V1 legacy execution flow
        return _handle_conclude_v1(state, draft_raw)

    # -------------------------------------------------------------
    # V2 Execution Flow: Contrary-Evidence Gate & Outcome Resolver
    # -------------------------------------------------------------
    # If run already terminated due to system/budget limits, conclude with unknown
    existing_term = state.get("termination_reason")
    if existing_term in (
        TerminationReason.STEP_CAP_REACHED.value,
        TerminationReason.BUDGET_EXHAUSTED.value,
        TerminationReason.INVALID_STEP_CAP_REACHED.value,
        TerminationReason.TIMEOUT_EXCEEDED.value,
        TerminationReason.TOKEN_BUDGET_EXCEEDED.value,
        TerminationReason.PROVIDER_ERROR_EXHAUSTED.value,
    ):
        forced_diag = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=list(state.get("evidence_ids", [])),
            confidence=0.0,
            model_confidence=0.0,
            capped_confidence=0.0,
            outcome_kind="unknown",
            outcome_reason=existing_term,
            gate_rejection_count=int(state.get("gate_rejection_count", 0)),
        )
        return {
            "draft_diagnosis": forced_diag.model_dump(),
            "termination_reason": existing_term,
            "status": "concluded_draft",
        }

    # If the model explicitly proposes an unknown diagnosis (abstention)
    is_unknown_draft = False
    if isinstance(draft_raw, dict) and draft_raw.get("root_cause_category") == "unknown":
        is_unknown_draft = True
    elif hasattr(draft_raw, "root_cause_category") and draft_raw.root_cause_category == "unknown":
        is_unknown_draft = True

    if is_unknown_draft:
        unknown_diag = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=list(state.get("evidence_ids", [])),
            confidence=0.0,
            model_confidence=0.0,
            capped_confidence=0.0,
            outcome_kind="unknown",
            outcome_reason=existing_term or "insufficient_evidence",
            gate_rejection_count=int(state.get("gate_rejection_count", 0)),
        )
        return {
            "draft_diagnosis": unknown_diag.model_dump(),
            "termination_reason": existing_term or "insufficient_evidence",
            "status": "concluded_draft",
        }

    ledger_data = state.get("hypotheses_ledger") or {}
    tool_trace = list(state.get("tool_trace") or [])
    alert = state.get("alert") or {}

    # Query observations from run_service if evidence_store is omitted
    resolved_store = evidence_store
    if resolved_store is None and run_service is not None:
        run_id_str = state.get("run_id")
        tenant_id_str = state.get("tenant_id")
        if run_id_str and tenant_id_str and hasattr(run_service, "run_repo") and run_service.run_repo is not None:
            try:
                ctx = TenantContext(tenant_id=UUID(tenant_id_str))
                if hasattr(run_service.run_repo, "get_evidence_list"):
                    resolved_store = run_service.run_repo.get_evidence_list(ctx, UUID(run_id_str))
                elif hasattr(run_service.run_repo, "observations"):
                    resolved_store = run_service.run_repo.observations.get(UUID(run_id_str), [])
            except Exception as exc:
                logger.debug("Could not fetch observations via run_service: %s", exc)

    # 1. Check for immediate escalation: multiple concurrent causes across distinct services
    norm_ledger = normalize_ledger(ledger_data)
    supported_services = {h.service for h in norm_ledger.hypotheses.values() if h.status == "supported" and h.service}
    if len(supported_services) >= 2:
        concurrent_diag = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=list(state.get("evidence_ids", [])),
            confidence=0.30,
            model_confidence=0.30,
            capped_confidence=0.30,
            outcome_kind="escalate",
            outcome_reason="multiple_concurrent_causes",
            gate_rejection_count=int(state.get("gate_rejection_count", 0)),
        )
        return {
            "draft_diagnosis": concurrent_diag.model_dump(),
            "termination_reason": "multiple_concurrent_causes",
            "status": "concluded_draft",
        }

    # 2. Evaluate Contrary-Evidence Gate
    gate = evaluate_gate(
        ledger=ledger_data,
        tool_trace=tool_trace,
        evidence_store=resolved_store,
        alert=alert,
    )

    if not gate.passed:
        rejection_count = state.get("gate_rejection_count", 0)
        logger.warning(
            "Contrary-Evidence Gate failed (failures=%s, previous_rejections=%d)",
            gate.failures,
            rejection_count,
        )

        if rejection_count < 2:
            new_rejection_count = rejection_count + 1
            if run_service and state.get("run_id") and state.get("tenant_id"):
                try:
                    ctx = TenantContext(tenant_id=UUID(state["tenant_id"]))
                    run_service.append_event(
                        ctx=ctx,
                        run_id=UUID(state["run_id"]),
                        event_type="gate_rejection_emitted",
                        actor="investigator",
                        payload={
                            "failures": gate.failures,
                            "details": gate.details,
                            "remaining_retries": 2 - new_rejection_count,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to emit gate_rejection_emitted event: %s", exc)

            rejection_payload = {
                "status": "rejected",
                "code": "gate_not_satisfied",
                "violations": gate.failures,
                "details": gate.details,
                "remaining_rejections": 2 - new_rejection_count,
                "directive": "Address the gate rejection violations with refutation or corroborating telemetry before concluding.",
            }
            return {
                "gate_rejection_count": new_rejection_count,
                "last_step_rejection": rejection_payload,
                "status": "gate_rejected",
            }

        # Rejection limit reached (2 retries exhausted) -> Force downgrade to unknown
        logger.warning("Gate rejection limit reached (2 retries exhausted); forcing unknown gate_not_satisfied")
        if run_service and state.get("run_id") and state.get("tenant_id"):
            try:
                ctx = TenantContext(tenant_id=UUID(state["tenant_id"]))
                run_service.append_event(
                    ctx=ctx,
                    run_id=UUID(state["run_id"]),
                    event_type="gate_rejection_cap_reached",
                    actor="investigator",
                    payload={"failures": gate.failures, "details": gate.details},
                )
            except Exception as exc:
                logger.warning("Failed to emit gate_rejection_cap_reached event: %s", exc)

        forced_diag = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=list(state.get("evidence_ids", [])),
            confidence=0.0,
            model_confidence=0.0,
            capped_confidence=0.0,
            outcome_kind="unknown",
            outcome_reason="gate_not_satisfied",
            gate_rejection_count=int(state.get("gate_rejection_count", 0)),
        )
        return {
            "draft_diagnosis": forced_diag.model_dump(),
            "termination_reason": "gate_not_satisfied",
            "status": "concluded_draft",
        }

    # 2. Gate passed: Resolve final outcome & apply confidence capping
    budget_state = {
        "remaining_calls": state.get("remaining_calls", 8),
        "tool_trace": tool_trace,
        "evidence_store": resolved_store,
    }
    tool_err = [state["last_error"]] if state.get("last_error") else None
    outcome = resolve_outcome(
        ledger=ledger_data,
        gate=gate,
        tool_errors=tool_err,
        budget_state=budget_state,
        alert=alert,
    )

    lead = leading_hypothesis(normalize_ledger(ledger_data))

    # Determine proposed model confidence
    model_conf_raw: Any = None
    if isinstance(last_step, ConcludeStepV2):
        model_conf_raw = last_step.confidence_label
        if not model_conf_raw and last_step.diagnosis_draft:
            model_conf_raw = last_step.diagnosis_draft.get("confidence")
    elif isinstance(last_step, dict):
        model_conf_raw = last_step.get("confidence_label") or (last_step.get("diagnosis_draft") or {}).get("confidence")
    if model_conf_raw is None and isinstance(draft_raw, dict):
        model_conf_raw = draft_raw.get("confidence")
    elif model_conf_raw is None and hasattr(draft_raw, "confidence"):
        model_conf_raw = draft_raw.confidence

    model_val = confidence_label_to_float(model_conf_raw) if model_conf_raw is not None else 0.90
    final_conf = min(model_val, outcome.confidence_cap)

    # Build final Diagnosis
    culprit_dep: str | None = None
    if isinstance(draft_raw, dict):
        culprit_dep = draft_raw.get("culprit_deploy_id")
    elif hasattr(draft_raw, "culprit_deploy_id"):
        culprit_dep = draft_raw.culprit_deploy_id

    if outcome.kind == "diagnosed" and lead is not None:
        if lead.category == "false_alarm":
            diagnosis = Diagnosis(
                root_cause_service=None,
                root_cause_category="false_alarm",
                mechanism="none",
                culprit_deploy_id=None,
                evidence_ids=list(lead.supporting_evidence_ids),
                confidence=round(final_conf, 2),
                model_confidence=model_conf_raw,
                capped_confidence=outcome.confidence_cap,
                outcome_kind=outcome.kind,
                outcome_reason="false_alarm_no_action",
                gate_rejection_count=int(state.get("gate_rejection_count", 0)),
            )
            term_reason = "false_alarm_no_action"
        else:
            diagnosis = Diagnosis(
                root_cause_service=lead.service,
                root_cause_category=lead.category,
                mechanism=lead.mechanism,
                culprit_deploy_id=culprit_dep,
                evidence_ids=list(lead.supporting_evidence_ids),
                confidence=round(final_conf, 2),
                model_confidence=model_conf_raw,
                capped_confidence=outcome.confidence_cap,
                outcome_kind=outcome.kind,
                outcome_reason="concluded_by_model",
                gate_rejection_count=int(state.get("gate_rejection_count", 0)),
            )
            term_reason = "concluded_by_model"
    else:
        diagnosis = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=list(state.get("evidence_ids", [])),
            confidence=round(final_conf, 2),
            model_confidence=model_conf_raw,
            capped_confidence=outcome.confidence_cap,
            outcome_kind=outcome.kind,
            outcome_reason=outcome.reason,
            gate_rejection_count=int(state.get("gate_rejection_count", 0)),
        )
        term_reason = outcome.reason

    return {
        "draft_diagnosis": diagnosis.model_dump(),
        "termination_reason": term_reason,
        "status": "concluded_draft",
    }


def _handle_conclude_v1(state: InvestigationState, draft_raw: Any) -> dict[str, Any]:
    """Legacy v1 conclude handler validating draft directly against Diagnosis model."""
    if draft_raw is not None:
        try:
            if isinstance(draft_raw, Diagnosis):
                diagnosis = draft_raw
            else:
                diagnosis = Diagnosis.model_validate(draft_raw)
            logger.info("Successfully validated v1 diagnosis draft: category=%s", diagnosis.root_cause_category)
        except Exception as exc:
            logger.warning("Diagnosis draft failed schema validation: %s; falling back to unknown", exc)
            diagnosis = Diagnosis(
                root_cause_service=None,
                root_cause_category="unknown",
                mechanism="unknown",
                culprit_deploy_id=None,
                evidence_ids=list(state.get("evidence_ids", [])),
                confidence=0.0,
            )
    else:
        logger.warning("No diagnosis draft found in state; falling back to unknown")
        diagnosis = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=list(state.get("evidence_ids", [])),
            confidence=0.0,
        )

    return {
        "draft_diagnosis": diagnosis.model_dump(),
        "status": "concluded_draft",
    }

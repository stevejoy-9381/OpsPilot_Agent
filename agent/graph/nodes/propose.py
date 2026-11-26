"""agent/graph/nodes/propose.py

LangGraph recovery proposal node for Feature 7.

Routes diagnosed (non-false-alarm) v2 runs through:
  propose_node (LLM generate_structured -> ProposalDecision)
    -> ProposalInput validation
    -> deterministic Policy Validator (agent/policy/validator.py)
    -> retry up to max_retries=2 on violation
    -> persist ActionPlan + transition to needs_review (or escalated on decline / policy_violation).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.graph.context import (
    PROMPTS_DIR,
    LeakageDetectedError,
    compute_prompt_hash,
    scan_for_leakage,
)
from agent.graph.gate import GateResult, Outcome
from agent.graph.state import InvestigationState, TerminationReason
from agent.llm.interface import BudgetExceededError, LLMClient, Message
from agent.policy.table import load_policy_table
from agent.policy.validator import PolicyContext, PolicyResult, validate
from apps.api.domain.context import TenantContext
from apps.api.services.run_service import RunService
from evals.graders.models import Diagnosis
from shared.actions import ActionType
from shared.plans import ActionPlan, PlanStatus, ProposalInput
from shared.targets import TargetState, TargetStateError, TargetStateReader, load_target_registry
from tools.common.sanitize import quarantine

logger = logging.getLogger("opspilot.agent.propose")

PROPOSAL_PROMPT_V1_PATH: Path = PROMPTS_DIR / "proposal_v1.md"
EXPECTED_PROPOSAL_V1_HASH: str = (
    "073d2e5226166cce142c1ffd7672d98a75bd974ebe587e26fb07c611296ea8b7"
)


class ProposalDecision(BaseModel):
    """Structured LLM response schema for the propose step."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["propose", "decline"] = Field(
        description="Whether to propose a recovery action or decline with a reason"
    )
    action: ActionType | None = Field(default=None, description="rollback_service or restart_service")
    service: str | None = Field(default=None, description="Target affected service")
    expected_version: str | None = Field(
        default=None, description="Current version copied verbatim from TargetState"
    )
    rationale_evidence_ids: list[str] = Field(
        default_factory=list, description="Evidence IDs supporting this proposal"
    )
    note: str = Field(default="", max_length=300, description="Short plain-text note (max 300 chars)")
    decline_reason: str = Field(
        default="", max_length=300, description="Short reason when decision='decline'"
    )


@dataclass(frozen=True)
class ProposeConfig:
    """Configuration for propose_node."""

    temperature: float = 0.0
    max_output_tokens: int = 1024
    timeout_s: float = 30.0
    max_retries: int = 2
    enforce_zero_leakage: bool = True


def load_proposal_prompt() -> str:
    """Loads proposal_v1.md and verifies its committed SHA-256 hash."""
    if not PROPOSAL_PROMPT_V1_PATH.exists():
        raise FileNotFoundError(f"Proposal prompt not found: {PROPOSAL_PROMPT_V1_PATH}")
    text = PROPOSAL_PROMPT_V1_PATH.read_text(encoding="utf-8").strip()
    schema_spec = json.dumps(
        {
            "decision": "propose|decline",
            "action": "rollback_service|restart_service|null",
            "service": "str|null",
            "expected_version": "str|null",
            "rationale_evidence_ids": "list[str]",
            "note": "str(max=300)",
            "decline_reason": "str(max=300)",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    actual_hash = compute_prompt_hash(text, schema_spec)
    if actual_hash != EXPECTED_PROPOSAL_V1_HASH:
        raise ValueError(
            f"Proposal prompt hash mismatch: expected {EXPECTED_PROPOSAL_V1_HASH}, got {actual_hash}"
        )
    return text


def build_proposal_context(
    *,
    diagnosis: Diagnosis,
    ledger_summary: dict[str, Any] | None,
    evidence_digest: list[dict[str, Any]],
    target_state: TargetState | TargetStateError | None,
    allowed_actions: tuple[ActionType, ...],
    remaining_attempts: int,
    enforce_zero_leakage: bool = True,
) -> list[Message]:
    """Assembles the system + user context for the propose step and scans for leakage."""
    system_prompt = load_proposal_prompt()

    diag_block = (
        f"Diagnosis:\n"
        f"- root_cause_category: {diagnosis.root_cause_category}\n"
        f"- mechanism: {diagnosis.mechanism}\n"
        f"- root_cause_service: {diagnosis.root_cause_service}\n"
        f"- culprit_deploy_id: {diagnosis.culprit_deploy_id}\n"
        f"- confidence: {diagnosis.confidence:.2f}\n"
        f"- cited_evidence_ids: {list(diagnosis.evidence_ids)}"
    )

    if isinstance(target_state, TargetState):
        state_block = (
            f"TargetState:\n"
            f"- target_id: {target_state.target_id}\n"
            f"- current_version: {target_state.current_version}\n"
            f"- previous_version: {target_state.previous_version}\n"
            f"- last_action_at: {target_state.last_action_at.isoformat() if target_state.last_action_at else 'none'}\n"
            f"- source: {target_state.source}\n"
            f"- read_at: {target_state.read_at.isoformat()}"
        )
    elif isinstance(target_state, TargetStateError):
        state_block = (
            f"TargetStateError:\n"
            f"- code: {target_state.code.value}\n"
            f"- target_id: {target_state.target_id}\n"
            f"- message: {target_state.message}"
        )
    else:
        state_block = "TargetState: unavailable"

    allowed_list = [a.value for a in allowed_actions]
    allowed_block = f"Allowed Actions (Policy Table): {json.dumps(allowed_list)}"
    attempts_block = f"Remaining Proposal Attempts: {remaining_attempts}"

    ev_lines: list[str] = ["Evidence Digest:"]
    for item in evidence_digest:
        eid = item.get("evidence_id", "")
        tool = item.get("tool") or item.get("source") or ""
        summary = quarantine(str(item.get("summary", "")), origin="evidence_summary").text
        ev_lines.append(f"- {eid} ({tool}): {summary}")
    evidence_block = "\n".join(ev_lines)

    ledger_json = json.dumps(ledger_summary or {}, sort_keys=True)
    ledger_block = f"Ledger Summary:\n{quarantine(ledger_json, origin='ledger_summary').text}"

    user_content = "\n\n".join(
        [
            diag_block,
            allowed_block,
            attempts_block,
            state_block,
            evidence_block,
            ledger_block,
        ]
    )

    if enforce_zero_leakage:
        prompt_leaks = scan_for_leakage(system_prompt)
        ctx_leaks = [
            leak
            for leak in scan_for_leakage(user_content)
            if leak != "forbidden_family:dependency_timeout"
        ]
        all_leaks = prompt_leaks + ctx_leaks
        if all_leaks:
            raise LeakageDetectedError(all_leaks)

    return [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_content),
    ]


def _emit_event(
    run_service: RunService | None,
    tenant_ctx: TenantContext,
    run_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if run_service is None:
        return
    try:
        run_service.append_event(
            ctx=tenant_ctx,
            run_id=run_id,
            event_type=event_type,
            actor="investigator_propose",
            payload=payload,
        )
    except Exception as exc:
        logger.warning("Failed to append %s event for run %s: %s", event_type, run_id, exc)


def _escalate_run(
    *,
    state: InvestigationState,
    run_service: RunService | None,
    tenant_ctx: TenantContext,
    run_id: UUID,
    reason: str,
    message: str,
    total_tokens: int,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_payload: dict[str, Any] = {
        "code": reason,
        "message": message,
        **(details or {}),
    }
    final_status = "escalated"
    if run_service is not None:
        try:
            ver = 1
            if hasattr(run_service, "run_repo") and hasattr(run_service.run_repo, "get_by_id"):
                ver = int(run_service.run_repo.get_by_id(tenant_ctx, run_id).version)
            updated_run, _ = run_service.transition(
                ctx=tenant_ctx,
                run_id=run_id,
                to_status="escalated",
                actor="investigator_propose",
                expected_version=ver,
                payload={"reason": reason, "error": error_payload},
            )
            if hasattr(run_service, "run_repo") and hasattr(run_service.run_repo, "record_error"):
                run_service.run_repo.record_error(tenant_ctx, run_id, error_payload)
            final_status = updated_run.status
        except Exception as exc:
            logger.error("Failed to transition run %s to escalated: %s", run_id, exc)

    return {
        "status": final_status,
        "termination_reason": reason,
        "last_error": error_payload,
        "total_tokens_consumed": total_tokens,
        "handoff_result": {"target_status": final_status, "reason": reason},
    }


def propose_node(
    state: InvestigationState,
    llm_client: LLMClient,
    run_service: RunService | None = None,
    target_reader: TargetStateReader | None = None,
    plan_persister: Callable[[TenantContext, ActionPlan, str | None], Any] | None = None,
    db_clock_fn: Callable[[], datetime] | None = None,
    config: ProposeConfig | None = None,
) -> dict[str, Any]:
    """LangGraph node that proposes, validates, and persists an ActionPlan."""
    cfg = config or ProposeConfig()
    run_id_raw = state.get("run_id")
    tenant_id_raw = state.get("tenant_id")
    if not run_id_raw or not tenant_id_raw:
        raise ValueError("InvestigationState missing run_id or tenant_id")

    run_id = UUID(str(run_id_raw))
    tenant_id = UUID(str(tenant_id_raw))
    tenant_ctx = TenantContext(tenant_id=tenant_id)
    total_tokens = int(state.get("total_tokens_consumed", 0))

    draft_raw = state.get("draft_diagnosis")
    if not draft_raw:
        return _escalate_run(
            state=state,
            run_service=run_service,
            tenant_ctx=tenant_ctx,
            run_id=run_id,
            reason=TerminationReason.PROPOSAL_POLICY_VIOLATION.value,
            message="Cannot propose recovery without a persisted diagnosis.",
            total_tokens=total_tokens,
        )

    diagnosis = Diagnosis(**draft_raw)
    registry = load_target_registry()
    policy_table = load_policy_table()
    allowed_actions = (
        policy_table.allowed_actions_for(diagnosis.root_cause_category, diagnosis.mechanism) or ()
    )

    # Resolve TargetState for diagnosed service
    target_state: TargetState | TargetStateError | None = None
    diag_svc = (diagnosis.root_cause_service or "").strip().lower()
    spec = registry.get_by_service(diag_svc) if diag_svc else None
    w_start_raw = state.get("window_start")
    w_end_raw = state.get("window_end")
    if w_start_raw and w_end_raw:
        w_start = datetime.fromisoformat(str(w_start_raw).replace("Z", "+00:00"))
        w_end = datetime.fromisoformat(str(w_end_raw).replace("Z", "+00:00"))
    else:
        alert_raw = state.get("alert") or {}
        fired_raw = (
            alert_raw.get("fired_at")
            if isinstance(alert_raw, dict)
            else getattr(alert_raw, "fired_at", None)
        )
        if fired_raw:
            fired_dt = (
                datetime.fromisoformat(str(fired_raw).replace("Z", "+00:00"))
                if isinstance(fired_raw, str)
                else fired_raw
            )
            if fired_dt.tzinfo is None:
                fired_dt = fired_dt.replace(tzinfo=timezone.utc)
            w_start = fired_dt - timedelta(minutes=15)
            w_end = fired_dt + timedelta(minutes=10)
        else:
            now_fallback = datetime.now(timezone.utc)
            w_start = now_fallback - timedelta(minutes=15)
            w_end = now_fallback + timedelta(minutes=10)

    if target_reader is not None and spec is not None:
        target_state = target_reader.current_state(spec.target_id, w_end)

    # Gather evidence digest & items
    evidence_digest: list[dict[str, Any]] = []
    evidence_items: list[dict[str, Any]] = []
    if run_service is not None and hasattr(run_service, "run_repo"):
        if hasattr(run_service.run_repo, "list_observations"):
            obs_list = run_service.run_repo.list_observations(tenant_ctx, run_id)
            for obs in obs_list:
                evidence_digest.append(
                    {
                        "evidence_id": obs.evidence_id,
                        "tool": obs.tool,
                        "summary": getattr(obs, "summary", ""),
                    }
                )
                evidence_items.append(
                    {
                        "evidence_id": obs.evidence_id,
                        "run_id": obs.run_id,
                        "source": obs.tool,
                        "window_start": obs.window_start,
                        "window_end": obs.window_end,
                        "data": getattr(obs, "payload", {}),
                    }
                )
    if not evidence_digest:
        for eid in state.get("evidence_ids") or []:
            evidence_digest.append({"evidence_id": eid, "tool": "search_logs", "summary": ""})
            evidence_items.append(
                {
                    "evidence_id": eid,
                    "run_id": str(run_id),
                    "source": "search_logs",
                    "timestamp": w_end,
                    "data": {},
                }
            )

    ledger_summary = diagnosis.ledger_summary
    max_attempts = cfg.max_retries + 1
    clock_fn = db_clock_fn or (lambda: datetime.now(timezone.utc))

    messages = build_proposal_context(
        diagnosis=diagnosis,
        ledger_summary=ledger_summary,
        evidence_digest=evidence_digest,
        target_state=target_state,
        allowed_actions=allowed_actions,
        remaining_attempts=max_attempts,
        enforce_zero_leakage=cfg.enforce_zero_leakage,
    )

    last_violations: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            result = llm_client.generate_structured(
                messages=messages,
                response_schema=ProposalDecision,
                max_output_tokens=cfg.max_output_tokens,
                timeout_s=cfg.timeout_s,
            )
        except BudgetExceededError as budget_err:
            return _escalate_run(
                state=state,
                run_service=run_service,
                tenant_ctx=tenant_ctx,
                run_id=run_id,
                reason=TerminationReason.TOKEN_BUDGET_EXCEEDED.value,
                message=f"Token budget exceeded during proposal: {budget_err}",
                total_tokens=total_tokens,
            )

        total_tokens += result.token_usage.total_tokens

        if not result.is_success or result.parsed is None:
            err_code = result.error.error_type if result.error else "provider_error"
            err_msg = result.error.message if result.error else "LLM proposal generation failed"
            mapped_reason = (
                TerminationReason.INVALID_STEP_CAP_REACHED.value
                if err_code == "schema_validation_failed"
                else TerminationReason.PROVIDER_ERROR_EXHAUSTED.value
            )
            return _escalate_run(
                state=state,
                run_service=run_service,
                tenant_ctx=tenant_ctx,
                run_id=run_id,
                reason=mapped_reason,
                message=err_msg,
                total_tokens=total_tokens,
                details={"provider_error_code": err_code},
            )

        decision: ProposalDecision = (
            result.parsed
            if isinstance(result.parsed, ProposalDecision)
            else ProposalDecision(**result.parsed)
        )

        # Decline path: never auto-create a plan
        if decision.decision == "decline":
            _emit_event(
                run_service,
                tenant_ctx,
                run_id,
                "proposal_attempt",
                {
                    "type": "proposal_attempt",
                    "attempt": attempt,
                    "allowed": False,
                    "declined": True,
                    "violation_codes": ["model_declined"],
                },
            )
            return _escalate_run(
                state=state,
                run_service=run_service,
                tenant_ctx=tenant_ctx,
                run_id=run_id,
                reason=TerminationReason.MODEL_DECLINED.value,
                message="Model declined to propose a recovery action.",
                total_tokens=total_tokens,
            )

        # Validate ProposalInput schema
        try:
            proposal_input = ProposalInput(
                action=decision.action,  # type: ignore[arg-type]
                service=decision.service or "",
                expected_version=decision.expected_version or "",
                rationale_evidence_ids=decision.rationale_evidence_ids,
                note=decision.note,
            )
        except ValidationError as val_err:
            last_violations = ["invalid_proposal_schema"]
            _emit_event(
                run_service,
                tenant_ctx,
                run_id,
                "proposal_attempt",
                {
                    "type": "proposal_attempt",
                    "attempt": attempt,
                    "allowed": False,
                    "violation_codes": last_violations,
                },
            )
            if attempt < max_attempts:
                messages.append(
                    Message(
                        role="user",
                        content=(
                            f"Proposal schema rejected [invalid_proposal_schema]: {val_err}. "
                            f"Remaining attempts: {max_attempts - attempt}."
                        ),
                    )
                )
                continue
            break

        # Deterministic Policy Validator
        policy_ctx = PolicyContext(
            tenant_id=tenant_id,
            run_id=run_id,
            diagnosis=diagnosis,
            outcome=Outcome(
                kind="diagnosed",
                reason=diagnosis.outcome_reason or "concluded_by_model",
                confidence_cap=float(diagnosis.capped_confidence or diagnosis.confidence),
                confidence_label="high",
            ),
            gate_result=GateResult(passed=True),
            ledger_summary=ledger_summary,
            window_start=w_start,
            window_end=w_end,
            as_of=w_end,
            evidence_items=evidence_items,
            target_reader=target_reader,
            db_clock_fn=clock_fn,
            target_registry=registry,
            policy_table=policy_table,
        )
        policy_res: PolicyResult = validate(proposal_input, policy_ctx)
        violation_codes = [v.code for v in policy_res.violations]

        _emit_event(
            run_service,
            tenant_ctx,
            run_id,
            "proposal_attempt",
            {
                "type": "proposal_attempt",
                "attempt": attempt,
                "allowed": policy_res.allowed,
                "violation_codes": violation_codes,
                "policy_version": policy_res.policy_version,
            },
        )

        if policy_res.allowed and policy_res.derived is not None:
            derived = policy_res.derived
            now_utc = clock_fn()
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            plan = ActionPlan(
                plan_id=f"plan_{uuid.uuid4().hex[:12]}",
                tenant_id=str(tenant_id),
                run_id=str(run_id),
                action=proposal_input.action,
                service=proposal_input.service,
                target_id=derived.target_id,
                expected_version=proposal_input.expected_version,
                rollback_to_version=derived.rollback_to_version,
                rollback_route=derived.rollback_route,
                rationale_evidence_ids=proposal_input.rationale_evidence_ids,
                note=proposal_input.note,
                status=PlanStatus.PROPOSED,
                created_at=now_utc,
                expires_at=derived.expires_at,
                plan_hash=derived.plan_hash,
            )

            if plan_persister is not None:
                try:
                    plan_persister(tenant_ctx, plan, derived.supersedes_plan_id)
                except Exception as persist_err:
                    return _escalate_run(
                        state=state,
                        run_service=run_service,
                        tenant_ctx=tenant_ctx,
                        run_id=run_id,
                        reason="persistence_failed",
                        message=f"Failed to persist action plan atomically: {persist_err}",
                        total_tokens=total_tokens,
                    )

            final_status = "needs_review"
            if run_service is not None:
                try:
                    ver = 1
                    if hasattr(run_service, "run_repo") and hasattr(run_service.run_repo, "get_by_id"):
                        ver = int(run_service.run_repo.get_by_id(tenant_ctx, run_id).version)
                    updated_run, _ = run_service.transition(
                        ctx=tenant_ctx,
                        run_id=run_id,
                        to_status="needs_review",
                        actor="investigator_propose",
                        expected_version=ver,
                        payload={
                            "plan_id": plan.plan_id,
                            "plan_hash": plan.plan_hash,
                            "policy_version": policy_res.policy_version,
                        },
                    )
                    final_status = updated_run.status
                except Exception as trans_err:
                    return _escalate_run(
                        state=state,
                        run_service=run_service,
                        tenant_ctx=tenant_ctx,
                        run_id=run_id,
                        reason="concurrent_modification",
                        message=f"Failed to transition run to needs_review: {trans_err}",
                        total_tokens=total_tokens,
                    )

            return {
                "status": final_status,
                "termination_reason": TerminationReason.CONCLUDED_BY_MODEL.value,
                "total_tokens_consumed": total_tokens,
                "proposed_plan_id": plan.plan_id,
                "proposed_plan_hash": plan.plan_hash,
                "proposed_action": plan.action.value,
                "proposed_service": plan.service,
                "proposed_target_id": plan.target_id,
                "proposed_expected_version": plan.expected_version,
                "proposed_rollback_to_version": plan.rollback_to_version,
                "proposed_expires_at": plan.expires_at.isoformat(),
                "handoff_result": {
                    "target_status": final_status,
                    "plan_id": plan.plan_id,
                    "plan_hash": plan.plan_hash,
                    "policy_version": policy_res.policy_version,
                },
            }

        # Rejected by Policy Validator
        last_violations = violation_codes
        if attempt < max_attempts:
            violation_text = "; ".join(
                f"[{v.code}] on '{v.field}': {v.hint}" for v in policy_res.violations
            )
            messages.append(
                Message(
                    role="user",
                    content=(
                        f"Your proposal was rejected by the policy validator: {violation_text}. "
                        f"Remaining attempts: {max_attempts - attempt}. Please correct and resubmit, or decline."
                    ),
                )
            )

    return _escalate_run(
        state=state,
        run_service=run_service,
        tenant_ctx=tenant_ctx,
        run_id=run_id,
        reason=TerminationReason.PROPOSAL_POLICY_VIOLATION.value,
        message=f"Proposal rejected after {max_attempts} attempts: {last_violations}",
        total_tokens=total_tokens,
        details={"violation_codes": last_violations},
    )

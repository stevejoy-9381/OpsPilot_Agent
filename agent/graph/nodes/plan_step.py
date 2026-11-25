"""agent/graph/nodes/plan_step.py

LangGraph planning node for the OpsPilot Incident Investigator Agent.

DESIGN DECISION (Investigator Step Generation Strategy):
--------------------------------------------------------
We use `LLMClient.generate_structured` exclusively with the `AgentStep` discriminated
union schema (or `ConcludeStep` when tool budget is exhausted).
Why:
1. Polymorphic Output: OpsPilot investigation turns require a polymorphic decision:
   either invoking an authorized read-only investigation tool (`CallToolStep`) or
   emitting a structured domain diagnosis artifact (`ConcludeStep` conforming to
   the Feature 1.12 Diagnosis schema).
2. Schema Rigor & Guardrails: `generate_structured` leverages constrained decoding
   and strict Pydantic validation (rejecting forbidden extra fields and invalid tools).
   In contrast, native provider tool calling (`generate_with_tools`) is engineered
   solely for function dispatch; it requires synthetic/dummy tools or unstructured text
   to conclude an investigation, adding schema translation complexity and error surface.
3. Repair Loop: Single-turn repair prompts can pass typed validation errors directly
   back to `generate_structured` for schema correction.
The `generate_with_tools` code path is deliberately omitted from this node.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent.graph.context import build_investigator_context
from agent.graph.state import (
    InvestigationState,
    TerminationReason,
    get_final_status_for_termination,
)
from agent.graph.steps import (
    AgentStep,
    CallToolStep,
    CallToolStepV2,
    ConcludeStep,
    ConcludeStepV2,
    FlatAgentStepV2,
    StepRejection,
    StepValidator,
    UpdateHypothesesStepV2,
    parse_agent_step,
    parse_agent_step_v2,
)
from agent.llm.interface import (
    BudgetExceededError,
    LLMClient,
    Message,
    StructuredResult,
)
from apps.api.domain.context import TenantContext
from apps.api.services.run_service import RunService

logger = logging.getLogger("opspilot.agent.plan_step")


@dataclass(frozen=True)
class PlanStepConfig:
    """Configuration parameters for the planning node."""

    temperature: float = 0.0
    max_output_tokens: int = 1024
    timeout_s: float = 30.0
    max_invalid_steps: int = 3
    enforce_zero_leakage: bool = True
    schema_version: str | None = None


def _format_step_summary(step_count: int, step: Any) -> str:
    """Formats a concise one-line audit summary of an agent step."""
    if isinstance(step, (CallToolStep, CallToolStepV2)):
        tool_name = step.tool
        return f"Turn {step_count}: call_tool({tool_name}) - {step.reason}"
    if isinstance(step, UpdateHypothesesStepV2):
        ops_desc = f"{len(step.operations)} ops"
        return f"Turn {step_count}: update_hypotheses({ops_desc}) - {step.reason}"
    return f"Turn {step_count}: conclude - {step.reason}"


def _append_to_summary(current_summary: str, line: str) -> str:
    """Appends a new turn summary line preserving chronological order."""
    if not current_summary.strip():
        return line
    return f"{current_summary.strip()}\n{line}"


def plan_step_node(
    state: InvestigationState,
    llm_client: LLMClient,
    run_service: RunService | None = None,
    config: PlanStepConfig | None = None,
) -> dict[str, Any]:
    """LangGraph node executing a single investigation planning turn.

    Pure with respect to the DB except writing audit events through RunService.

    Flow:
    1. Guard check: if remaining_calls <= 0, route straight to forced conclusion
       using ConcludeStep schema and a concise budget-exhausted prompt variant.
    2. Otherwise, assemble full investigation context and prompt with AgentStep.
    3. Invoke LLMClient.generate_structured under token budget guard.
    4. Handle outputs:
       - Success -> record step, emit run_event, update state.
       - Schema validation failure -> 1 repair attempt. A second failure counts
         as an invalid step. If invalid_step_count reaches cap (3), terminate.
       - Provider error or timeout -> terminate with typed failure (never a diagnosis).
       - Token budget exceeded -> terminate with token_budget_exceeded.
    """
    cfg = config or PlanStepConfig()
    schema_ver = cfg.schema_version or state.get("schema_version", "v1")
    is_v2 = schema_ver == "v2"
    remaining_calls = state.get("remaining_calls", 8)
    is_forced_conclude = remaining_calls <= 0

    # 1. Select schema and assemble context
    if is_v2:
        response_schema: Any = ConcludeStepV2 if is_forced_conclude else FlatAgentStepV2
    else:
        response_schema = ConcludeStep if is_forced_conclude else AgentStep

    try:
        assembled = build_investigator_context(
            state,
            enforce_zero_leakage=cfg.enforce_zero_leakage,
            forced_conclude=is_forced_conclude,
        )
    except Exception as exc:
        logger.error("Failed to build investigator context: %s", exc)
        return {
            "status": "escalated",
            "termination_reason": TerminationReason.INVALID_STEP_CAP_REACHED.value,
            "last_error": {"code": "context_assembly_failed", "message": str(exc)},
        }

    # 2. Invoke LLM through LLMClient with TokenBudgetGuard handling
    try:
        result: StructuredResult[Any] = llm_client.generate_structured(
            messages=assembled.messages,
            response_schema=response_schema,
            max_output_tokens=cfg.max_output_tokens,
            timeout_s=cfg.timeout_s,
        )
    except BudgetExceededError as budget_err:
        return _handle_token_budget_exceeded(state, budget_err, run_service)

    tokens_consumed_this_turn = result.token_usage.total_tokens

    # 3. Provider error or timeout -> terminate with typed provider failure
    if result.error and result.error.error_type in ("provider_error", "timeout"):
        return _handle_provider_failure(
            state=state,
            error=result.error,
            run_service=run_service,
            tokens_consumed=tokens_consumed_this_turn,
        )

    # 4. Schema failure -> single repair attempt
    if not result.is_success:
        err_msg = result.error.message if result.error else "Schema validation failed"
        logger.warning("Initial plan_step generation failed schema validation: %s", err_msg)

        repair_messages = list(assembled.messages)
        if result.raw_text:
            repair_messages.append(Message(role="model", content=result.raw_text))
        repair_messages.append(
            Message(
                role="user",
                content=(
                    f"Your previous response failed schema validation: {err_msg}. "
                    "Please correct your response and output valid JSON conforming strictly to the required schema."
                ),
            )
        )

        try:
            repair_result: StructuredResult[Any] = llm_client.generate_structured(
                messages=repair_messages,
                response_schema=response_schema,
                max_output_tokens=cfg.max_output_tokens,
                timeout_s=cfg.timeout_s,
            )
        except BudgetExceededError as budget_err:
            return _handle_token_budget_exceeded(state, budget_err, run_service)

        tokens_consumed_this_turn += repair_result.token_usage.total_tokens

        if repair_result.is_success:
            # Repair succeeded!
            logger.info("Plan step schema repair attempt succeeded.")
            result = repair_result
        else:
            # Repair failed: counts as an invalid step
            logger.warning("Plan step schema repair attempt also failed.")
            return _handle_invalid_step(
                state=state,
                error_message=repair_result.error.message if repair_result.error else err_msg,
                cfg=cfg,
                run_service=run_service,
                tokens_consumed=tokens_consumed_this_turn,
            )

    # 5. Parse and semantically validate step
    step = result.parsed
    try:
        if isinstance(step, FlatAgentStepV2):
            step = parse_agent_step_v2(step.model_dump())
        elif isinstance(step, dict):
            step = parse_agent_step(step, version="v2" if is_v2 else "v1")
    except Exception as parse_exc:
        return _handle_invalid_step(
            state=state,
            error_message=f"Step validation failed: {parse_exc}",
            cfg=cfg,
            run_service=run_service,
            tokens_consumed=tokens_consumed_this_turn,
        )

    if not isinstance(
        step,
        (CallToolStep, ConcludeStep, CallToolStepV2, UpdateHypothesesStepV2, ConcludeStepV2),
    ):
        return _handle_invalid_step(
            state=state,
            error_message="Parsed result did not conform to recognized agent step",
            cfg=cfg,
            run_service=run_service,
            tokens_consumed=tokens_consumed_this_turn,
        )

    # 6. Feature 6 StepValidator check
    rejection = StepValidator.validate_step(step, state=state)
    if rejection is not None:
        logger.warning(
            "Step failed semantic validation: [%s] on %s: %s",
            rejection.code,
            rejection.field,
            rejection.hint,
        )
        repair_messages = list(assembled.messages)
        if result.raw_text:
            repair_messages.append(Message(role="model", content=result.raw_text))
        repair_messages.append(
            Message(
                role="user",
                content=(
                    f"Your proposed step was rejected: [{rejection.code}] on field '{rejection.field}': {rejection.hint}. "
                    "Please correct the step and adhere strictly to the empirical ledger rules."
                ),
            )
        )
        try:
            repair_result = llm_client.generate_structured(
                messages=repair_messages,
                response_schema=response_schema,
                max_output_tokens=cfg.max_output_tokens,
                timeout_s=cfg.timeout_s,
            )
        except BudgetExceededError as budget_err:
            return _handle_token_budget_exceeded(state, budget_err, run_service)

        tokens_consumed_this_turn += repair_result.token_usage.total_tokens

        if repair_result.is_success:
            repaired_step = repair_result.parsed
            if isinstance(repaired_step, FlatAgentStepV2):
                repaired_step = parse_agent_step_v2(repaired_step.model_dump())
            elif isinstance(repaired_step, dict):
                repaired_step = parse_agent_step(repaired_step, version="v2" if is_v2 else "v1")

            re_rejection = StepValidator.validate_step(repaired_step, state=state)
            if re_rejection is None and isinstance(
                repaired_step,
                (CallToolStep, ConcludeStep, CallToolStepV2, UpdateHypothesesStepV2, ConcludeStepV2),
            ):
                logger.info("Plan step semantic repair attempt succeeded.")
                step = repaired_step
                result = repair_result
            else:
                return _handle_invalid_step(
                    state=state,
                    error_message=f"Step validation failed: [{re_rejection.code if re_rejection else 'invalid_step'}]",
                    cfg=cfg,
                    run_service=run_service,
                    tokens_consumed=tokens_consumed_this_turn,
                    rejection=re_rejection or rejection,
                )
        else:
            return _handle_invalid_step(
                state=state,
                error_message=repair_result.error.message if repair_result.error else "Repair generation failed",
                cfg=cfg,
                run_service=run_service,
                tokens_consumed=tokens_consumed_this_turn,
                rejection=rejection,
            )

    return _handle_valid_step(
        state=state,
        step=step,
        result=result,
        cfg=cfg,
        run_service=run_service,
        tokens_consumed=tokens_consumed_this_turn,
    )


def _handle_valid_step(
    state: InvestigationState,
    step: Any,
    result: StructuredResult[Any],
    cfg: PlanStepConfig,
    run_service: RunService | None,
    tokens_consumed: int,
) -> dict[str, Any]:
    """Processes and persists a valid step decision."""
    current_step_count = state.get("step_count", 0) + 1
    total_tokens = state.get("total_tokens_consumed", 0) + tokens_consumed
    summary_line = _format_step_summary(current_step_count, step)
    updated_summary = _append_to_summary(state.get("messages_summary", ""), summary_line)

    step_kind = step.step
    tool_name = getattr(step, "tool", None)

    # Emit audit event through RunService if configured (excluding raw args)
    _emit_run_event(
        state=state,
        run_service=run_service,
        event_type="agent_step",
        payload={
            "step_kind": step_kind,
            "tool": tool_name,
            "reason": step.reason,
            "temperature": cfg.temperature,
            "tokens": result.token_usage.model_dump(),
            "total_tokens": total_tokens,
        },
    )

    updates: dict[str, Any] = {
        "step_count": current_step_count,
        "last_step_decision": step.model_dump(),
        "messages_summary": updated_summary,
        "total_tokens_consumed": total_tokens,
    }

    if isinstance(step, (ConcludeStep, ConcludeStepV2)):
        diagnosis_dict = step.diagnosis_draft.model_dump()
        updates["draft_diagnosis"] = diagnosis_dict
        updates["status"] = "needs_review"
        updates["termination_reason"] = TerminationReason.CONCLUDED_BY_MODEL.value

    # Budget interaction: check step cap (MAX_STEPS = 2 * MAX_TOOL_CALLS + 4)
    max_calls = state.get("max_tool_calls") or 8
    max_steps = state.get("max_steps") or (2 * max_calls + 4)
    if current_step_count >= max_steps:
        logger.info("Reached maximum step budget (%d steps); terminating", max_steps)
        updates["termination_reason"] = TerminationReason.STEP_CAP_REACHED.value
        if "draft_diagnosis" not in updates and not state.get("draft_diagnosis"):
            updates["status"] = "escalated"

    return updates


def _handle_invalid_step(
    state: InvestigationState,
    error_message: str,
    cfg: PlanStepConfig,
    run_service: RunService | None,
    tokens_consumed: int,
    rejection: StepRejection | None = None,
) -> dict[str, Any]:
    """Handles an invalid step occurrence and terminates if cap reached."""
    new_invalid_count = state.get("invalid_step_count", 0) + 1
    total_tokens = state.get("total_tokens_consumed", 0) + tokens_consumed

    code = rejection.code if rejection else "schema_validation_failed"
    error_payload: dict[str, Any] = {
        "code": code,
        "message": error_message,
        "invalid_step_count": new_invalid_count,
        "max_invalid_steps": cfg.max_invalid_steps,
    }
    if rejection:
        error_payload["field"] = rejection.field
        error_payload["hint"] = rejection.hint

    if new_invalid_count >= cfg.max_invalid_steps:
        termination_reason = TerminationReason.INVALID_STEP_CAP_REACHED
        final_status = get_final_status_for_termination(termination_reason)

        _emit_run_event(
            state=state,
            run_service=run_service,
            event_type="invalid_steps_escalated",
            payload={
                "error": error_payload,
                "invalid_step_count": new_invalid_count,
                "status": final_status,
            },
        )

        return {
            "invalid_step_count": new_invalid_count,
            "total_tokens_consumed": total_tokens,
            "last_error": error_payload,
            "termination_reason": termination_reason.value,
            "status": final_status,
        }

    return {
        "invalid_step_count": new_invalid_count,
        "total_tokens_consumed": total_tokens,
        "last_error": error_payload,
    }


def _handle_provider_failure(
    state: InvestigationState,
    error: Any,
    run_service: RunService | None,
    tokens_consumed: int,
) -> dict[str, Any]:
    """Handles persistent LLM provider failure or timeout (never converts to diagnosis)."""
    termination_reason = TerminationReason.PROVIDER_ERROR_EXHAUSTED
    final_status = get_final_status_for_termination(termination_reason)
    total_tokens = state.get("total_tokens_consumed", 0) + tokens_consumed

    error_payload = {
        "code": getattr(error, "error_type", "provider_error"),
        "message": getattr(error, "message", "LLM provider error"),
        "details": getattr(error, "details", {}),
    }

    _emit_run_event(
        state=state,
        run_service=run_service,
        event_type="provider_error_escalated",
        payload={"error": error_payload, "status": final_status},
    )

    return {
        "status": final_status,
        "termination_reason": termination_reason.value,
        "last_error": error_payload,
        "total_tokens_consumed": total_tokens,
    }


def _handle_token_budget_exceeded(
    state: InvestigationState,
    budget_err: BudgetExceededError,
    run_service: RunService | None,
) -> dict[str, Any]:
    """Handles token budget exhaustion raised by TokenBudgetGuard."""
    termination_reason = TerminationReason.TOKEN_BUDGET_EXCEEDED
    final_status = get_final_status_for_termination(termination_reason)

    error_payload = {
        "code": "token_budget_exceeded",
        "message": str(budget_err),
        "current_usage": budget_err.current_usage,
        "limit": budget_err.limit,
    }

    _emit_run_event(
        state=state,
        run_service=run_service,
        event_type="token_budget_escalated",
        payload={"error": error_payload, "status": final_status},
    )

    return {
        "status": final_status,
        "termination_reason": termination_reason.value,
        "last_error": error_payload,
        "total_tokens_consumed": budget_err.current_usage,
    }


def _emit_run_event(
    state: InvestigationState,
    run_service: RunService | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Helper to safely emit audit events via RunService if provided."""
    if run_service is None:
        return

    run_id_str = state.get("run_id")
    tenant_id_str = state.get("tenant_id")
    if not run_id_str or not tenant_id_str:
        return

    try:
        run_uuid = UUID(run_id_str)
        tenant_ctx = TenantContext(tenant_id=UUID(tenant_id_str))
        run_service.append_event(
            ctx=tenant_ctx,
            run_id=run_uuid,
            event_type=event_type,
            actor="investigator",
            payload=payload,
        )
    except Exception as exc:
        logger.warning("Failed to emit audit event %s through RunService: %s", event_type, exc)

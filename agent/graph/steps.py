"""agent/graph/steps.py

Pydantic step schemas and deterministic pre-execution validation for incident investigation.

Supports:
- Version 1: CallToolStep, ConcludeStep, AgentStep (preserved for backward compatibility).
- Version 2 (Feature 6):
    * CallToolStepV2: Adds tests_hypothesis_id, expected_if_true, expected_if_false (max 160 chars).
    * UpdateHypothesesStepV2: Structured operations (add, link_evidence, set_status; max 6 ops).
    * ConcludeStepV2: diagnosis_draft, leading_hypothesis_id, reason.
    * FlatAgentStepV2: Single flat schema for reliable Gemini SDK structured output.
    * StepValidator: Deterministic validator returning typed StepRejection before execution.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from agent.graph.ledger import (
    MAX_HYPOTHESES_PER_RUN,
    HypothesisLedger,
)
from evals.graders.evidence import grade_evidence_support
from evals.graders.models import Diagnosis, DiagnosisCategory, DiagnosisMechanism

# Closed allowlist of read-only investigation tools conforming to Feature 2 tool contract
ALLOWED_INVESTIGATION_TOOLS: tuple[str, ...] = (
    "get_metrics",
    "search_logs",
    "get_trace",
    "list_deploys",
    "get_deploy_diff",
)

ConfidenceLabel = Literal["low", "medium", "high"]
CONFIDENCE_LABEL_MAP: dict[ConfidenceLabel, float] = {
    "low": 0.30,
    "medium": 0.60,
    "high": 0.90,
}


def validate_step_reason(reason: str) -> str:
    """Validates that reason is concise, plain-text operational rationale (max 300 chars)."""
    cleaned = reason.strip()
    if not cleaned:
        raise ValueError("Step reason cannot be empty or whitespace only")

    if len(cleaned) > 300:
        raise ValueError(
            f"Step reason exceeds 300 characters: {len(cleaned)} chars provided"
        )

    for ch in cleaned:
        if unicodedata.category(ch).startswith("C") and ch not in ("\n", "\t"):
            raise ValueError(
                "Step reason must be plain text without control characters or null bytes"
            )

    return cleaned


def validate_short_expectation(text: str | None, field_name: str) -> str | None:
    """Validates that expected_if_true/false is concise, plain-text (max 160 chars)."""
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) > 160:
        raise ValueError(
            f"{field_name} exceeds 160 characters: {len(cleaned)} chars provided"
        )
    for ch in cleaned:
        if unicodedata.category(ch).startswith("C") and ch not in ("\n", "\t"):
            raise ValueError(
                f"{field_name} must be plain text without control characters or null bytes"
            )
    return cleaned


# ===========================================================================
# V1 Step Schemas (Preserved for backwards compatibility)
# ===========================================================================

class CallToolStep(BaseModel):
    """Model decision to invoke an authorized investigation tool (v1)."""

    model_config = ConfigDict(extra="forbid")

    step: Literal["call_tool"] = "call_tool"
    tool: str = Field(description="Authorized investigation tool to invoke")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments to be validated against tool schema at dispatch",
    )
    reason: str = Field(
        description="Short operational rationale for this step (max 300 chars; NO chain-of-thought)",
    )

    @field_validator("tool")
    @classmethod
    def validate_tool_name(cls, v: str) -> str:
        if v not in ALLOWED_INVESTIGATION_TOOLS:
            allowed_list = sorted(ALLOWED_INVESTIGATION_TOOLS)
            raise ValueError(
                f"Unknown tool '{v}'. Allowed investigation tools: {allowed_list}"
            )
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        return validate_step_reason(v)

    def validate_args_against_tool_schema(self) -> BaseModel:
        """Validates args against the registered tool's Pydantic schema in the tool registry."""
        from tools.registry import registry

        tool_entry = registry._tools.get(self.tool)
        if tool_entry is None:
            raise ValueError(f"Unknown tool '{self.tool}' not found in registry")
        input_model, _ = tool_entry
        return input_model.model_validate(self.args)


class ConcludeStep(BaseModel):
    """Model decision to conclude the investigation with a diagnosis artifact (v1)."""

    model_config = ConfigDict(extra="forbid")

    step: Literal["conclude"] = "conclude"
    diagnosis_draft: Diagnosis = Field(
        description="Structured diagnosis artifact conforming to Feature 1.12 Diagnosis schema",
    )
    reason: str = Field(
        description="Short summary rationale explaining the diagnostic conclusion (max 300 chars)",
    )

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        return validate_step_reason(v)


AgentStep = Annotated[
    Union[CallToolStep, ConcludeStep],
    Field(discriminator="step"),
]

_AGENT_STEP_V1_ADAPTER: TypeAdapter[CallToolStep | ConcludeStep] = TypeAdapter(AgentStep)


# ===========================================================================
# V2 Step Schemas (Feature 6: Hypothesis Tracking & Falsification)
# ===========================================================================

class CallToolStepV2(BaseModel):
    """Model decision to invoke an authorized investigation tool with falsification expectations."""

    model_config = ConfigDict(extra="forbid")

    step: Literal["call_tool"] = "call_tool"
    tool: str = Field(description="Authorized investigation tool to invoke")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments to be validated against tool schema at dispatch",
    )
    tests_hypothesis_id: str | None = Field(
        default=None,
        description="Hypothesis ID on the ledger this call aims to test or discriminate against rivals",
    )
    expected_if_true: str | None = Field(
        default=None,
        description="Expected observation pattern if tested hypothesis holds (max 160 chars)",
    )
    expected_if_false: str | None = Field(
        default=None,
        description="Expected observation pattern if tested hypothesis is false/rival holds (max 160 chars)",
    )
    reason: str = Field(
        description="Short operational rationale for this step (max 300 chars; NO chain-of-thought)",
    )

    @field_validator("tool")
    @classmethod
    def validate_tool_name(cls, v: str) -> str:
        if v not in ALLOWED_INVESTIGATION_TOOLS:
            allowed_list = sorted(ALLOWED_INVESTIGATION_TOOLS)
            raise ValueError(
                f"Unknown tool '{v}'. Allowed investigation tools: {allowed_list}"
            )
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        return validate_step_reason(v)

    @field_validator("expected_if_true")
    @classmethod
    def validate_exp_true(cls, v: str | None) -> str | None:
        return validate_short_expectation(v, "expected_if_true")

    @field_validator("expected_if_false")
    @classmethod
    def validate_exp_false(cls, v: str | None) -> str | None:
        return validate_short_expectation(v, "expected_if_false")

    def validate_args_against_tool_schema(self) -> BaseModel:
        from tools.registry import registry

        tool_entry = registry._tools.get(self.tool)
        if tool_entry is None:
            raise ValueError(f"Unknown tool '{self.tool}' not found in registry")
        input_model, _ = tool_entry
        return input_model.model_validate(self.args)


class AddHypothesisOp(BaseModel):
    """Operation adding a new candidate hypothesis to the ledger."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add"] = "add"
    statement: str = Field(description="Proposition statement (max 200 chars)")
    category: DiagnosisCategory = Field(description="Root cause category")
    mechanism: DiagnosisMechanism = Field(description="Concrete failure mechanism")
    service: str | None = Field(default=None, description="Suspected microservice or None")
    confidence_label: ConfidenceLabel = Field(
        default="medium",
        description="Proposed confidence level: 'low', 'medium', or 'high' (clamped by cap table)",
    )
    confidence: float | None = Field(
        default=None,
        description="Numerical confidence is forbidden; rejected by StepValidator if present",
    )


class LinkEvidenceOp(BaseModel):
    """Operation linking an observation evidence ID to a hypothesis."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["link_evidence"] = "link_evidence"
    hypothesis_id: str = Field(description="Hypothesis ID to link to")
    evidence_id: str = Field(description="Observation evidence ID")
    relation: Literal["supports", "contradicts"] = Field(
        default="supports",
        description="Whether evidence corroborates or refutes hypothesis",
    )


class SetStatusOp(BaseModel):
    """Operation mutating the validation status of a hypothesis."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["set_status"] = "set_status"
    hypothesis_id: str = Field(description="Hypothesis ID to mutate")
    status: Literal["open", "supported", "refuted", "inconclusive"] = Field(
        description="New validation status",
    )


HypothesisOp = Annotated[
    Union[AddHypothesisOp, LinkEvidenceOp, SetStatusOp],
    Field(discriminator="op"),
]


class UpdateHypothesesStepV2(BaseModel):
    """Model decision to manipulate the hypothesis ledger via structured operations."""

    model_config = ConfigDict(extra="forbid")

    step: Literal["update_hypotheses"] = "update_hypotheses"
    operations: list[HypothesisOp] = Field(
        description="List of hypothesis operations (max 6 operations per turn)",
    )
    reason: str = Field(
        description="Short summary rationale for ledger modifications (max 300 chars)",
    )

    @field_validator("operations")
    @classmethod
    def validate_ops_count(cls, v: list[Any]) -> list[Any]:
        if len(v) > 6:
            raise ValueError(f"Maximum 6 operations allowed per turn; received {len(v)}")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        return validate_step_reason(v)


class ConcludeStepV2(BaseModel):
    """Model decision to conclude the investigation, referencing the winning hypothesis."""

    model_config = ConfigDict(extra="forbid")

    step: Literal["conclude"] = "conclude"
    leading_hypothesis_id: str | None = Field(
        default=None,
        description="Hypothesis ID from the ledger that won the investigation or None if unknown",
    )
    diagnosis_draft: Diagnosis = Field(
        description="Structured diagnosis artifact conforming to Feature 1.12 Diagnosis schema",
    )
    reason: str = Field(
        description="Short summary rationale explaining the diagnostic conclusion (max 300 chars)",
    )

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        return validate_step_reason(v)


AgentStepV2 = Annotated[
    Union[CallToolStepV2, UpdateHypothesesStepV2, ConcludeStepV2],
    Field(discriminator="step"),
]

_AGENT_STEP_V2_ADAPTER: TypeAdapter[CallToolStepV2 | UpdateHypothesesStepV2 | ConcludeStepV2] = (
    TypeAdapter(AgentStepV2)
)


# ===========================================================================
# Single Flat Schema for Gemini SDK Structured Output (Requirement 4)
# ===========================================================================

class FlatAgentStepV2(BaseModel):
    """Single flat model-facing schema for reliable Gemini SDK structured output.
    
    Avoids OpenAPI 'anyOf' / polymorphic Union rejection in Google GenAI REST backend.
    Parsed deterministically into CallToolStepV2, UpdateHypothesesStepV2, or ConcludeStepV2.
    """

    model_config = ConfigDict(extra="forbid")

    step: Literal["call_tool", "update_hypotheses", "conclude"] = Field(
        description="Decision kind for this turn: 'call_tool', 'update_hypotheses', or 'conclude'",
    )
    # call_tool fields
    tool: str | None = Field(default=None, description="Authorized tool to invoke (if step=call_tool)")
    args: dict[str, Any] = Field(default_factory=dict, description="Tool arguments (if step=call_tool)")
    tests_hypothesis_id: str | None = Field(
        default=None, description="Hypothesis tested by tool call (if step=call_tool)"
    )
    expected_if_true: str | None = Field(
        default=None, description="Expected observation if true (max 160 chars, if step=call_tool)"
    )
    expected_if_false: str | None = Field(
        default=None, description="Expected observation if false (max 160 chars, if step=call_tool)"
    )
    # update_hypotheses fields
    operations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of hypothesis ops: add, link_evidence, set_status (max 6, if step=update_hypotheses)",
    )
    # conclude fields
    leading_hypothesis_id: str | None = Field(
        default=None, description="Winning hypothesis ID (if step=conclude)"
    )
    diagnosis_draft: Diagnosis | dict[str, Any] | None = Field(
        default=None, description="Diagnosis dictionary conforming to Feature 1.12 (if step=conclude)"
    )
    # Universal field
    reason: str = Field(description="Operational rationale for this step (max 300 chars)")


# ===========================================================================
# Typed Step Rejection (Requirement 3)
# ===========================================================================

class StepRejection(BaseModel):
    """Deterministic typed rejection returned when a proposed step fails pre-execution validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(description="Machine-readable rejection code")
    field: str = Field(description="Name of rejected field or subfield")
    hint: str = Field(description="Actionable correction guidance for the model on next turn")


# ===========================================================================
# Deterministic StepValidator (Requirement 2)
# ===========================================================================

class StepValidator:
    """Pre-execution validator ensuring proposed steps strictly adhere to empirical ledger rules."""

    @classmethod
    def validate_step(
        cls,
        step: Any,
        state: Mapping[str, Any] | dict[str, Any],
        evidence_store: Any = None,
        incident_window: tuple[datetime, datetime] | None = None,
    ) -> StepRejection | None:
        """Validates a step prior to dispatch or execution. Returns StepRejection on failure, else None."""
        raw_ledger = state.get("hypotheses_ledger", {})
        if isinstance(raw_ledger, HypothesisLedger):
            ledger: dict[str, Any] = raw_ledger.hypotheses
        elif isinstance(raw_ledger, dict):
            ledger = raw_ledger
        else:
            ledger = {}

        # -------------------------------------------------------------------
        # 1. Validate CallToolStep
        # -------------------------------------------------------------------
        if getattr(step, "step", None) == "call_tool":
            tests_id = getattr(step, "tests_hypothesis_id", None)
            if tests_id is not None:
                hyp = ledger.get(tests_id)
                if hyp is None:
                    return StepRejection(
                        code="unknown_hypothesis",
                        field="tests_hypothesis_id",
                        hint=f"Hypothesis '{tests_id}' does not exist on the ledger.",
                    )
                status = getattr(hyp, "status", None) or (hyp.get("status") if isinstance(hyp, dict) else None)
                if status == "refuted":
                    return StepRejection(
                        code="hypothesis_already_refuted",
                        field="tests_hypothesis_id",
                        hint=f"Hypothesis '{tests_id}' is already refuted; cannot be tested further without reopening.",
                    )
            return None

        # -------------------------------------------------------------------
        # 2. Validate UpdateHypothesesStep
        # -------------------------------------------------------------------
        if getattr(step, "step", None) == "update_hypotheses":
            ops = getattr(step, "operations", [])
            if len(ops) > 6:
                return StepRejection(
                    code="too_many_ops",
                    field="operations",
                    hint=f"Maximum 6 operations allowed per turn; received {len(ops)}.",
                )

            simulated_ledger: dict[str, dict[str, Any]] = {}
            for k, v in ledger.items():
                s_ids = list(getattr(v, "supporting_evidence_ids", ()) or (v.get("supporting_evidence_ids", []) if isinstance(v, dict) else []))
                c_ids = list(getattr(v, "contrary_evidence_ids", ()) or (v.get("contrary_evidence_ids", []) if isinstance(v, dict) else []))
                st = getattr(v, "status", None) or (v.get("status") if isinstance(v, dict) else "open")
                simulated_ledger[k] = {"status": st, "supporting_evidence_ids": s_ids, "contrary_evidence_ids": c_ids}

            current_count = len(ledger)
            for i, op in enumerate(ops):
                op_dict = op if isinstance(op, dict) else op.model_dump()
                op_type = op_dict.get("op")

                if op_type == "add":
                    if current_count >= MAX_HYPOTHESES_PER_RUN:
                        return StepRejection(
                            code="ledger_capacity_exceeded",
                            field=f"operations[{i}]",
                            hint=f"Cannot add hypothesis: ledger has reached maximum capacity of {MAX_HYPOTHESES_PER_RUN}.",
                        )
                    # Check confidence proposal format
                    if "confidence" in op_dict and isinstance(op_dict["confidence"], float):
                        return StepRejection(
                            code="invalid_confidence_format",
                            field=f"operations[{i}].confidence",
                            hint="Model cannot set numerical confidence directly. Propose confidence_label ('low', 'medium', 'high').",
                        )
                    current_count += 1
                    new_hyp_id = f"hyp_{current_count:02d}"
                    simulated_ledger[new_hyp_id] = {
                        "status": "open",
                        "supporting_evidence_ids": [],
                        "contrary_evidence_ids": [],
                    }

                elif op_type == "link_evidence":
                    hyp_id = op_dict.get("hypothesis_id")
                    ev_id = op_dict.get("evidence_id")
                    if hyp_id not in simulated_ledger:
                        return StepRejection(
                            code="unknown_hypothesis",
                            field=f"operations[{i}].hypothesis_id",
                            hint=f"Hypothesis '{hyp_id}' does not exist on the ledger.",
                        )
                    # Verify evidence groundedness using evidence_store or run state
                    run_evidence_ids = state.get("evidence_ids", [])
                    if evidence_store is not None:
                        temp_diag = Diagnosis(
                            root_cause_service="dummy",
                            root_cause_category="bad_deploy",
                            mechanism="connection_pool_exhaustion",
                            evidence_ids=[ev_id] if ev_id else [],
                            confidence=0.5,
                        )
                        grade = grade_evidence_support(
                            diagnosis=temp_diag,
                            evidence_store=evidence_store,
                            incident_window=incident_window,
                        )
                        if not grade.supported:
                            return StepRejection(
                                code="invalid_evidence_id",
                                field=f"operations[{i}].evidence_id",
                                hint=f"Evidence ID '{ev_id}' does not exist in this run's observations, is out-of-window, or is from an unauthorized tool.",
                            )
                    elif ev_id not in run_evidence_ids:
                        return StepRejection(
                            code="invalid_evidence_id",
                            field=f"operations[{i}].evidence_id",
                            hint=f"Evidence ID '{ev_id}' does not exist in this run's observations.",
                        )

                    relation = op_dict.get("relation", "supports")
                    if relation == "supports":
                        simulated_ledger[hyp_id]["supporting_evidence_ids"].append(ev_id)
                    elif relation == "contradicts":
                        simulated_ledger[hyp_id]["contrary_evidence_ids"].append(ev_id)
                    simulated_ledger[hyp_id]["_new_evidence_linked"] = True

                elif op_type == "set_status":
                    hyp_id = op_dict.get("hypothesis_id")
                    new_status = op_dict.get("status")
                    hyp_entry = simulated_ledger.get(hyp_id) if isinstance(hyp_id, str) else None
                    if hyp_entry is None:
                        return StepRejection(
                            code="unknown_hypothesis",
                            field=f"operations[{i}].hypothesis_id",
                            hint=f"Hypothesis '{hyp_id}' does not exist on the ledger.",
                        )
                    supp_ids = hyp_entry["supporting_evidence_ids"]
                    cont_ids = hyp_entry["contrary_evidence_ids"]
                    curr_status = hyp_entry.get("status", "open")
                    if curr_status == "refuted" and new_status != "refuted":
                        total_ev = len(supp_ids) + len(cont_ids)
                        count_at_ref = hyp_entry.get("evidence_count_at_refuted")
                        has_new_ev = bool(hyp_entry.get("_new_evidence_linked")) or (
                            count_at_ref is not None and total_ev > int(count_at_ref)
                        )
                        if not has_new_ev:
                            return StepRejection(
                                code="cannot_reopen_refuted",
                                field=f"operations[{i}].status",
                                hint=f"Refuted hypothesis '{hyp_id}' cannot be reopened without linking new evidence.",
                            )
                    if new_status == "supported" and len(supp_ids) == 0:
                        return StepRejection(
                            code="supported_without_evidence",
                            field=f"operations[{i}].status",
                            hint=f"Hypothesis '{hyp_id}' cannot be marked 'supported' with zero supporting evidence.",
                        )
                    if new_status == "refuted" and len(cont_ids) == 0:
                        return StepRejection(
                            code="refuted_without_contradiction",
                            field=f"operations[{i}].status",
                            hint=f"Hypothesis '{hyp_id}' cannot be marked 'refuted' with zero contradicting evidence.",
                        )
                    if new_status == "refuted":
                        hyp_entry["evidence_count_at_refuted"] = len(supp_ids) + len(cont_ids)
                        hyp_entry["_new_evidence_linked"] = False
                    hyp_entry["status"] = new_status
            return None

        # -------------------------------------------------------------------
        # 3. Validate ConcludeStep
        # -------------------------------------------------------------------
        if getattr(step, "step", None) == "conclude":
            leading_id = getattr(step, "leading_hypothesis_id", None)
            if leading_id is not None and isinstance(leading_id, str):
                hyp = ledger.get(leading_id)
                if hyp is None:
                    return StepRejection(
                        code="unknown_hypothesis",
                        field="leading_hypothesis_id",
                        hint=f"Leading hypothesis '{leading_id}' does not exist on the ledger.",
                    )
                status = getattr(hyp, "status", None) or (hyp.get("status") if isinstance(hyp, dict) else None)
                if status == "refuted":
                    return StepRejection(
                        code="hypothesis_already_refuted",
                        field="leading_hypothesis_id",
                        hint=f"Leading hypothesis '{leading_id}' is refuted and cannot be concluded.",
                    )
            return None

        return None


# ===========================================================================
# Parser Helpers
# ===========================================================================

def parse_agent_step(
    raw: dict[str, Any] | str,
    version: Literal["v1", "v2"] = "v1",
) -> Any:
    """Parses and validates a raw dict or JSON string into an AgentStep (v1 or v2)."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError(f"Step payload must be a JSON object, got {type(data)}")

    if version == "v1":
        return _AGENT_STEP_V1_ADAPTER.validate_python(data)

    return parse_agent_step_v2(data)


def parse_agent_step_v2(
    raw: dict[str, Any] | str,
) -> CallToolStepV2 | UpdateHypothesesStepV2 | ConcludeStepV2:
    """Parses and validates a raw dict or JSON string into a V2 AgentStep."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError(f"Step payload must be a JSON object, got {type(data)}")

    step_kind = data.get("step")
    if step_kind == "call_tool":
        filtered = {
            "step": "call_tool",
            "tool": data.get("tool"),
            "args": data.get("args") or {},
            "tests_hypothesis_id": data.get("tests_hypothesis_id"),
            "expected_if_true": data.get("expected_if_true"),
            "expected_if_false": data.get("expected_if_false"),
            "reason": data.get("reason", ""),
        }
        return CallToolStepV2.model_validate(filtered)
    elif step_kind == "update_hypotheses":
        filtered = {
            "step": "update_hypotheses",
            "operations": data.get("operations") or [],
            "reason": data.get("reason", ""),
        }
        return UpdateHypothesesStepV2.model_validate(filtered)
    elif step_kind == "conclude":
        diag = data.get("diagnosis_draft")
        if diag is not None and hasattr(diag, "model_dump"):
            diag = diag.model_dump()
        filtered = {
            "step": "conclude",
            "leading_hypothesis_id": data.get("leading_hypothesis_id"),
            "diagnosis_draft": diag,
            "reason": data.get("reason", ""),
        }
        return ConcludeStepV2.model_validate(filtered)
    else:
        # Fallback to discriminated union adapter validation
        return _AGENT_STEP_V2_ADAPTER.validate_python(data)

"""agent/graph/steps.py

Pydantic step schemas emitted by the LLM each turn during incident investigation.

Defines:
- CallToolStep: Model decision to invoke an authorized investigation tool.
- ConcludeStep: Model decision to conclude investigation and output a diagnosis.
- AgentStep: Discriminated union of CallToolStep and ConcludeStep.

Constraints:
1. tool name must be in the closed allowlist of read-only investigation tools.
2. args validated by the tool's own input model at dispatch time.
3. reason must be plain text and maximum 300 characters (no chain-of-thought).
4. diagnosis_draft conforms strictly to the Feature 1.12 Diagnosis schema.
5. All extra fields are forbidden (extra="forbid").
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from evals.graders.models import Diagnosis

# Closed allowlist of read-only investigation tools conforming to Feature 2 tool contract
ALLOWED_INVESTIGATION_TOOLS: tuple[str, ...] = (
    "get_metrics",
    "search_logs",
    "get_trace",
    "list_deploys",
    "get_deploy_diff",
)


def validate_step_reason(reason: str) -> str:
    """Validates that reason is concise, plain-text operational rationale (max 300 chars).
    
    Rejects:
    - Empty or whitespace-only strings.
    - Strings exceeding 300 characters.
    - Non-printable control characters and null bytes.
    """
    cleaned = reason.strip()
    if not cleaned:
        raise ValueError("Step reason cannot be empty or whitespace only")

    if len(cleaned) > 300:
        raise ValueError(
            f"Step reason exceeds 300 characters: {len(cleaned)} chars provided"
        )

    for ch in cleaned:
        # Disallow non-printable ASCII/Unicode control characters except newline and tab
        if unicodedata.category(ch).startswith("C") and ch not in ("\n", "\t"):
            raise ValueError(
                "Step reason must be plain text without control characters or null bytes"
            )

    return cleaned


class CallToolStep(BaseModel):
    """Model decision to invoke an authorized investigation tool."""

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
    """Model decision to conclude the investigation with a diagnosis artifact."""

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


# Discriminated union of model turn decisions
AgentStep = Annotated[
    Union[CallToolStep, ConcludeStep],
    Field(discriminator="step"),
]

_AGENT_STEP_ADAPTER: TypeAdapter[CallToolStep | ConcludeStep] = TypeAdapter(AgentStep)


def parse_agent_step(raw: dict[str, Any] | str) -> CallToolStep | ConcludeStep:
    """Parses and validates a raw dict or JSON string into a CallToolStep or ConcludeStep."""
    if isinstance(raw, str):
        return _AGENT_STEP_ADAPTER.validate_json(raw)
    return _AGENT_STEP_ADAPTER.validate_python(raw)

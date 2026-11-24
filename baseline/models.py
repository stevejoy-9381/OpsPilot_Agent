"""Common data models for baseline evaluation runners (B0, B1, B2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis
from shared.actions import RecommendedAction


class BaselineResult(BaseModel):
    """Encapsulates the complete diagnostic and remediation output of a baseline runner."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str | None = Field(default=None, description="Scenario ID under test (if known)")
    baseline_name: str = Field(description="Name of the baseline: B0, B1, or B2")
    alert: AlertEvent = Field(description="Triggering alert event")
    diagnosis: Diagnosis = Field(description="Structured root cause diagnosis")
    action: RecommendedAction = Field(description="Recommended recovery or escalation action")
    tool_calls: list[str] = Field(
        default_factory=list, description="Ordered list of tool calls made"
    )
    evidence_ids: list[str] = Field(default_factory=list, description="Collected evidence IDs")
    rule_matched: str | None = Field(default=None, description="Runbook rule matched (for B1)")
    duration_seconds: float = Field(default=0.0, description="Execution duration in seconds")
    tokens_in: int = Field(default=0, description="Prompt tokens consumed (0 for non-LLM)")
    tokens_out: int = Field(default=0, description="Completion tokens produced (0 for non-LLM)")
    cost_usd: float = Field(default=0.0, description="Financial cost in USD (0.0 for non-LLM)")
    latency_ms: float = Field(default=0.0, description="LLM call latency in milliseconds")
    error_type: str | None = Field(
        default=None,
        description="Error type if provider_error, timeout, or schema_failure occurred",
    )
    uncertainty_note: str | None = Field(
        default=None, description="Explanatory uncertainty note from LLM"
    )
    prompt_hash: str | None = Field(
        default=None, description="SHA-256 hash of the prompt template used"
    )
    trial: int = Field(default=1, description="Trial index for multi-trial evaluation")

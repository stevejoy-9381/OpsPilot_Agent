"""Data models and schemas for OpsPilot Lab Controller and Vault."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RootCause(BaseModel):
    """Ground truth root-cause specification."""

    service: str | None = Field(default=None, description="Primary culpable service or subsystem")
    component: str | None = Field(
        default=None, description="Subsystem component (e.g. db_pool, cache, dependency)"
    )
    mechanism: str = Field(description="Detailed failure mechanism")


class HealthThresholds(BaseModel):
    """Scenario health evaluation criteria."""

    error_rate: float = Field(
        default=0.05,
        description="Max acceptable HTTP error rate (0.0 to 1.0)",
    )
    p95_latency_ms: float = Field(
        default=500.0,
        description="Max acceptable p95 latency in milliseconds",
    )


class FaultInjection(BaseModel):
    """Specification for simulated dependency latency or error rate fault injection."""

    service: str = Field(description="Target service (e.g. gateway, orders, inventory)")
    dependency_latency_ms: dict[str, float] | None = Field(
        default=None, description="Latency to inject per dependency in milliseconds"
    )
    dependency_error_rate: dict[str, float] | None = Field(
        default=None, description="Synthetic error rate to inject (0.0 to 1.0)"
    )
    leak_on_conflict: bool | None = Field(
        default=None, description="Explicit connection leak flag on 409 conflict"
    )


class ScenarioDefinition(BaseModel):
    """Immutable scenario specification."""

    scenario_id: str = Field(description="Unique scenario code (e.g. F1, F2, M1)")
    name: str = Field(description="Human readable scenario title")
    description: str = Field(description="Scenario summary")
    family: str = Field(description="Incident family (e.g. leak, latency, regression, multi_fault)")
    root_cause: RootCause
    culprit_service: str | None = Field(
        default=None, description="Target service to deploy culprit profile to"
    )
    culprit_version: str | None = Field(
        default=None, description="Target culprit version profile"
    )
    faults: list[FaultInjection] = Field(
        default_factory=list, description="List of concurrent fault injections"
    )
    expected_action: dict[str, Any] = Field(
        description="Ground truth expected recovery action (e.g. rollback, restart)"
    )
    decoys: list[dict[str, Any]] = Field(
        default_factory=list, description="Synthetic decoy changes or logs"
    )
    health_thresholds: HealthThresholds = Field(default_factory=HealthThresholds)
    split_tag: str = Field(default="eval", description="Evaluation partition (eval, test, train)")
    load_profile: str = Field(default="steady_20rps", description="Named loadgen profile to run")
    warmup_seconds: float = Field(default=5.0, description="Warmup duration before injection")


class TruthRecord(BaseModel):
    """Vault record containing immutable ground truth for an incident run."""

    run_id: str
    scenario_id: str
    family: str
    root_cause: RootCause | dict[str, Any]
    culprit_deploy_id: str | None = None
    injected_at: str
    expected_action: dict[str, Any]
    decoys: list[dict[str, Any]] = Field(default_factory=list)
    health_thresholds: dict[str, float]
    split_tag: str
    created_at: str
    status: str = "running"


class RunRequest(BaseModel):
    """Request payload to initiate a new scenario run."""

    scenario_id: str = Field(description="Scenario identifier to execute (e.g. F1, F2)")
    seed: int = Field(default=42, description="Random seed for deterministic request generation")
    warmup_s: float | None = Field(
        default=None,
        description="Optional override for warmup duration",
    )


class RunResponse(BaseModel):
    """Response returned upon starting a run. CRITICAL: Never discloses truth."""

    run_id: str
    scenario_id: str
    status: str = "running"


class OracleResult(BaseModel):
    """Diagnostic health assessment produced by the Oracle."""

    healthy: bool
    error_rate: float
    p95_latency_ms: float
    thresholds: dict[str, float]
    window: str
    timestamp: str
    details: dict[str, Any] = Field(default_factory=dict)

"""Pydantic schema definitions and semantic validators for OpsPilot scenarios."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Known registered service profiles across the synthetic lab
KNOWN_SERVICE_VERSIONS: dict[str, set[str]] = {
    "orders": {"v1.0.0", "v1.1.0", "v1.3.0"},
    "inventory": {"v1.0.0", "v1.1.0", "v1.2.0"},
    "gateway": {"v1.0.0", "v1.1.0"},
}

KNOWN_FAMILIES: set[str] = {
    "pool_leak",
    "latency_regression",
    "dependency_timeout",
    "dependency_timeout_with_decoy",
    "false_alarm",
}


class BaseTimelineEvent(BaseModel):
    """Base model for all events in a scenario timeline."""

    model_config = ConfigDict(extra="forbid")

    event_id: str | None = Field(default=None, description="Optional unique identifier for event")
    offset_s: float = Field(ge=0, description="Seconds from run start when event triggers")
    type: str


class DeployEvent(BaseTimelineEvent):
    """Event switching a service's active version profile."""

    type: Literal["deploy"] = "deploy"
    service: str = Field(description="Target service to deploy")
    version: str = Field(description="Target version profile to activate")


class FaultEvent(BaseTimelineEvent):
    """Event injecting synthetic latency, error rate, or configuration fault."""

    type: Literal["fault"] = "fault"
    service: str = Field(description="Target service for fault injection")
    dependency: str | None = Field(
        default=None, description="Target dependency name (e.g. redis, db)"
    )
    dependency_latency_ms: float | None = Field(
        default=None, ge=0, description="Injected latency in milliseconds"
    )
    dependency_error_rate: float | None = Field(
        default=None, ge=0, le=1.0, description="Injected error rate probability (0.0 to 1.0)"
    )
    leak_on_conflict: bool | None = Field(
        default=None, description="Explicit flag for connection leak on 409 conflict"
    )


class LoadChangeEvent(BaseTimelineEvent):
    """Event modifying synthetic traffic pattern or profile."""

    type: Literal["load_change"] = "load_change"
    profile: str | None = Field(default=None, description="Target named loadgen profile")
    rps: float | None = Field(default=None, ge=0, description="Direct target request rate")
    duration_s: float | None = Field(default=None, ge=0, description="Duration of load change")


TimelineEvent = Annotated[
    Union[DeployEvent, FaultEvent, LoadChangeEvent],
    Field(discriminator="type"),
]


class ExpectedAction(BaseModel):
    """Ground truth expected recovery action."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        description="Target recovery action (e.g. rollback, remediate_dependency, no_action)"
    )
    service: str | None = Field(
        default=None, description="Target service for rollback or remediation"
    )
    target: str | None = Field(default=None, description="Target component or dependency")


class GroundTruth(BaseModel):
    """Ground truth failure mechanism and diagnosis criteria."""

    model_config = ConfigDict(extra="forbid")

    root_cause_service: str | None = Field(
        default=None, description="Primary culpable service or null if false alarm"
    )
    root_cause_category: str = Field(
        description="Category: software_defect, performance_regression, dependency_failure, none"
    )
    mechanism: str = Field(description="Detailed physical failure mechanism")
    culprit_deploy: str | None = Field(
        default=None, description="Timeline event_id of culprit deploy, or null"
    )
    expected_action: ExpectedAction


class HealthThresholds(BaseModel):
    """Criteria defining SLA health boundaries."""

    model_config = ConfigDict(extra="forbid")

    error_rate: float = Field(
        ge=0, le=1.0, description="Maximum acceptable error rate (0.0 to 1.0)"
    )
    p95_latency_ms: float = Field(
        gt=0, description="Maximum acceptable tail latency in milliseconds"
    )


class Scenario(BaseModel):
    """Complete specification of a deterministic incident simulation scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(description="Unique scenario code (e.g. S1, S2)")
    family: str = Field(description="Incident family (pool_leak, latency_regression, etc.)")
    variant: str = Field(description="Scenario variation code")
    template_id: str = Field(description="Reference scenario archetype identifier")
    seed: int = Field(description="Deterministic random seed")
    split: str = Field(
        default="unassigned", description="Dataset split (unassigned, train, eval, test)"
    )
    baseline_versions: dict[str, str] = Field(
        description="Baseline version profile for each lab service"
    )
    timeline: list[TimelineEvent] = Field(
        description="Ordered sequence of timeline events during the run"
    )
    load_profile: str = Field(description="Named loadgen profile to execute")
    warmup_s: float = Field(ge=0, description="Warmup duration in seconds before timeline starts")
    observe_s: float = Field(gt=0, description="Observation observation window in seconds")
    evidence_degradations: list[Any] = Field(
        default_factory=list, description="Degradation indicators (empty for baseline)"
    )
    ground_truth: GroundTruth
    health_thresholds: HealthThresholds
    narrative: str = Field(description="2-3 sentence human summary of the incident")

    @model_validator(mode="after")
    def validate_scenario_semantics(self) -> Scenario:
        # 1. Validate family is known
        if self.family not in KNOWN_FAMILIES:
            raise ValueError(
                f"Unknown scenario family '{self.family}'. Must be one of {sorted(KNOWN_FAMILIES)}"
            )

        # 2. Validate baseline versions exist
        for service, version in self.baseline_versions.items():
            if service not in KNOWN_SERVICE_VERSIONS:
                raise ValueError(
                    f"Unknown service '{service}' in baseline_versions. "
                    f"Known services: {sorted(KNOWN_SERVICE_VERSIONS.keys())}"
                )
            if version not in KNOWN_SERVICE_VERSIONS[service]:
                valid_versions = sorted(KNOWN_SERVICE_VERSIONS[service])
                raise ValueError(
                    f"Version '{version}' for service '{service}' not in baseline_versions. "
                    f"Valid: {valid_versions}"
                )

        # 3. Validate timeline offsets are non-negative and non-decreasing
        prev_offset = -1.0
        deploy_event_ids: set[str] = set()

        for idx, event in enumerate(self.timeline):
            if event.offset_s < 0:
                raise ValueError(
                    f"Timeline event at index {idx} has negative offset_s: {event.offset_s}"
                )
            if event.offset_s < prev_offset:
                raise ValueError(
                    f"Timeline events must be ordered by offset_s: event at index {idx} "
                    f"has offset_s={event.offset_s} < previous offset_s={prev_offset}"
                )
            prev_offset = event.offset_s

            # Validate deploy event versions exist
            if isinstance(event, DeployEvent):
                if event.service not in KNOWN_SERVICE_VERSIONS:
                    raise ValueError(
                        f"Deploy event at index {idx} references unknown service '{event.service}'"
                    )
                if event.version not in KNOWN_SERVICE_VERSIONS[event.service]:
                    valid = sorted(KNOWN_SERVICE_VERSIONS[event.service])
                    raise ValueError(
                        f"Deploy event at index {idx} references invalid version '{event.version}' "
                        f"for service '{event.service}'. Valid versions: {valid}"
                    )
                if event.event_id:
                    deploy_event_ids.add(event.event_id)

        # 4. Validate culprit_deploy reference
        culprit = self.ground_truth.culprit_deploy
        if culprit is not None:
            if culprit not in deploy_event_ids:
                raise ValueError(
                    f"ground_truth.culprit_deploy '{culprit}' does not match any deploy event_id "
                    f"in the timeline. Available deploy event_ids: {sorted(deploy_event_ids)}"
                )

        # 5. Validate narrative length (2-3 sentences)
        sentences = re.split(r"\.(?:\s+|$)", self.narrative.strip())
        sentences = [s.strip() for s in sentences if s.strip()]
        if len(sentences) < 2 or len(sentences) > 3:
            narrative_repr = repr(self.narrative)
            raise ValueError(
                f"Scenario narrative must be exactly 2-3 sentences. "
                f"Found {len(sentences)}: {narrative_repr}"
            )

        return self

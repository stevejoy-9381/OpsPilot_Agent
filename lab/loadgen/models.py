from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


class ProfileType(str, Enum):
    STEADY = "steady"
    BURST = "burst"
    RAMP = "ramp"


class BaseProfile(BaseModel):
    """Common configuration attributes across all load profiles."""

    profile_id: str = Field(description="Unique identifier for the profile")
    type: ProfileType = Field(description="Load generation profile type")
    seed: int = Field(default=42, description="Random seed for reproducible traffic sequences")
    description: str = Field(default="", description="Human-readable description of the profile")


class SteadyProfile(BaseProfile):
    """Steady constant request rate profile."""

    type: Literal[ProfileType.STEADY] = ProfileType.STEADY
    rps: float = Field(..., gt=0, description="Target constant requests per second")


class BurstProfile(BaseProfile):
    """Baseline request rate with a Kx spike multiplier for T seconds."""

    type: Literal[ProfileType.BURST] = ProfileType.BURST
    baseline_rps: float = Field(..., gt=0, description="Baseline continuous request rate")
    spike_multiplier: float = Field(..., gt=1.0, description="Multiplier during the spike period")
    spike_duration_s: float = Field(..., gt=0, description="Duration of the spike in seconds")
    spike_start_s: float = Field(
        default=10.0, ge=0, description="Start offset in seconds for the burst"
    )

    @property
    def spike_rps(self) -> float:
        return self.baseline_rps * self.spike_multiplier


class RampProfile(BaseProfile):
    """Linear ramp request rate profile from start_rps to end_rps."""

    type: Literal[ProfileType.RAMP] = ProfileType.RAMP
    start_rps: float = Field(..., gt=0, description="Starting requests per second")
    end_rps: float = Field(..., gt=0, description="Target ending requests per second")


LoadProfile = Annotated[
    Union[SteadyProfile, BurstProfile, RampProfile],
    Field(discriminator="type"),
]


class ScheduledRequest(BaseModel):
    """A single scheduled request in the open-loop dispatch sequence."""

    offset_s: float = Field(description="Offset in seconds from test start to dispatch")
    product_id: int = Field(description="Product ID chosen via seeded Zipf distribution")
    qty: int = Field(description="Quantity chosen via seeded distribution")


class LatencyStats(BaseModel):
    """Summary latency distribution statistics in milliseconds."""

    min: float
    avg: float
    p50: float
    p95: float
    p99: float
    max: float


class RunSummary(BaseModel):
    """Final output summary written to loadgen volume after a run."""

    profile_id: str
    seed: int
    duration_s: float
    start_time: str
    end_time: str
    target_rps: float
    achieved_rps: float
    total_requests: int
    status_counts: dict[str, int]
    error_count: int
    error_rate: float
    latencies_ms: LatencyStats | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

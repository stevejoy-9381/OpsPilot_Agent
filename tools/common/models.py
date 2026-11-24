"""Shared Pydantic models and types for investigation tools."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """Standard machine-readable error codes for tool failures."""

    INVALID_ARGUMENT = "invalid_argument"
    OUT_OF_WINDOW = "out_of_window"
    NOT_FOUND = "not_found"
    STALE_DATA = "stale_data"
    SOURCE_UNAVAILABLE = "source_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ACCESS_DENIED = "access_denied"


class Unit(str, Enum):
    """Physical telemetry units for tool measurements."""

    RATIO = "ratio"
    REQUESTS_PER_SECOND = "requests_per_second"
    SECONDS = "seconds"
    COUNT = "count"
    PERCENT = "percent"


class Window(BaseModel):
    """Temporal time range for tool queries and telemetry windows."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    step_seconds: int | None = None


class EvidenceRef(BaseModel):
    """Reference pointer to a captured piece of evidence."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    kind: str
    source: str
    captured_at: datetime


class ToolContext(BaseModel):
    """Server-injected execution context for investigation tools.

    This context is strictly injected by the server runtime and is immutable (frozen).
    The model cannot modify or override any fields.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    tenant_id: str
    incident_id: str
    window_start: datetime
    window_end: datetime
    allowed_services: list[str]
    remaining_calls: int


class ToolErrorDetail(BaseModel):
    """Structured details of an investigation tool error."""

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    remedy: str = ""


class ToolError(BaseModel):
    """Error envelope returned by investigation tools."""

    model_config = ConfigDict(frozen=True)

    ok: Literal[False] = False
    error: ToolErrorDetail


T = TypeVar("T")


class ToolSuccess(BaseModel, Generic[T]):
    """Success envelope returned by investigation tools."""

    model_config = ConfigDict(frozen=True)

    ok: Literal[True] = True
    evidence_id: str
    tool: str
    captured_at: datetime
    window: Window
    source: str
    units: Unit | None = None
    data: T
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


# Discriminated union for tool responses
ToolResult = Annotated[ToolSuccess[Any] | ToolError, Field(discriminator="ok")]

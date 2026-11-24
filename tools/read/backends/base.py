"""Abstract backend interfaces for read tools.

Defines:
- RawMetricPoint, RawMetricSeries
- RawLogLine
- RawSpan, RawTrace
- RawDeployRecord, RawDeployDiff
- Abstract base protocols: MetricsBackend, LogsBackend, TraceBackend, DeployBackend
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from pydantic import BaseModel, ConfigDict, Field


class RawMetricPoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    timestamp: datetime
    value: float


class RawMetricSeries(BaseModel):
    model_config = ConfigDict(frozen=True)
    metric_name: str
    service: str
    labels: dict[str, str] = Field(default_factory=dict)
    points: list[RawMetricPoint] = Field(default_factory=list)


class RawLogLine(BaseModel):
    model_config = ConfigDict(frozen=True)
    timestamp: datetime
    service: str
    level: str
    message: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class RawSpan(BaseModel):
    model_config = ConfigDict(frozen=True)
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    service_name: str
    name: str
    start_time: datetime
    end_time: datetime
    status_code: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class RawTrace(BaseModel):
    model_config = ConfigDict(frozen=True)
    trace_id: str
    spans: list[RawSpan] = Field(default_factory=list)


class RawDeployRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    deploy_id: str
    service: str
    version: str
    deployed_at: datetime
    git_commit: str
    status: str
    environment: str = "lab"


class RawDeployDiff(BaseModel):
    model_config = ConfigDict(frozen=True)
    deploy_id: str
    service: str
    previous_version: str
    current_version: str
    diff_text: str
    files_changed: list[str] = Field(default_factory=list)


@runtime_checkable
class MetricsBackend(Protocol):
    async def query_metric(
        self,
        service: str,
        metric: str,
        start_time: datetime,
        end_time: datetime,
        step_seconds: int = 15,
    ) -> list[RawMetricSeries]:
        ...


@runtime_checkable
class LogsBackend(Protocol):
    async def search_logs(
        self,
        service: str,
        start_time: datetime,
        end_time: datetime,
        pattern: str | None = None,
        level: str | None = None,
        limit: int = 100,
    ) -> list[RawLogLine]:
        ...


@runtime_checkable
class TraceBackend(Protocol):
    async def get_trace(
        self,
        trace_id: str,
    ) -> RawTrace | None:
        ...


@runtime_checkable
class DeployBackend(Protocol):
    async def list_deploys(
        self,
        service: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 10,
    ) -> list[RawDeployRecord]:
        ...

    async def get_deploy_diff(
        self,
        deploy_id: str,
    ) -> RawDeployDiff | None:
        ...

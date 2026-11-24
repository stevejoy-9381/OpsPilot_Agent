"""get_metrics tool implementation.

Queries time-series metrics from the configured backend, downsamples to <= 30 points,
computes point statistics, baseline comparison over preceding window, mechanical anomaly notes,
persists to EvidenceStore, and returns a ToolSuccess[MetricsPayload] or ToolError.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from tools.common.errors import (
    access_denied,
    catch_tool_errors,
    invalid_argument,
    not_found,
    source_unavailable,
    stale_data,
)
from tools.common.evidence import make_evidence_id
from tools.common.evidence_store import EvidenceRecord, EvidenceStore
from tools.common.limits import tool_limits
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolResult, ToolSuccess, Unit
from tools.common.window import validate_and_clamp_window
from tools.read.backends.base import MetricsBackend, RawMetricPoint
from tools.read.backends.factory import get_backend

# ---------------------------------------------------------------------------
# Metric Units & Enums
# ---------------------------------------------------------------------------
METRIC_UNITS: dict[str, Unit] = {
    "error_rate": Unit.RATIO,
    "request_rate": Unit.REQUESTS_PER_SECOND,
    "latency_p50": Unit.SECONDS,
    "latency_p95": Unit.SECONDS,
    "latency_p99": Unit.SECONDS,
    "db_pool_in_use": Unit.COUNT,
    "db_pool_wait_seconds": Unit.SECONDS,
    "dependency_latency_p95": Unit.SECONDS,
    "dependency_errors_rate": Unit.RATIO,
}

STALE_AFTER_SECONDS: int = 120
MAX_OUTPUT_POINTS: int = 30


class MetricPoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    t: str
    v: float


class MetricSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    min: float
    max: float
    mean: float
    first: float
    last: float
    change_pct: float
    peak_time: str
    trend: Literal["rising", "falling", "flat"]


class BaselineComparison(BaseModel):
    model_config = ConfigDict(frozen=True)
    window_start: str
    window_end: str
    summary: MetricSummary | None = None
    insufficient_data: bool = False


class MetricsPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    service: str
    metric: str
    unit: str
    points: list[MetricPoint]
    summary: MetricSummary
    baseline: BaselineComparison


def _downsample_lttb(points: list[RawMetricPoint], threshold: int) -> list[RawMetricPoint]:
    """Downsample point series using Largest Triangle Three Buckets (LTTB)."""
    if len(points) <= threshold or threshold <= 2:
        return points

    sampled: list[RawMetricPoint] = [points[0]]
    every = (len(points) - 2) / (threshold - 2)
    a = 0

    for i in range(threshold - 2):
        # Calculate average point for the next bucket (c)
        avg_x = 0.0
        avg_y = 0.0
        c_start = int(math.floor((i + 1) * every)) + 1
        c_end = min(int(math.floor((i + 2) * every)) + 1, len(points))
        c_size = c_end - c_start
        if c_size <= 0:
            c_size = 1

        for c_idx in range(c_start, min(c_end, len(points))):
            avg_x += points[c_idx].timestamp.timestamp()
            avg_y += points[c_idx].value
        avg_x /= c_size
        avg_y /= c_size

        # Find point in current bucket that maximizes triangle area
        b_start = int(math.floor(i * every)) + 1
        b_end = int(math.floor((i + 1) * every)) + 1
        max_area = -1.0
        max_idx = b_start

        point_a = points[a]
        ax = point_a.timestamp.timestamp()
        ay = point_a.value

        for b_idx in range(b_start, min(b_end, len(points))):
            bx = points[b_idx].timestamp.timestamp()
            by = points[b_idx].value
            area = abs((ax - avg_x) * (by - ay) - (ax - bx) * (avg_y - ay)) * 0.5
            if area > max_area:
                max_area = area
                max_idx = b_idx

        sampled.append(points[max_idx])
        a = max_idx

    sampled.append(points[-1])
    return sampled


def _compute_summary(points: list[RawMetricPoint]) -> MetricSummary | None:
    if not points:
        return None

    values = [p.value for p in points]
    p_min = round(min(values), 4)
    p_max = round(max(values), 4)
    p_mean = round(sum(values) / len(values), 4)
    p_first = round(values[0], 4)
    p_last = round(values[-1], 4)

    # Change percentage
    if p_first != 0.0:
        change_pct = round(((p_last - p_first) / abs(p_first)) * 100.0, 2)
    else:
        change_pct = 0.0 if p_last == 0.0 else 100.0

    # Peak time
    peak_pt = max(points, key=lambda p: p.value)
    peak_time = peak_pt.timestamp.isoformat()

    # Trend calculation
    delta = p_last - p_first
    if abs(delta) <= 0.05 * (p_max - p_min if p_max > p_min else 1.0):
        trend: Literal["rising", "falling", "flat"] = "flat"
    elif delta > 0:
        trend = "rising"
    else:
        trend = "falling"

    return MetricSummary(
        min=p_min,
        max=p_max,
        mean=p_mean,
        first=p_first,
        last=p_last,
        change_pct=change_pct,
        peak_time=peak_time,
        trend=trend,
    )


def _compute_anomaly_notes(current: MetricSummary, baseline: MetricSummary | None) -> list[str]:
    notes: list[str] = []
    if baseline and baseline.mean > 0:
        ratio = current.last / baseline.mean
        if ratio >= 2.0:
            notes.append(f"last value is {ratio:.1f}x baseline mean")
        elif ratio <= 0.5:
            notes.append(f"last value dropped to {ratio:.1f}x baseline mean")
    elif baseline and baseline.mean == 0.0 and current.last > 0:
        notes.append(f"last value rose from 0 baseline to {current.last}")

    if current.trend == "rising" and current.change_pct >= 50.0:
        notes.append(f"rapid increase of {current.change_pct}% over window")
    return notes


@catch_tool_errors
async def get_metrics(
    ctx: ToolContext,
    service: str,
    metric: str,
    start: datetime | None = None,
    end: datetime | None = None,
    step_seconds: int | None = None,
    backend: MetricsBackend | None = None,
    store: EvidenceStore | None = None,
) -> ToolResult:
    """Query time-series metric for a service with automatic baseline and downsampling."""
    # 1. Validate service
    if service not in ctx.allowed_services:
        return access_denied(
            f"Service '{service}' is not in allowed services: {ctx.allowed_services}",
            remedy="Select an allowed service within the incident boundary.",
        )

    # 2. Validate metric enum
    if metric not in METRIC_UNITS:
        return invalid_argument(
            f"Metric '{metric}' is not a recognized metric enum.",
            details={"allowed_metrics": list(METRIC_UNITS.keys())},
            remedy=f"Choose a metric from {list(METRIC_UNITS.keys())}",
        )

    # 3. Validate and clamp window
    window_res = validate_and_clamp_window(
        requested_start=start,
        requested_end=end,
        context=ctx,
        max_span_seconds=tool_limits.max_window_seconds,
    )
    if isinstance(window_res, ToolError):
        return window_res

    w_start, w_end = window_res.start, window_res.end
    clamped_window = window_res

    # Automatic step selection to produce <= 30 points
    window_duration = (w_end - w_start).total_seconds()
    if step_seconds is None:
        step_seconds = max(15, int(math.ceil(window_duration / MAX_OUTPUT_POINTS)))

    # 4. Query current window from backend
    if backend is None:
        backend = get_backend()

    try:
        current_series_list = await backend.query_metric(
            service=service,
            metric=metric,
            start_time=w_start,
            end_time=w_end,
            step_seconds=step_seconds,
        )
    except Exception as exc:
        return source_unavailable(
            f"Failed to query metrics backend: {exc}",
            remedy="Check telemetry service status or retry.",
        )

    if not current_series_list or not any(s.points for s in current_series_list):
        return not_found(
            f"No metric data found for service '{service}' metric '{metric}'.",
            details={"service": service, "metric": metric},
            remedy="Verify that the service is running and emits this metric.",
        )

    # Aggregate points across series (or take the primary matching series)
    primary_series = current_series_list[0]
    raw_points = sorted(primary_series.points, key=lambda p: p.timestamp)

    # 5. Check staleness
    latest_pt = raw_points[-1]
    now_or_end = ctx.window_end
    age_seconds = (now_or_end - latest_pt.timestamp).total_seconds()
    if age_seconds > STALE_AFTER_SECONDS:
        return stale_data(
            f"Latest metric sample is {int(age_seconds)}s old (exceeds {STALE_AFTER_SECONDS}s threshold).",
            details={"age_seconds": age_seconds, "latest_timestamp": latest_pt.timestamp.isoformat()},
            remedy="Check if service or telemetry scraper is lagging.",
        )

    # 6. Query baseline window (equal length before start)
    base_start = w_start - (w_end - w_start)
    base_end = w_start
    try:
        base_series_list = await backend.query_metric(
            service=service,
            metric=metric,
            start_time=base_start,
            end_time=base_end,
            step_seconds=step_seconds,
        )
        base_points = sorted(base_series_list[0].points, key=lambda p: p.timestamp) if base_series_list else []
    except Exception:
        base_points = []

    # 7. Downsample and summarize
    downsampled = _downsample_lttb(raw_points, MAX_OUTPUT_POINTS)
    summary = _compute_summary(downsampled)
    if summary is None:
        return not_found("Unable to compute metric summary.")

    base_summary = _compute_summary(base_points) if base_points else None
    baseline = BaselineComparison(
        window_start=base_start.isoformat(),
        window_end=base_end.isoformat(),
        summary=base_summary,
        insufficient_data=(base_summary is None or len(base_points) < 2),
    )

    notes = _compute_anomaly_notes(summary, base_summary)

    formatted_points = [
        MetricPoint(t=p.timestamp.isoformat(), v=round(p.value, 4))
        for p in downsampled
    ]

    unit_val = METRIC_UNITS[metric].value
    payload = MetricsPayload(
        service=service,
        metric=metric,
        unit=unit_val,
        points=formatted_points,
        summary=summary,
        baseline=baseline,
    )

    # 8. Deterministic Evidence ID & Store record
    normalized_args = {
        "end": w_end.isoformat(),
        "metric": metric,
        "service": service,
        "start": w_start.isoformat(),
        "step_seconds": step_seconds,
    }
    source_version = "v1"
    evidence_id = make_evidence_id(
        kind="metric",
        normalized_args=normalized_args,
        window=clamped_window,
        source_version=source_version,
    )

    source_id = f"prometheus:{service}:{metric}"
    record = EvidenceRecord(
        evidence_id=evidence_id,
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        tool="get_metrics",
        source_id=source_id,
        captured_at=datetime.now(timezone.utc),
        window_start=w_start,
        window_end=w_end,
        args=normalized_args,
        summary=f"{metric} for {service}: {summary.trend} from {summary.first} to {summary.last} (peak {summary.max})",
        payload=payload.model_dump(),
        source_version=source_version,
    )

    if store is not None:
        try:
            store.put(record)
        except Exception as exc:
            return source_unavailable(
                f"Failed to persist evidence record: {exc}",
                remedy="Check evidence storage subsystem.",
            )

    now_utc = datetime.now(timezone.utc)
    return ToolSuccess[MetricsPayload](
        evidence_id=evidence_id,
        tool="get_metrics",
        captured_at=now_utc,
        window=clamped_window,
        source=source_id,
        units=METRIC_UNITS[metric],
        data=payload,
        truncated=False,
        notes=notes,
    )

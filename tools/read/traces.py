"""get_trace tool implementation.

Supports two modes:
1. Target Trace Mode (trace_id provided):
   Returns structured, capped (<= 40 spans with sibling collapsing) span tree,
   computed critical path, slowest span, db.query count, and per-dependency time breakdown.
2. Candidate Search Mode (service + window filters):
   Returns up to `limit` (max 5) candidate trace summaries (slowest or errored).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from tools.common.errors import (
    access_denied,
    catch_tool_errors,
    invalid_argument,
    not_found,
    out_of_window,
    source_unavailable,
)
from tools.common.evidence import make_evidence_id
from tools.common.evidence_store import EvidenceRecord, EvidenceStore
from tools.common.limits import tool_limits
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolResult, ToolSuccess
from tools.common.sanitize import quarantine, redact
from tools.common.window import validate_and_clamp_window
from tools.read.backends.base import RawSpan, RawTrace, TraceBackend
from tools.read.backends.factory import get_backend

# ---------------------------------------------------------------------------
# Allowlisted Span Attributes
# ---------------------------------------------------------------------------
ALLOWLISTED_ATTRS = {
    "http.status_code",
    "http.method",
    "http.route",
    "db.system",
    "db.operation",
    "db.table",
    "db.pool.wait_ms",
    "peer.service",
    "cache.hit",
    "error",
    "error.type",
    "timeout.ms",
}

MAX_SPANS_IN_TREE = 40


class SpanNode(BaseModel):
    model_config = ConfigDict(frozen=True)
    span_id: str
    parent_id: str | None = None
    service: str
    name: str
    version: str | None = None
    start_offset_ms: float
    duration_ms: float
    status: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    collapsed_count: int = 1


class TraceSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    total_duration_ms: float
    slowest_span: str
    critical_path: list[str]
    error_spans: list[str]
    db_query_count: int
    dependency_duration_ms: dict[str, float]
    missing_parent_spans: bool = False


class TraceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    trace_id: str
    service: str
    start_time: str
    duration_ms: float
    has_error: bool
    summary: str


class TracePayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    mode: Literal["full_trace", "candidates"]
    trace_id: str | None = None
    spans: list[SpanNode] | None = None
    summary: TraceSummary | None = None
    candidates: list[TraceCandidate] | None = None


def _sanitize_attributes(attrs: dict[str, Any], origin: str) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for k, v in attrs.items():
        if k in ALLOWLISTED_ATTRS:
            if isinstance(v, str):
                redacted = redact(v)
                if any(delim in redacted for delim in ("<", ">", "&")):
                    q = quarantine(redacted, origin=origin)
                    cleaned[k] = q.text
                else:
                    cleaned[k] = redacted
            else:
                cleaned[k] = v
    return cleaned


def _collapse_spans(spans: list[SpanNode], max_spans: int) -> list[SpanNode]:
    """Collapse repeated adjacent sibling spans with same name and service."""
    if len(spans) <= max_spans:
        return spans

    collapsed: list[SpanNode] = []
    i = 0
    while i < len(spans):
        curr = spans[i]
        j = i + 1
        # Look ahead for identical siblings (same parent, service, and name)
        while (
            j < len(spans)
            and spans[j].parent_id == curr.parent_id
            and spans[j].service == curr.service
            and spans[j].name == curr.name
            and spans[j].status == curr.status
        ):
            j += 1

        count = j - i
        if count > 1:
            total_dur = sum(s.duration_ms for s in spans[i:j])
            collapsed.append(
                SpanNode(
                    span_id=curr.span_id,
                    parent_id=curr.parent_id,
                    service=curr.service,
                    name=f"{count} x {curr.name}",
                    version=curr.version,
                    start_offset_ms=curr.start_offset_ms,
                    duration_ms=round(total_dur, 2),
                    status=curr.status,
                    attributes={**curr.attributes, "repeat_count": count},
                    collapsed_count=count,
                )
            )
        else:
            collapsed.append(curr)
        i = j

    # If still exceeding cap, take top by duration
    if len(collapsed) > max_spans:
        collapsed = sorted(collapsed, key=lambda s: s.duration_ms, reverse=True)[:max_spans]
        collapsed = sorted(collapsed, key=lambda s: s.start_offset_ms)

    return collapsed


def _compute_trace_summary(spans: list[RawSpan]) -> TraceSummary:
    if not spans:
        return TraceSummary(
            total_duration_ms=0.0,
            slowest_span="",
            critical_path=[],
            error_spans=[],
            db_query_count=0,
            dependency_duration_ms={},
        )

    min_start = min(s.start_time for s in spans)
    max_end = max(s.end_time for s in spans)
    total_dur_ms = round((max_end - min_start).total_seconds() * 1000.0, 2)

    # Compute span durations for leaf/child spans (prefer child/leaf for slowest span)
    durations = {
        s.span_id: (s.end_time - s.start_time).total_seconds() * 1000.0
        for s in spans
    }
    # To identify root cause bottleneck, find slowest leaf or child span if available
    child_spans = [s for s in spans if s.parent_span_id is not None]
    candidate_spans = child_spans if child_spans else spans
    slowest_span_obj = max(candidate_spans, key=lambda s: durations[s.span_id])
    slowest_desc = f"{slowest_span_obj.service_name}:{slowest_span_obj.name} ({durations[slowest_span_obj.span_id]:.1f}ms)"

    # Error spans
    error_spans = [
        f"{s.service_name}:{s.name} [{s.status_code}]"
        for s in spans
        if s.status_code.upper() in {"ERROR", "500", "504"} or s.attributes.get("error") is True
    ]

    # DB query count
    db_count = sum(
        1
        for s in spans
        if "db" in s.name.lower() or s.attributes.get("db.operation") or s.attributes.get("db.system")
    )

    # Dependency duration by peer.service
    dep_durations: dict[str, float] = {}
    for s in spans:
        peer = s.attributes.get("peer.service")
        if peer:
            dep_durations[peer] = round(
                dep_durations.get(peer, 0.0) + durations[s.span_id], 2
            )

    # Critical path: follow chain of longest overlapping child spans from root
    span_map = {s.span_id: s for s in spans}
    roots = [s for s in spans if not s.parent_span_id or s.parent_span_id not in span_map]
    missing_parents = any(s.parent_span_id and s.parent_span_id not in span_map for s in spans)

    critical_path: list[str] = []
    if roots:
        curr = max(roots, key=lambda s: durations[s.span_id])
        critical_path.append(f"{curr.service_name}:{curr.name}")
        while True:
            children = [s for s in spans if s.parent_span_id == curr.span_id]
            if not children:
                break
            curr = max(children, key=lambda s: durations[s.span_id])
            critical_path.append(f"{curr.service_name}:{curr.name}")

    return TraceSummary(
        total_duration_ms=total_dur_ms,
        slowest_span=slowest_desc,
        critical_path=critical_path,
        error_spans=error_spans,
        db_query_count=db_count,
        dependency_duration_ms=dep_durations,
        missing_parent_spans=missing_parents,
    )


@catch_tool_errors
async def get_trace(
    ctx: ToolContext,
    start: datetime | None = None,
    end: datetime | None = None,
    trace_id: str | None = None,
    service: str | None = None,
    min_duration_ms: float | None = None,
    errors_only: bool = False,
    limit: int = 3,
    backend: TraceBackend | None = None,
    store: EvidenceStore | None = None,
) -> ToolResult:
    """Retrieve a full span tree by trace_id or query candidate trace IDs with filters."""
    # Enforce mutual exclusivity
    if trace_id is not None and service is not None:
        return invalid_argument(
            "Provide either trace_id for full trace retrieval OR service + window filters for candidate search, not both."
        )

    if trace_id is None and service is None:
        return invalid_argument(
            "Must provide either trace_id OR service."
        )

    # Validate window
    window_res = validate_and_clamp_window(
        requested_start=start,
        requested_end=end,
        context=ctx,
        max_span_seconds=tool_limits.max_window_seconds,
    )
    if isinstance(window_res, ToolError):
        return window_res

    w_start, w_end = window_res.start, window_res.end

    if backend is None:
        backend = get_backend()

    # -----------------------------------------------------------------------
    # Mode 1: Fetch Full Trace by trace_id
    # -----------------------------------------------------------------------
    if trace_id is not None:
        try:
            raw_trace = await backend.get_trace(trace_id=trace_id)
        except Exception as exc:
            return source_unavailable(
                f"Failed to query trace backend: {exc}",
                remedy="Check telemetry store availability.",
            )

        if raw_trace is None or not raw_trace.spans:
            return not_found(
                f"Trace '{trace_id}' not found.",
                details={"trace_id": trace_id},
                remedy="Verify trace ID against candidate listing or log records.",
            )

        # Check window containment of trace root
        trace_start = min(s.start_time for s in raw_trace.spans)
        if trace_start < ctx.window_start or trace_start > ctx.window_end:
            return out_of_window(
                f"Trace '{trace_id}' start time {trace_start.isoformat()} falls outside the investigation window [{ctx.window_start.isoformat()}, {ctx.window_end.isoformat()}].",
                remedy="Adjust investigation window to cover the incident timeframe.",
            )

        # Check service authorization
        trace_services = {s.service_name for s in raw_trace.spans}
        if not trace_services.issubset(ctx.allowed_services):
            unauthorized = trace_services - ctx.allowed_services
            return access_denied(
                f"Trace spans across unauthorized services: {unauthorized}",
                remedy=f"Scope query to authorized services: {ctx.allowed_services}",
            )

        # Build raw span nodes
        raw_spans = sorted(raw_trace.spans, key=lambda s: s.start_time)
        root_start = raw_spans[0].start_time

        span_nodes: list[SpanNode] = []
        for s in raw_spans:
            offset_ms = round((s.start_time - root_start).total_seconds() * 1000.0, 2)
            dur_ms = round((s.end_time - s.start_time).total_seconds() * 1000.0, 2)
            sanitized_attrs = _sanitize_attributes(s.attributes, origin=f"trace:{s.service_name}")

            span_nodes.append(
                SpanNode(
                    span_id=s.span_id,
                    parent_id=s.parent_span_id,
                    service=s.service_name,
                    name=s.name,
                    version=s.attributes.get("version"),
                    start_offset_ms=offset_ms,
                    duration_ms=dur_ms,
                    status=s.status_code,
                    attributes=sanitized_attrs,
                )
            )

        # Apply collapsing rule (collapse repeated adjacent siblings)
        final_spans = _collapse_spans(span_nodes, MAX_SPANS_IN_TREE)
        summary = _compute_trace_summary(raw_spans)

        payload = TracePayload(
            mode="full_trace",
            trace_id=trace_id,
            spans=final_spans,
            summary=summary,
        )

        notes = []
        if summary.missing_parent_spans:
            notes.append("partial trace: missing parent span")
        if len(raw_spans) > len(final_spans):
            notes.append(f"collapsed {len(raw_spans) - len(final_spans)} repeated sibling spans")

        normalized_args = {"trace_id": trace_id}
        source_id = f"trace:{trace_id}"
        evidence_id = make_evidence_id(
            kind="trace",
            normalized_args=normalized_args,
            window=window_res,
            source_version="v1",
        )

        record = EvidenceRecord(
            evidence_id=evidence_id,
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            tool="get_trace",
            source_id=source_id,
            captured_at=datetime.now(timezone.utc),
            window_start=w_start,
            window_end=w_end,
            args=normalized_args,
            summary=f"Trace {trace_id} ({summary.total_duration_ms}ms) across {trace_services}. Slowest: {summary.slowest_span}",
            payload=payload.model_dump(),
        )
        if store is not None:
            store.put(record)

        return ToolSuccess[TracePayload](
            evidence_id=evidence_id,
            tool="get_trace",
            captured_at=datetime.now(timezone.utc),
            window=window_res,
            source=source_id,
            data=payload,
            notes=notes,
        )

    # -----------------------------------------------------------------------
    # Mode 2: Candidate Search Mode (service + window filters)
    # -----------------------------------------------------------------------
    assert service is not None
    if service not in ctx.allowed_services:
        return access_denied(
            f"Service '{service}' is not in allowed services: {ctx.allowed_services}"
        )

    clamped_limit = max(1, min(limit, 5))

    # Candidate search: inspect traces directory or index
    # In SnapshotBackend, traces are in snapshot_dir / traces / <id>.json
    candidates: list[TraceCandidate] = []
    traces_dir = getattr(backend, "root_dir", None)
    if traces_dir:
        t_dir = traces_dir / "traces"
        if t_dir.exists():
            for t_file in t_dir.glob("*.json"):
                try:
                    import json

                    t_data = json.loads(t_file.read_text(encoding="utf-8"))
                    spans_data = t_data.get("spans", [])
                    if not any(s.get("service_name") == service for s in spans_data):
                        continue

                    min_st = min(
                        datetime.fromisoformat(s["start_time"]) if isinstance(s["start_time"], str) else datetime.fromtimestamp(s["start_time"])
                        for s in spans_data
                    )
                    max_et = max(
                        datetime.fromisoformat(s["end_time"]) if isinstance(s["end_time"], str) else datetime.fromtimestamp(s["end_time"])
                        for s in spans_data
                    )
                    dur_ms = round((max_et - min_st).total_seconds() * 1000.0, 2)

                    if min_st < w_start or min_st > w_end:
                        continue

                    if min_duration_ms and dur_ms < min_duration_ms:
                        continue

                    has_err = any(
                        s.get("status_code", "").upper() in {"ERROR", "500", "504"}
                        for s in spans_data
                    )
                    if errors_only and not has_err:
                        continue

                    root_span = next(
                        (s for s in spans_data if not s.get("parent_span_id")),
                        spans_data[0] if spans_data else None,
                    )
                    root_name = root_span.get("name", "unknown") if root_span else "unknown"

                    candidates.append(
                        TraceCandidate(
                            trace_id=t_data.get("trace_id", t_file.stem),
                            service=service,
                            start_time=min_st.isoformat(),
                            duration_ms=dur_ms,
                            has_error=has_err,
                            summary=f"{service}:{root_name} took {dur_ms}ms (error={has_err})",
                        )
                    )
                except Exception:
                    continue

    # Order candidates by duration descending, pick top `clamped_limit`
    candidates = sorted(candidates, key=lambda c: c.duration_ms, reverse=True)[:clamped_limit]

    payload = TracePayload(
        mode="candidates",
        candidates=candidates,
    )

    normalized_args = {
        "end": w_end.isoformat(),
        "errors_only": errors_only,
        "limit": clamped_limit,
        "min_duration_ms": min_duration_ms or 0.0,
        "service": service,
        "start": w_start.isoformat(),
    }
    source_id = f"trace_candidates:{service}"
    evidence_id = make_evidence_id(
        kind="trace",
        normalized_args=normalized_args,
        window=window_res,
        source_version="v1",
    )

    record = EvidenceRecord(
        evidence_id=evidence_id,
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        tool="get_trace",
        source_id=source_id,
        captured_at=datetime.now(timezone.utc),
        window_start=w_start,
        window_end=w_end,
        args=normalized_args,
        summary=f"Found {len(candidates)} candidate traces for {service} (limit {clamped_limit}).",
        payload=payload.model_dump(),
    )
    if store is not None:
        store.put(record)

    return ToolSuccess[TracePayload](
        evidence_id=evidence_id,
        tool="get_trace",
        captured_at=datetime.now(timezone.utc),
        window=window_res,
        source=source_id,
        data=payload,
        notes=[f"returned top {len(candidates)} candidate traces"],
    )

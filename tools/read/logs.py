"""search_logs tool implementation.

Queries logs from configured backend, applies redaction and untrusted data quarantine,
performs whole-window statistical aggregation (counts by level/event, top 5 message templates),
samples ordered rows (first 5, last 5, evenly spaced), and persists evidence.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from tools.common.errors import (
    access_denied,
    catch_tool_errors,
    invalid_argument,
    not_found,
    source_unavailable,
)
from tools.common.evidence import make_evidence_id
from tools.common.evidence_store import EvidenceRecord, EvidenceStore
from tools.common.limits import tool_limits
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolResult, ToolSuccess
from tools.common.sanitize import quarantine, redact
from tools.common.window import validate_and_clamp_window
from tools.read.backends.base import LogsBackend, RawLogLine
from tools.read.backends.factory import get_backend

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
ALLOWED_LEVELS = {"error", "warn", "info", "any"}


class LogRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    ts: str
    level: str
    event: str
    msg: str
    trace_id: str | None = None
    request_id: str | None = None
    version: str | None = None


class TemplateCount(BaseModel):
    model_config = ConfigDict(frozen=True)
    template: str
    count: int
    first_seen: str
    last_seen: str


class LogAggregation(BaseModel):
    model_config = ConfigDict(frozen=True)
    counts_by_level: dict[str, int]
    counts_by_event: dict[str, int]
    top_templates: list[TemplateCount]
    sampling_rule: str = "first 5, last 5, rest evenly spaced up to limit"


class LogsPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    service: str
    total_matches: int
    rows: list[LogRow]
    aggregation: LogAggregation


# Template masking regex: mask UUIDs, hex IDs, and numbers with <ID> / <NUM>
_UUID_OR_HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_NUM_RE = re.compile(r"\b\d+\b")


def _template_message(msg: str) -> str:
    """Mask variable IDs and numbers to extract common message template."""
    t = _UUID_OR_HEX_RE.sub("<ID>", msg)
    t = _NUM_RE.sub("<NUM>", t)
    return t.strip()


def _sample_rows(all_rows: list[RawLogLine], limit: int) -> list[RawLogLine]:
    """Sample up to `limit` rows using: first 5, last 5, and rest evenly spaced."""
    total = len(all_rows)
    if total <= limit:
        return all_rows

    if limit <= 10:
        # If limit is small, take evenly spaced points
        step = total / limit
        return [all_rows[int(i * step)] for i in range(limit)]

    first_k = 5
    last_k = 5
    mid_k = limit - (first_k + last_k)

    first_part = all_rows[:first_k]
    last_part = all_rows[total - last_k :]

    # Sample remaining from the middle section
    middle_pool = all_rows[first_k : total - last_k]
    mid_part: list[RawLogLine] = []
    if middle_pool and mid_k > 0:
        step = len(middle_pool) / mid_k
        mid_part = [middle_pool[int(i * step)] for i in range(mid_k)]

    return first_part + mid_part + last_part


@catch_tool_errors
async def search_logs(
    ctx: ToolContext,
    service: str,
    start: datetime | None = None,
    end: datetime | None = None,
    level: str | None = None,
    contains: str | None = None,
    limit: int = 20,
    backend: LogsBackend | None = None,
    store: EvidenceStore | None = None,
) -> ToolResult:
    """Search and aggregate logs for a service within a bounded window."""
    # 1. Validate service
    if service not in ctx.allowed_services:
        return access_denied(
            f"Service '{service}' is not in allowed services: {ctx.allowed_services}",
            remedy="Select an allowed service within the incident boundary.",
        )

    # 2. Validate filter arguments
    level_filter: str | None = None
    if level is not None:
        lvl_clean = level.lower().strip()
        if lvl_clean not in ALLOWED_LEVELS:
            return invalid_argument(
                f"Invalid level '{level}'. Allowed levels: {sorted(ALLOWED_LEVELS)}",
                details={"allowed_levels": sorted(ALLOWED_LEVELS)},
            )
        if lvl_clean != "any":
            level_filter = lvl_clean.upper()

    if contains is not None:
        if len(contains) > 64:
            return invalid_argument("Contains query exceeds maximum length of 64 characters.")
        # Substring search only: reject regex syntax markers if detected
        contains = contains.strip()

    clamped_limit = max(1, min(limit, tool_limits.max_log_rows))

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

    # 4. Query backend for raw logs in window
    if backend is None:
        backend = get_backend()

    try:
        # Request a large candidate window from backend to perform full aggregation
        all_lines = await backend.search_logs(
            service=service,
            start_time=w_start,
            end_time=w_end,
            pattern=contains,
            level=level_filter,
            limit=10_000,
        )
    except Exception as exc:
        return source_unavailable(
            f"Failed to query logs backend: {exc}",
            remedy="Check telemetry volume or logging daemon.",
        )

    if all_lines is None:
        return not_found(
            f"No log stream or volume found for service '{service}'.",
            details={"service": service},
            remedy="Verify service deployment and log volume mounts.",
        )

    total_matches = len(all_lines)

    # 5. Whole-window aggregation
    level_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    template_counter: Counter[str] = Counter()
    template_first_seen: dict[str, str] = {}
    template_last_seen: dict[str, str] = {}

    for row in all_lines:
        lvl = row.level.upper()
        level_counts[lvl] += 1

        attrs = row.attributes or {}
        event_name = attrs.get("event", row.level.lower())
        event_counts[event_name] += 1

        tmpl = _template_message(redact(row.message))
        template_counter[tmpl] += 1

        ts_str = row.timestamp.isoformat()
        if tmpl not in template_first_seen:
            template_first_seen[tmpl] = ts_str
        template_last_seen[tmpl] = ts_str

    top_templates = [
        TemplateCount(
            template=tmpl,
            count=cnt,
            first_seen=template_first_seen[tmpl],
            last_seen=template_last_seen[tmpl],
        )
        for tmpl, cnt in template_counter.most_common(5)
    ]

    aggregation = LogAggregation(
        counts_by_level=dict(level_counts),
        counts_by_event=dict(event_counts),
        top_templates=top_templates,
    )

    # 6. Sample ordered rows (first 5, last 5, evenly spaced)
    sampled = _sample_rows(all_lines, clamped_limit)
    is_truncated = total_matches > len(sampled)

    # 7. Redact, Quarantine & construct rows
    evidence_id_dummy = "pending"
    output_rows: list[LogRow] = []
    collected_notes: list[str] = []

    for item in sampled:
        attrs = item.attributes or {}
        # Redact raw message
        redacted_msg = redact(item.message)
        # Quarantine untrusted log message
        q_res = quarantine(
            redacted_msg,
            origin=f"log:{service}",
            evidence_id=evidence_id_dummy,
            max_chars=tool_limits.max_log_chars_per_line,
        )
        if q_res.notes:
            for n in q_res.notes:
                if n not in collected_notes:
                    collected_notes.append(n)

        output_rows.append(
            LogRow(
                ts=item.timestamp.isoformat(),
                level=item.level,
                event=attrs.get("event", item.level.lower()),
                msg=q_res.text,
                trace_id=attrs.get("trace_id"),
                request_id=attrs.get("request_id"),
                version=attrs.get("version"),
            )
        )

    # 8. Deterministic Evidence ID & Persistence
    normalized_args = {
        "contains": contains or "",
        "end": w_end.isoformat(),
        "level": level or "any",
        "limit": clamped_limit,
        "service": service,
        "start": w_start.isoformat(),
    }
    source_version = "v1"
    evidence_id = make_evidence_id(
        kind="logs",
        normalized_args=normalized_args,
        window=window_res,
        source_version=source_version,
    )

    # Update evidence_id inside quarantined envelopes
    final_rows: list[LogRow] = [
        LogRow(
            ts=r.ts,
            level=r.level,
            event=r.event,
            msg=r.msg.replace('id="pending"', f'id="{evidence_id}"'),
            trace_id=r.trace_id,
            request_id=r.request_id,
            version=r.version,
        )
        for r in output_rows
    ]

    payload = LogsPayload(
        service=service,
        total_matches=total_matches,
        rows=final_rows,
        aggregation=aggregation,
    )

    source_id = f"logs:{service}"
    record = EvidenceRecord(
        evidence_id=evidence_id,
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        tool="search_logs",
        source_id=source_id,
        captured_at=datetime.now(timezone.utc),
        window_start=w_start,
        window_end=w_end,
        args=normalized_args,
        summary=f"Found {total_matches} logs for {service} (returned {len(final_rows)}). Levels: {dict(level_counts)}",
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

    return ToolSuccess[LogsPayload](
        evidence_id=evidence_id,
        tool="search_logs",
        captured_at=datetime.now(timezone.utc),
        window=window_res,
        source=source_id,
        units=None,
        data=payload,
        truncated=is_truncated,
        notes=collected_notes,
    )

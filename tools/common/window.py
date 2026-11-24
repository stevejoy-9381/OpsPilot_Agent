"""Window validation and clamping helpers for investigation tools."""

from datetime import datetime, timezone

from tools.common.models import ErrorCode, ToolContext, ToolError, ToolErrorDetail, Window


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC timezone-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_and_clamp_window(
    requested_start: datetime | None,
    requested_end: datetime | None,
    context: ToolContext,
    max_span_seconds: int | None = None,
    step_seconds: int | None = None,
) -> Window | ToolError:
    """Validate requested time window against ToolContext and limits.

    Rules:
    1. Defaults omitted boundaries to context.window_start / context.window_end.
    2. Rejects reversed or zero windows (start >= end) with invalid_argument.
    3. Rejects any requested timestamp after context.window_end with out_of_window.
    4. Clamps start time to context.window_start if requested start is earlier.
    5. Rejects windows that lie entirely before context.window_start with out_of_window.
    6. Enforces max_span_seconds limit per tool with invalid_argument.

    Returns:
        Window on success, or ToolError on validation failure.
    """
    ctx_start = _ensure_utc(context.window_start)
    ctx_end = _ensure_utc(context.window_end)

    req_start = _ensure_utc(requested_start) if requested_start is not None else ctx_start
    req_end = _ensure_utc(requested_end) if requested_end is not None else ctx_end

    # Check reversed or zero window
    if req_start >= req_end:
        return ToolError(
            error=ToolErrorDetail(
                code=ErrorCode.INVALID_ARGUMENT,
                message=(
                    f"Window start ({req_start.isoformat()}) must be strictly before "
                    f"window end ({req_end.isoformat()})."
                ),
                details={
                    "requested_start": req_start.isoformat(),
                    "requested_end": req_end.isoformat(),
                },
                remedy="Specify a start_time strictly earlier than end_time.",
            )
        )

    # Reject anything after window_end
    if req_start > ctx_end:
        return ToolError(
            error=ToolErrorDetail(
                code=ErrorCode.OUT_OF_WINDOW,
                message=(
                    f"Requested start_time ({req_start.isoformat()}) is after "
                    f"incident window_end ({ctx_end.isoformat()})."
                ),
                details={
                    "requested_start": req_start.isoformat(),
                    "window_end": ctx_end.isoformat(),
                },
                remedy="Constrain query window to fall on or before incident window_end.",
            )
        )

    if req_end > ctx_end:
        return ToolError(
            error=ToolErrorDetail(
                code=ErrorCode.OUT_OF_WINDOW,
                message=(
                    f"Requested end_time ({req_end.isoformat()}) is after "
                    f"incident window_end ({ctx_end.isoformat()})."
                ),
                details={
                    "requested_end": req_end.isoformat(),
                    "window_end": ctx_end.isoformat(),
                },
                remedy="Constrain query window to fall on or before incident window_end.",
            )
        )

    # Reject window entirely before context.window_start
    if req_end <= ctx_start:
        return ToolError(
            error=ToolErrorDetail(
                code=ErrorCode.OUT_OF_WINDOW,
                message=(
                    f"Requested window ({req_start.isoformat()} to {req_end.isoformat()}) "
                    f"lies entirely before incident window_start ({ctx_start.isoformat()})."
                ),
                details={
                    "requested_start": req_start.isoformat(),
                    "requested_end": req_end.isoformat(),
                    "window_start": ctx_start.isoformat(),
                },
                remedy="Adjust query window to overlap with [window_start, window_end].",
            )
        )

    # Clamp start to context.window_start if earlier
    clamped_start = max(req_start, ctx_start)
    clamped_end = req_end

    # Enforce max span
    if max_span_seconds is not None:
        span_s = (clamped_end - clamped_start).total_seconds()
        if span_s > max_span_seconds:
            return ToolError(
                error=ToolErrorDetail(
                    code=ErrorCode.INVALID_ARGUMENT,
                    message=(
                        f"Requested window span of {int(span_s)}s exceeds "
                        f"maximum allowed span of {max_span_seconds}s."
                    ),
                    details={
                        "span_seconds": span_s,
                        "max_span_seconds": max_span_seconds,
                    },
                    remedy=f"Reduce requested window span to at most {max_span_seconds} seconds.",
                )
            )

    return Window(start=clamped_start, end=clamped_end, step_seconds=step_seconds)

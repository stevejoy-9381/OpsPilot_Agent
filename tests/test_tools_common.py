"""Unit tests for tools/common shared models, bounds, evidence hashing, and error boundaries."""

import json
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from tools.common import (
    ErrorCode,
    EvidenceRef,
    ToolContext,
    ToolError,
    ToolErrorDetail,
    ToolLimitsConfig,
    ToolResult,
    ToolSuccess,
    Unit,
    Window,
    access_denied,
    budget_exhausted,
    catch_tool_errors,
    invalid_argument,
    make_evidence_id,
    not_found,
    out_of_window,
    redact_sensitive,
    source_unavailable,
    stale_data,
    tool_limits,
    validate_and_clamp_window,
)


@pytest.fixture
def sample_context() -> ToolContext:
    return ToolContext(
        run_id="run-test-1234",
        tenant_id="tenant-alpha",
        incident_id="inc-2026-11-24-001",
        window_start=datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 11, 24, 8, 30, 0, tzinfo=timezone.utc),
        allowed_services=["gateway", "orders", "inventory"],
        remaining_calls=8,
    )


# -----------------------------------------------------------------------------
# 1. Evidence ID Determinism & Stability Tests
# -----------------------------------------------------------------------------


def test_evidence_id_format() -> None:
    window = Window(
        start=datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 24, 8, 15, 0, tzinfo=timezone.utc),
        step_seconds=15,
    )
    ev_id = make_evidence_id("metric", {"service": "orders", "metric": "error_rate"}, window)
    assert ev_id.startswith("ev_metric_")
    hex_part = ev_id.replace("ev_metric_", "")
    assert len(hex_part) == 8
    int(hex_part, 16)  # Must be valid hex


def test_evidence_id_stability_equivalent_args() -> None:
    """Equivalent args (different key order, float precision, equivalent tz) yield identical IDs."""
    window1 = Window(
        start=datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 24, 8, 15, 0, tzinfo=timezone.utc),
        step_seconds=15,
    )
    # Equivalent timezone representations
    window2 = Window(
        start=datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 24, 8, 15, 0, tzinfo=timezone.utc),
        step_seconds=15,
    )

    args_a = {
        "service": "orders",
        "metric": "error_rate",
        "threshold": 0.5000000001,
        "nested": {"b": 2, "a": 1},
    }
    args_b = {
        "metric": "error_rate",
        "service": "orders",
        "threshold": 0.5,
        "nested": {"a": 1, "b": 2},
    }

    id_a = make_evidence_id("metric", args_a, window1, source_version="v1.0.0")
    id_b = make_evidence_id("metric", args_b, window2, source_version="v1.0.0")

    assert id_a == id_b


def test_evidence_id_different_for_different_args() -> None:
    window = Window(
        start=datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 24, 8, 15, 0, tzinfo=timezone.utc),
    )

    id_base = make_evidence_id("metric", {"service": "orders"}, window)
    id_diff_service = make_evidence_id("metric", {"service": "inventory"}, window)
    id_diff_kind = make_evidence_id("log", {"service": "orders"}, window)
    id_diff_version = make_evidence_id("metric", {"service": "orders"}, window, source_version="v2")

    window_diff = Window(
        start=datetime(2026, 11, 24, 8, 5, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 24, 8, 20, 0, tzinfo=timezone.utc),
    )
    id_diff_window = make_evidence_id("metric", {"service": "orders"}, window_diff)

    assert id_base != id_diff_service
    assert id_base != id_diff_kind
    assert id_base != id_diff_version
    assert id_base != id_diff_window


# -----------------------------------------------------------------------------
# 2. Window Clamping & Validation Table-Driven Tests
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "req_start, req_end, max_span, expected_ok, expected_code, expected_start, expected_end",
    [
        # Normal inside window
        (
            datetime(2026, 11, 24, 8, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 15, 0, tzinfo=timezone.utc),
            None,
            True,
            None,
            datetime(2026, 11, 24, 8, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 15, 0, tzinfo=timezone.utc),
        ),
        # Default omitted to context boundaries
        (
            None,
            None,
            None,
            True,
            None,
            datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 30, 0, tzinfo=timezone.utc),
        ),
        # Clamp start earlier than context window_start
        (
            datetime(2026, 11, 24, 7, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 10, 0, tzinfo=timezone.utc),
            None,
            True,
            None,
            datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 10, 0, tzinfo=timezone.utc),
        ),
        # Reversed window (start > end) -> invalid_argument
        (
            datetime(2026, 11, 24, 8, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 10, 0, tzinfo=timezone.utc),
            None,
            False,
            ErrorCode.INVALID_ARGUMENT,
            None,
            None,
        ),
        # Zero duration window (start == end) -> invalid_argument
        (
            datetime(2026, 11, 24, 8, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 10, 0, tzinfo=timezone.utc),
            None,
            False,
            ErrorCode.INVALID_ARGUMENT,
            None,
            None,
        ),
        # Start after context window_end -> out_of_window
        (
            datetime(2026, 11, 24, 8, 35, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 45, 0, tzinfo=timezone.utc),
            None,
            False,
            ErrorCode.OUT_OF_WINDOW,
            None,
            None,
        ),
        # End after context window_end -> out_of_window
        (
            datetime(2026, 11, 24, 8, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 35, 0, tzinfo=timezone.utc),
            None,
            False,
            ErrorCode.OUT_OF_WINDOW,
            None,
            None,
        ),
        # Window entirely before context window_start -> out_of_window
        (
            datetime(2026, 11, 24, 7, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 7, 50, 0, tzinfo=timezone.utc),
            None,
            False,
            ErrorCode.OUT_OF_WINDOW,
            None,
            None,
        ),
        # Max span exceeded -> invalid_argument
        (
            datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 25, 0, tzinfo=timezone.utc),
            900,  # 15 minutes limit, requested is 25 minutes
            False,
            ErrorCode.INVALID_ARGUMENT,
            None,
            None,
        ),
        # Max span respected -> success
        (
            datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 10, 0, tzinfo=timezone.utc),
            900,  # 15 minutes limit, requested is 10 minutes
            True,
            None,
            datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 24, 8, 10, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_validate_and_clamp_window_table(
    sample_context: ToolContext,
    req_start: datetime | None,
    req_end: datetime | None,
    max_span: int | None,
    expected_ok: bool,
    expected_code: ErrorCode | None,
    expected_start: datetime | None,
    expected_end: datetime | None,
) -> None:
    res = validate_and_clamp_window(
        requested_start=req_start,
        requested_end=req_end,
        context=sample_context,
        max_span_seconds=max_span,
    )

    if expected_ok:
        assert isinstance(res, Window)
        assert res.start == expected_start
        assert res.end == expected_end
    else:
        assert isinstance(res, ToolError)
        assert res.ok is False
        assert res.error.code == expected_code


# -----------------------------------------------------------------------------
# 3. ToolError Serialization & Discriminated Union Tests
# -----------------------------------------------------------------------------


def test_tool_error_serialization() -> None:
    err = ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.OUT_OF_WINDOW,
            message="Requested timestamp is after window_end",
            details={"requested": "2026-11-24T09:00:00Z"},
            remedy="Constrain query to incident window",
        )
    )
    raw_json = err.model_dump_json()
    data = json.loads(raw_json)

    assert data["ok"] is False
    assert data["error"]["code"] == "out_of_window"
    assert data["error"]["message"] == "Requested timestamp is after window_end"
    assert data["error"]["details"]["requested"] == "2026-11-24T09:00:00Z"
    assert data["error"]["remedy"] == "Constrain query to incident window"


def test_discriminated_union_parsing() -> None:
    adapter: TypeAdapter[ToolResult] = TypeAdapter(ToolResult)

    # Validate ToolError
    err_json = {
        "ok": False,
        "error": {
            "code": "access_denied",
            "message": "Service billing not authorized",
            "details": {},
            "remedy": "Confine query to allowed services",
        },
    }
    parsed_err = adapter.validate_python(err_json)
    assert isinstance(parsed_err, ToolError)
    assert parsed_err.ok is False
    assert parsed_err.error.code == ErrorCode.ACCESS_DENIED

    # Validate ToolSuccess
    success_json = {
        "ok": True,
        "evidence_id": "ev_metric_12345678",
        "tool": "get_metrics",
        "captured_at": "2026-11-24T08:10:00Z",
        "window": {
            "start": "2026-11-24T08:00:00Z",
            "end": "2026-11-24T08:10:00Z",
            "step_seconds": 15,
        },
        "source": "prometheus:orders",
        "units": "ratio",
        "data": {"points": [0.01, 0.02]},
        "truncated": False,
        "notes": [],
    }
    parsed_success = adapter.validate_python(success_json)
    assert isinstance(parsed_success, ToolSuccess)
    assert parsed_success.ok is True
    assert parsed_success.evidence_id == "ev_metric_12345678"
    assert parsed_success.units == Unit.RATIO


# -----------------------------------------------------------------------------
# 4. ToolContext Immutability (Frozen) Tests
# -----------------------------------------------------------------------------


def test_tool_context_immutability(sample_context: ToolContext) -> None:
    with pytest.raises(ValidationError):
        sample_context.remaining_calls = 5

    with pytest.raises(ValidationError):
        sample_context.run_id = "modified-id"

    with pytest.raises(ValidationError):
        sample_context.allowed_services = ["other"]


# -----------------------------------------------------------------------------
# 5. Redaction and Tool Boundary Decorator Tests
# -----------------------------------------------------------------------------


def test_redact_sensitive() -> None:
    raw = (
        "Failed request with Bearer secret-token-abc12345 and "
        "X-Lab-Admin-Token: super_secret_token_123 and password=superpassword."
    )
    redacted = redact_sensitive(raw)
    assert "secret-token-abc12345" not in redacted
    assert "super_secret_token_123" not in redacted
    assert "superpassword" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_catch_tool_errors_sync() -> None:
    @catch_tool_errors
    def failing_tool(arg: str) -> ToolResult:
        raise ConnectionResetError(f"Connection reset with token=secret-tok-12345678 for {arg}")

    result = failing_tool("orders")
    assert isinstance(result, ToolError)
    assert result.ok is False
    assert result.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "secret-tok-12345678" not in result.error.message
    assert "[REDACTED_SECRET]" in result.error.message


@pytest.mark.asyncio
async def test_catch_tool_errors_async() -> None:
    @catch_tool_errors
    async def failing_async_tool(arg: str) -> ToolResult:
        raise TimeoutError(f"HTTP timeout on Bearer tok-secret-999 for {arg}")

    result = await failing_async_tool("inventory")
    assert isinstance(result, ToolError)
    assert result.ok is False
    assert result.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "tok-secret-999" not in result.error.message
    assert "[REDACTED_SECRET]" in result.error.message


# -----------------------------------------------------------------------------
# 6. Standard Error Factory Helpers
# -----------------------------------------------------------------------------


def test_error_factories() -> None:
    err_inv = invalid_argument("bad argument")
    assert err_inv.error.code == ErrorCode.INVALID_ARGUMENT

    err_oow = out_of_window("out of bounds")
    assert err_oow.error.code == ErrorCode.OUT_OF_WINDOW

    err_nf = not_found("entity missing")
    assert err_nf.error.code == ErrorCode.NOT_FOUND

    err_stale = stale_data("cache expired")
    assert err_stale.error.code == ErrorCode.STALE_DATA

    err_su = source_unavailable("downstream down")
    assert err_su.error.code == ErrorCode.SOURCE_UNAVAILABLE

    err_be = budget_exhausted("no calls left")
    assert err_be.error.code == ErrorCode.BUDGET_EXHAUSTED

    err_ad = access_denied("service unauthorized")
    assert err_ad.error.code == ErrorCode.ACCESS_DENIED


# -----------------------------------------------------------------------------
# 7. Units Enum & Limits Configuration Tests
# -----------------------------------------------------------------------------


def test_units_enum() -> None:
    assert Unit.RATIO.value == "ratio"
    assert Unit.REQUESTS_PER_SECOND.value == "requests_per_second"
    assert Unit.SECONDS.value == "seconds"
    assert Unit.COUNT.value == "count"
    assert Unit.PERCENT.value == "percent"


def test_limits_config() -> None:
    assert tool_limits.max_window_seconds == 3600
    assert tool_limits.max_metric_points == 30
    assert tool_limits.max_log_rows == 50
    assert tool_limits.default_log_rows == 25
    assert tool_limits.max_diff_chars == 4000
    assert tool_limits.default_diff_chars == 2000
    assert tool_limits.target_token_budget == 1500
    assert tool_limits.max_token_budget == 2500

    custom = ToolLimitsConfig(max_metric_points=60)
    assert custom.max_metric_points == 60


def test_evidence_ref_model() -> None:
    ref = EvidenceRef(
        evidence_id="ev_metric_12345678",
        kind="metric",
        source="prometheus:orders",
        captured_at=datetime(2026, 11, 24, 8, 0, 0, tzinfo=timezone.utc),
    )
    assert ref.evidence_id == "ev_metric_12345678"
    assert ref.kind == "metric"

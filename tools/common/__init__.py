"""Tools common package: shared models, bounds, evidence hashing, and error handling."""

from tools.common.errors import (
    access_denied,
    budget_exhausted,
    catch_tool_errors,
    invalid_argument,
    not_found,
    out_of_window,
    redact_sensitive,
    source_unavailable,
    stale_data,
)
from tools.common.evidence import make_evidence_id
from tools.common.limits import ToolLimitsConfig, tool_limits
from tools.common.models import (
    ErrorCode,
    EvidenceRef,
    ToolContext,
    ToolError,
    ToolErrorDetail,
    ToolResult,
    ToolSuccess,
    Unit,
    Window,
)
from tools.common.window import validate_and_clamp_window

__all__ = [
    # Models & Enums
    "ErrorCode",
    "Unit",
    "Window",
    "EvidenceRef",
    "ToolContext",
    "ToolErrorDetail",
    "ToolError",
    "ToolSuccess",
    "ToolResult",
    # Limits
    "ToolLimitsConfig",
    "tool_limits",
    # Evidence ID
    "make_evidence_id",
    # Window validation
    "validate_and_clamp_window",
    # Errors & Boundary
    "catch_tool_errors",
    "redact_sensitive",
    "invalid_argument",
    "out_of_window",
    "not_found",
    "stale_data",
    "source_unavailable",
    "budget_exhausted",
    "access_denied",
]

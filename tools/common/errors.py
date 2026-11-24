"""Error boundary, redaction, and error factories for investigation tools."""

import functools
import inspect
import logging
import re
from typing import Any, Callable, TypeVar

from tools.common.models import ErrorCode, ToolError, ToolErrorDetail

logger = logging.getLogger("opspilot.tools")

# Sensitive patterns to redact from logs and messages
_REDACTION_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"(?i)(x-lab-admin-token\s*[:=]\s*)[^\s,]+", re.IGNORECASE),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,]+", re.IGNORECASE),
    re.compile(r"(?i)(secret\s*[:=]\s*)[^\s,]+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,]+", re.IGNORECASE),
    re.compile(r"(?i)(token\s*[:=]\s*['\"]?)[A-Za-z0-9_\-\.]{8,}['\"]?", re.IGNORECASE),
]


def redact_sensitive(text: str) -> str:
    """Redact passwords, secret tokens, and auth headers from text."""
    redacted = text
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED_SECRET]", redacted)
    return redacted


def invalid_argument(
    message: str, details: dict[str, Any] | None = None, remedy: str = ""
) -> ToolError:
    """Create a ToolError with code invalid_argument."""
    return ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.INVALID_ARGUMENT,
            message=redact_sensitive(message),
            details=details or {},
            remedy=remedy or "Check parameter types, valid enums, and bounds.",
        )
    )


def out_of_window(
    message: str, details: dict[str, Any] | None = None, remedy: str = ""
) -> ToolError:
    """Create a ToolError with code out_of_window."""
    return ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.OUT_OF_WINDOW,
            message=redact_sensitive(message),
            details=details or {},
            remedy=remedy or (
                "Constrain query time range to fall within [window_start, window_end]."
            ),
        )
    )


def not_found(
    message: str, details: dict[str, Any] | None = None, remedy: str = ""
) -> ToolError:
    """Create a ToolError with code not_found."""
    return ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.NOT_FOUND,
            message=redact_sensitive(message),
            details=details or {},
            remedy=remedy or "Verify the identifier against prior listing queries.",
        )
    )


def stale_data(
    message: str, details: dict[str, Any] | None = None, remedy: str = ""
) -> ToolError:
    """Create a ToolError with code stale_data."""
    return ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.STALE_DATA,
            message=redact_sensitive(message),
            details=details or {},
            remedy=remedy or "Refresh listings or re-query recent metrics.",
        )
    )


def source_unavailable(
    message: str, details: dict[str, Any] | None = None, remedy: str = ""
) -> ToolError:
    """Create a ToolError with code source_unavailable."""
    return ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.SOURCE_UNAVAILABLE,
            message=redact_sensitive(message),
            details=details or {},
            remedy=remedy or (
                "Check subsystem availability or query an alternative evidence source."
            ),
        )
    )


def budget_exhausted(
    message: str, details: dict[str, Any] | None = None, remedy: str = ""
) -> ToolError:
    """Create a ToolError with code budget_exhausted."""
    return ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.BUDGET_EXHAUSTED,
            message=redact_sensitive(message),
            details=details or {},
            remedy=remedy or "Conclude investigation with existing evidence or request escalation.",
        )
    )


def access_denied(
    message: str, details: dict[str, Any] | None = None, remedy: str = ""
) -> ToolError:
    """Create a ToolError with code access_denied."""
    return ToolError(
        error=ToolErrorDetail(
            code=ErrorCode.ACCESS_DENIED,
            message=redact_sensitive(message),
            details=details or {},
            remedy=remedy or (
                "Restrict queries to services authorized in ToolContext.allowed_services."
            ),
        )
    )


F = TypeVar("F", bound=Callable[..., Any])


def catch_tool_errors(func: F) -> F:
    """Tool boundary decorator that catches unexpected exceptions.

    Unexpected exceptions are logged with sensitive tokens redacted,
    and converted to a structured ToolError with code source_unavailable.
    Tools NEVER raise unhandled exceptions to the caller.
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                raw_msg = str(exc)
                sanitized_msg = redact_sensitive(raw_msg)
                logger.error(
                    "Unexpected tool exception caught at boundary",
                    exc_info=True,
                    extra={"sanitized_error": sanitized_msg, "tool": func.__name__},
                )
                return source_unavailable(
                    message=f"Subsystem error in {func.__name__}: {sanitized_msg}",
                    details={"error_type": type(exc).__name__},
                    remedy="Telemetry subsystem encountered an error. Query alternative evidence.",
                )

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            raw_msg = str(exc)
            sanitized_msg = redact_sensitive(raw_msg)
            logger.error(
                "Unexpected tool exception caught at boundary",
                exc_info=True,
                extra={"sanitized_error": sanitized_msg, "tool": func.__name__},
            )
            return source_unavailable(
                message=f"Subsystem error in {func.__name__}: {sanitized_msg}",
                details={"error_type": type(exc).__name__},
                remedy="Telemetry subsystem encountered an error. Query alternative evidence.",
            )

    return sync_wrapper  # type: ignore[return-value]

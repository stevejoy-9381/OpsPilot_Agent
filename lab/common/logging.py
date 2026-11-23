import contextvars
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, MutableMapping

import structlog
from opentelemetry import trace

from apps.api.logging import redact_sensitive_data

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
version_provider_ctx: contextvars.ContextVar[Callable[[], str] | None] = contextvars.ContextVar(
    "version_provider", default=None
)


def set_request_id(req_id: str) -> None:
    request_id_ctx.set(req_id)


def get_request_id() -> str:
    return request_id_ctx.get()


def set_version_provider(provider: Callable[[], str]) -> None:
    version_provider_ctx.set(provider)


def add_lab_telemetry_fields(
    service_name: str,
    static_version: str,
) -> Callable[[Any, str, MutableMapping[str, Any]], MutableMapping[str, Any]]:
    def _processor(
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        # Timestamp formatted ISO 8601 UTC
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Resolve active version dynamically if provider registered
        provider = version_provider_ctx.get()
        active_version = provider() if provider is not None else static_version

        # Trace and Span IDs from active OpenTelemetry context
        span = trace.get_current_span()
        span_context = span.get_span_context() if span else None
        if span_context and span_context.is_valid:
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")
        else:
            trace_id = ""
            span_id = ""

        event_name = event_dict.pop("event", "")
        msg = event_dict.pop("msg", event_name)

        # Standard ordered canonical lab fields
        ordered_dict: dict[str, Any] = {
            "ts": now,
            "service": service_name,
            "version": active_version,
            "level": event_dict.pop("level", method_name.upper()),
            "event": event_name,
            "msg": msg,
            "request_id": request_id_ctx.get(),
            "trace_id": trace_id,
            "span_id": span_id,
        }

        # Preserve remaining extra contextual fields under payload
        for k, v in event_dict.items():
            if k not in ("logger", "log_level"):
                ordered_dict[k] = v

        return ordered_dict

    return _processor


def setup_lab_logging(
    service_name: str,
    version: str = "v1.0.0",
    log_dir: str = "/telemetry/logs",
    log_level: str = "INFO",
) -> None:
    """Configures structured JSON logging writing to stdout and /telemetry/logs/<service>.jsonl."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    # Attempt to prepare telemetry file handler
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_path = os.path.join(log_dir, f"{service_name}.jsonl")
        file_handler = logging.FileHandler(file_path, mode="a", encoding="utf-8")
        handlers.append(file_handler)
    except OSError:
        # Fallback gracefully if directory is non-writable (e.g. strict sandbox / testing)
        pass

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # Clear existing handlers
    root_logger.handlers = []
    for h in handlers:
        h.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(h)

    # Mute noisy third-party HTTP client libraries from polluting structured logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            add_lab_telemetry_fields(service_name, version),
            redact_sensitive_data,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def get_lab_logger(name: str = "lab.service") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]

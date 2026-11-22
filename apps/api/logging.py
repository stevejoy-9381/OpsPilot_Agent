import logging
import sys
from typing import Any, MutableMapping

import structlog

SENSITIVE_PATTERNS = ("key", "secret", "token", "password")


def redact_sensitive_data(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    def _process_dict(d: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        for k, v in list(d.items()):
            lower_k = str(k).lower()
            if any(pattern in lower_k for pattern in SENSITIVE_PATTERNS):
                d[k] = "[REDACTED]"
            elif isinstance(v, MutableMapping):
                d[k] = _process_dict(v)
            elif isinstance(v, list):
                d[k] = [
                    _process_dict(item) if isinstance(item, MutableMapping) else item
                    for item in v
                ]
        return d

    return _process_dict(event_dict)


def setup_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_sensitive_data,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "opspilot") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]

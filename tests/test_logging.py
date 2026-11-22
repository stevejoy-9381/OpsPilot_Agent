import io
import json
import logging
from typing import Any, Dict

import structlog

from apps.api.logging import redact_sensitive_data


def test_redact_sensitive_data_direct() -> None:
    sensitive_payload: Dict[str, Any] = {
        "event": "user_login",
        "api_key": "raw-api-key-12345",
        "db_password": "super-secret-password",
        "auth_token": "bearer-token-abcde",
        "my_secret": "my-hidden-secret",
        "nested": {
            "oauth_token": "nested-token",
            "safe_field": "visible",
        },
        "safe_key_not_sensitive": "visible_too",
    }

    result = redact_sensitive_data(None, "info", sensitive_payload)

    assert result["api_key"] == "[REDACTED]"
    assert result["db_password"] == "[REDACTED]"
    assert result["auth_token"] == "[REDACTED]"
    assert result["my_secret"] == "[REDACTED]"
    assert result["nested"]["oauth_token"] == "[REDACTED]"
    assert result["nested"]["safe_field"] == "visible"
    assert result["safe_key_not_sensitive"] == "[REDACTED]"  # contains 'key'


def test_log_redaction_in_json_stream() -> None:
    output_stream = io.StringIO()

    structlog.reset_defaults()
    logging.basicConfig(level=logging.INFO)

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            redact_sensitive_data,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=output_stream),
        cache_logger_on_first_use=False,
    )

    logger = structlog.get_logger("test")

    secret_val = "SUPER_SECRET_VALUE_123"
    password_val = "SECRET_PASSWORD_456"
    token_val = "SECRET_TOKEN_789"
    api_key_val = "SECRET_KEY_ABC"

    logger.info(
        "operation_executed",
        db_password=password_val,
        auth_token=token_val,
        api_key=api_key_val,
        custom_secret=secret_val,
        public_info="safe_message",
    )

    logged_text = output_stream.getvalue()

    # Secrets must NEVER appear in the log output
    assert secret_val not in logged_text
    assert password_val not in logged_text
    assert token_val not in logged_text
    assert api_key_val not in logged_text

    # Redacted placeholder and safe field must appear
    assert "[REDACTED]" in logged_text
    assert "safe_message" in logged_text

    # Parse and verify valid JSON
    parsed = json.loads(logged_text.strip())
    assert parsed["db_password"] == "[REDACTED]"
    assert parsed["auth_token"] == "[REDACTED]"
    assert parsed["api_key"] == "[REDACTED]"
    assert parsed["custom_secret"] == "[REDACTED]"
    assert parsed["public_info"] == "safe_message"

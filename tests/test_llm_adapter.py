import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, Field, SecretStr

from agent.llm.fake import FakeLLMClient
from agent.llm.gemini import _GENAI_AVAILABLE, GeminiClient, errors
from agent.llm.interface import (
    BudgetExceededError,
    Message,
    TokenBudgetGuard,
    ToolCall,
    ToolSpec,
)


class IncidentDiagnosis(BaseModel):
    service: str
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)


class MockUsageMetadata:
    prompt_token_count = 100
    candidates_token_count = 50
    total_token_count = 150


class MockGenerateContentResponse:
    def __init__(
        self,
        *,
        text: str = "",
        parsed: Any = None,
        function_calls: list[Any] | None = None,
        finish_reason: str = "STOP",
        response_id: str = "req-12345",
    ) -> None:
        self.text = text
        self.parsed = parsed
        self.function_calls = function_calls or []
        self.response_id = response_id
        self.usage_metadata = MockUsageMetadata()
        self.prompt_feedback = None
        self.candidates = [MagicMock(finish_reason=finish_reason)]


# 1. FakeLLMClient tests


def test_fake_llm_structured_success() -> None:
    expected = IncidentDiagnosis(
        service="payment-service", root_cause="deadlock on orders table", confidence=0.95
    )
    client = FakeLLMClient(responses=[expected])

    result = client.generate_structured(
        messages=[Message(role="user", content="Analyze incident")],
        response_schema=IncidentDiagnosis,
    )

    assert result.is_success
    assert result.parsed == expected
    assert result.error is None
    assert result.token_usage.total_tokens == 80
    assert len(client.call_history) == 1


def test_fake_llm_structured_validation_failure() -> None:
    # Invalid dict missing required fields
    client = FakeLLMClient(responses=[{"service": "auth-service"}])

    result = client.generate_structured(
        messages=[Message(role="user", content="Diagnose")],
        response_schema=IncidentDiagnosis,
    )

    assert not result.is_success
    assert result.parsed is None
    assert result.error is not None
    assert result.error.error_type == "schema_validation_failed"


def test_fake_llm_tools_generation() -> None:
    tool_call = ToolCall(id="tc-1", name="query_logs", args={"service": "payment"})
    client = FakeLLMClient(responses=[[tool_call]])

    tools = [
        ToolSpec(
            name="query_logs",
            description="Query logs for service",
            parameters_schema={
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        )
    ]
    result = client.generate_with_tools(
        messages=[Message(role="user", content="Check payment logs")],
        tools=tools,
    )

    assert result.is_success
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "query_logs"
    assert result.tool_calls[0].args == {"service": "payment"}


def test_fake_llm_budget_guard_exceeded() -> None:
    guard = TokenBudgetGuard(max_tokens=100)
    client = FakeLLMClient(budget_guard=guard)

    client.push_response(
        IncidentDiagnosis(service="s1", root_cause="c1", confidence=0.8)
    )
    # First call uses 80 tokens
    client.generate_structured(
        messages=[Message(role="user", content="m1")],
        response_schema=IncidentDiagnosis,
    )
    assert guard.consumed_tokens == 80

    client.push_response(
        IncidentDiagnosis(service="s2", root_cause="c2", confidence=0.9)
    )
    # Second call uses another 80 tokens -> 160 > 100 limit
    with pytest.raises(BudgetExceededError) as exc_info:
        client.generate_structured(
            messages=[Message(role="user", content="m2")],
            response_schema=IncidentDiagnosis,
        )
    assert exc_info.value.limit == 100
    assert exc_info.value.current_usage == 80


# 2. GeminiClient tests (unit with mocked API)


@pytest.fixture(autouse=True)
def require_genai(request: pytest.FixtureRequest) -> None:
    if "gemini" in request.node.name.lower() and not _GENAI_AVAILABLE:
        pytest.skip("google-genai is not installed in the environment")


def test_gemini_client_structured_success() -> None:
    expected = IncidentDiagnosis(service="db", root_cause="OOM", confidence=0.99)
    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
    )

    mock_resp = MockGenerateContentResponse(
        text=expected.model_dump_json(),
        parsed=expected,
    )

    with patch.object(client._client.models, "generate_content", return_value=mock_resp):
        result = client.generate_structured(
            messages=[
                Message(role="system", content="You are OpsPilot"),
                Message(role="user", content="Diagnose DB crash"),
            ],
            response_schema=IncidentDiagnosis,
        )

    assert result.is_success
    assert result.parsed == expected
    assert result.token_usage.total_tokens == 150
    assert result.error is None


def test_gemini_client_structured_validation_failure() -> None:
    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
    )

    mock_resp = MockGenerateContentResponse(
        text='{"invalid": "schema"}',
        parsed=None,
    )

    with patch.object(client._client.models, "generate_content", return_value=mock_resp):
        result = client.generate_structured(
            messages=[Message(role="user", content="Diagnose")],
            response_schema=IncidentDiagnosis,
        )

    assert not result.is_success
    assert result.parsed is None
    assert result.error is not None
    assert result.error.error_type == "schema_validation_failed"
    assert "Schema validation failed" in result.error.message


def test_gemini_client_timeout_handling() -> None:
    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
        max_retries=1,
        initial_backoff_s=0.01,
    )

    with patch.object(
        client._client.models,
        "generate_content",
        side_effect=TimeoutError("Request timed out after 30s"),
    ):
        result = client.generate_structured(
            messages=[Message(role="user", content="Diagnose")],
            response_schema=IncidentDiagnosis,
            timeout_s=0.1,
        )

    assert not result.is_success
    assert result.error is not None
    assert result.error.error_type == "timeout"
    assert "timed out" in result.error.message


def test_gemini_client_budget_guard() -> None:
    guard = TokenBudgetGuard(max_tokens=200)
    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
        budget_guard=guard,
    )

    diag = IncidentDiagnosis(service="api", root_cause="500s", confidence=0.7)
    mock_resp = MockGenerateContentResponse(text=diag.model_dump_json(), parsed=diag)

    with patch.object(client._client.models, "generate_content", return_value=mock_resp):
        # 1st call consumes 150 tokens (MockUsageMetadata has total=150)
        res1 = client.generate_structured(
            messages=[Message(role="user", content="msg1")],
            response_schema=IncidentDiagnosis,
        )
        assert res1.is_success
        assert guard.consumed_tokens == 150

        # 2nd call attempts to consume another 150 tokens -> 300 > 200 limit
        with pytest.raises(BudgetExceededError) as exc_info:
            client.generate_structured(
                messages=[Message(role="user", content="msg2")],
                response_schema=IncidentDiagnosis,
            )
        assert exc_info.value.limit == 200
        assert exc_info.value.current_usage == 150


def test_gemini_client_retry_count_and_backoff() -> None:
    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
        max_retries=3,
        initial_backoff_s=0.01,
        backoff_multiplier=1.5,
    )

    transient_err = errors.ServerError(
        code=503,
        response_json={"error": {"code": 503, "message": "Service Unavailable"}},
    )

    diag = IncidentDiagnosis(service="redis", root_cause="eviction", confidence=0.88)
    success_resp = MockGenerateContentResponse(text=diag.model_dump_json(), parsed=diag)

    # Fail twice with 503, then succeed on 3rd attempt
    mock_generate = MagicMock(side_effect=[transient_err, transient_err, success_resp])

    with patch.object(client._client.models, "generate_content", mock_generate):
        result = client.generate_structured(
            messages=[Message(role="user", content="Diagnose redis")],
            response_schema=IncidentDiagnosis,
        )

    assert result.is_success
    assert result.parsed == diag
    assert mock_generate.call_count == 3


def test_gemini_client_retry_exhausted_provider_error() -> None:
    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
        max_retries=3,
        initial_backoff_s=0.01,
    )

    rate_limit_err = errors.ClientError(
        code=429,
        response_json={"error": {"code": 429, "message": "Resource Exhausted"}},
    )

    mock_generate = MagicMock(side_effect=rate_limit_err)

    with patch.object(client._client.models, "generate_content", mock_generate):
        result = client.generate_structured(
            messages=[Message(role="user", content="Diagnose")],
            response_schema=IncidentDiagnosis,
        )

    assert not result.is_success
    assert result.error is not None
    assert result.error.error_type == "provider_error"
    # Max retries = 3 means 1 initial attempt + 3 retries = 4 attempts total
    assert mock_generate.call_count == 4


def test_gemini_client_safety_blocked() -> None:
    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
    )

    mock_resp = MockGenerateContentResponse(
        text="",
        finish_reason="SAFETY",
    )

    with patch.object(client._client.models, "generate_content", return_value=mock_resp):
        result = client.generate_structured(
            messages=[Message(role="user", content="Dangerous command")],
            response_schema=IncidentDiagnosis,
        )

    assert not result.is_success
    assert result.error is not None
    assert result.error.error_type == "safety_blocked"


def test_gemini_client_log_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    from apps.api.logging import setup_logging

    setup_logging("INFO")

    client = GeminiClient(
        api_key=SecretStr("fake-key"),
        model_name="gemini-2.0-flash",
    )

    diag = IncidentDiagnosis(service="s", root_cause="c", confidence=0.5)
    mock_resp = MockGenerateContentResponse(text=diag.model_dump_json(), parsed=diag)

    with patch.object(client._client.models, "generate_content", return_value=mock_resp):
        client.generate_structured(
            messages=[
                Message(
                    role="user",
                    content="Check DB password=super_secret_password_12345",
                )
            ],
            response_schema=IncidentDiagnosis,
        )

    captured = capsys.readouterr().out
    assert "super_secret_password_12345" not in captured


# 3. Optional Live smoke test


@pytest.mark.live
def test_gemini_live_smoke() -> None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "replace-with-your-api-key":
        pytest.skip("GEMINI_API_KEY is not set or is a placeholder")

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    client = GeminiClient(
        api_key=SecretStr(api_key),
        model_name=model_name,
    )

    result = client.generate_structured(
        messages=[
            Message(role="system", content="You are an automated diagnostic agent."),
            Message(
                role="user",
                content="A payment service had a deadlock on orders. Return a diagnosis.",
            ),
        ],
        response_schema=IncidentDiagnosis,
        timeout_s=30.0,
    )

    assert result.is_success
    assert result.parsed is not None
    assert result.parsed.service
    assert result.parsed.root_cause
    assert 0.0 <= result.parsed.confidence <= 1.0

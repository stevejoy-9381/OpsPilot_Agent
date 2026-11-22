import time
from typing import Any, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from agent.llm.interface import (
    LLMClient,
    LLMError,
    Message,
    StructuredResult,
    TokenBudgetGuard,
    TokenUsage,
    ToolCall,
    ToolCallResult,
    ToolSpec,
)

T = TypeVar("T", bound=BaseModel)


class FakeLLMClient(LLMClient):
    """Offline, deterministic LLM client replaying scripted responses."""

    def __init__(
        self,
        *,
        model_name: str = "fake-model",
        budget_guard: TokenBudgetGuard | None = None,
        responses: Sequence[Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self.budget_guard = budget_guard
        self._responses: list[Any] = list(responses) if responses is not None else []
        self._call_history: list[dict[str, Any]] = []

    @property
    def call_history(self) -> list[dict[str, Any]]:
        return self._call_history

    def push_response(self, response: Any) -> None:
        self._responses.append(response)

    def _next_response(self) -> Any:
        if not self._responses:
            raise IndexError("FakeLLMClient ran out of scripted responses")
        return self._responses.pop(0)

    def generate_structured(
        self,
        messages: list[Message],
        response_schema: type[T],
        *,
        max_output_tokens: int = 2048,
        timeout_s: float = 30.0,
    ) -> StructuredResult[T]:
        start = time.perf_counter()
        self._call_history.append(
            {
                "type": "structured",
                "messages": messages,
                "schema": response_schema,
                "max_output_tokens": max_output_tokens,
                "timeout_s": timeout_s,
            }
        )

        resp = self._next_response()

        # Handle exception / error simulation
        if isinstance(resp, Exception):
            raise resp

        if isinstance(resp, StructuredResult):
            # Enforce budget guard if present
            if self.budget_guard:
                self.budget_guard.check_and_consume(resp.token_usage.total_tokens)
            return resp

        # Handle simulated error dict
        if isinstance(resp, dict) and "error" in resp:
            err = resp["error"]
            if isinstance(err, str):
                llm_error = LLMError(error_type="provider_error", message=err)
            elif isinstance(err, LLMError):
                llm_error = err
            else:
                llm_error = LLMError.model_validate(err)
            return StructuredResult[T](
                model=self.model_name,
                error=llm_error,
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )

        # Handle schema validation error simulation (e.g. invalid dict or raw string)
        usage = TokenUsage(input_tokens=50, output_tokens=30, total_tokens=80)
        if self.budget_guard:
            self.budget_guard.check_and_consume(usage.total_tokens)

        latency_ms = (time.perf_counter() - start) * 1000.0

        if isinstance(resp, response_schema):
            return StructuredResult[T](
                parsed=resp,
                raw_text=resp.model_dump_json(),
                token_usage=usage,
                latency_ms=latency_ms,
                model=self.model_name,
                provider_request_id="fake-req-id",
            )
        elif isinstance(resp, dict):
            try:
                parsed = response_schema.model_validate(resp)
                return StructuredResult[T](
                    parsed=parsed,
                    raw_text=parsed.model_dump_json(),
                    token_usage=usage,
                    latency_ms=latency_ms,
                    model=self.model_name,
                    provider_request_id="fake-req-id",
                )
            except ValidationError as val_err:
                return StructuredResult[T](
                    raw_text=str(resp),
                    token_usage=usage,
                    latency_ms=latency_ms,
                    model=self.model_name,
                    provider_request_id="fake-req-id",
                    error=LLMError(
                        error_type="schema_validation_failed",
                        message=str(val_err),
                        details={"errors": val_err.errors()},
                    ),
                )
        elif isinstance(resp, str):
            try:
                parsed = response_schema.model_validate_json(resp)
                return StructuredResult[T](
                    parsed=parsed,
                    raw_text=resp,
                    token_usage=usage,
                    latency_ms=latency_ms,
                    model=self.model_name,
                    provider_request_id="fake-req-id",
                )
            except ValidationError as val_err:
                return StructuredResult[T](
                    raw_text=resp,
                    token_usage=usage,
                    latency_ms=latency_ms,
                    model=self.model_name,
                    provider_request_id="fake-req-id",
                    error=LLMError(
                        error_type="schema_validation_failed",
                        message=str(val_err),
                        details={"errors": val_err.errors()},
                    ),
                )

        raise TypeError(f"Unsupported scripted response type: {type(resp)}")

    def generate_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_output_tokens: int = 2048,
        timeout_s: float = 30.0,
    ) -> ToolCallResult:
        start = time.perf_counter()
        self._call_history.append(
            {
                "type": "tools",
                "messages": messages,
                "tools": tools,
                "max_output_tokens": max_output_tokens,
                "timeout_s": timeout_s,
            }
        )

        resp = self._next_response()

        if isinstance(resp, Exception):
            raise resp

        if isinstance(resp, ToolCallResult):
            if self.budget_guard:
                self.budget_guard.check_and_consume(resp.token_usage.total_tokens)
            return resp

        if isinstance(resp, dict) and "error" in resp:
            err = resp["error"]
            if isinstance(err, str):
                llm_error = LLMError(error_type="provider_error", message=err)
            elif isinstance(err, LLMError):
                llm_error = err
            else:
                llm_error = LLMError.model_validate(err)
            return ToolCallResult(
                model=self.model_name,
                error=llm_error,
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )

        usage = TokenUsage(input_tokens=60, output_tokens=40, total_tokens=100)
        if self.budget_guard:
            self.budget_guard.check_and_consume(usage.total_tokens)

        latency_ms = (time.perf_counter() - start) * 1000.0

        if isinstance(resp, list):
            # List of ToolCall objects or dicts
            tool_calls = [
                tc if isinstance(tc, ToolCall) else ToolCall.model_validate(tc) for tc in resp
            ]
            return ToolCallResult(
                tool_calls=tool_calls,
                token_usage=usage,
                latency_ms=latency_ms,
                model=self.model_name,
                provider_request_id="fake-req-id",
            )
        elif isinstance(resp, ToolCall):
            return ToolCallResult(
                tool_calls=[resp],
                token_usage=usage,
                latency_ms=latency_ms,
                model=self.model_name,
                provider_request_id="fake-req-id",
            )
        elif isinstance(resp, str):
            return ToolCallResult(
                raw_text=resp,
                token_usage=usage,
                latency_ms=latency_ms,
                model=self.model_name,
                provider_request_id="fake-req-id",
            )

        raise TypeError(f"Unsupported scripted response type: {type(resp)}")

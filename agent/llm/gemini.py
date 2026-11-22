import time
import uuid
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, SecretStr, ValidationError

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
from apps.api.logging import get_logger

T = TypeVar("T", bound=BaseModel)
logger = get_logger("agent.llm.gemini")

__all__ = ["GeminiClient", "errors"]

TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, errors.APIError):
        if exc.code in TRANSIENT_STATUS_CODES:
            return True
        if exc.status in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED"):
            return True
    if isinstance(exc, (TimeoutError, errors.ServerError)):
        return True
    return False


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, errors.APIError):
        if exc.code == 408 or exc.status == "DEADLINE_EXCEEDED":
            return True
    return False


class GeminiClient(LLMClient):
    """Google Gemini LLM client implementing LLMClient."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model_name: str,
        budget_guard: TokenBudgetGuard | None = None,
        max_retries: int = 3,
        initial_backoff_s: float = 1.0,
        backoff_multiplier: float = 2.0,
    ) -> None:
        self.model_name = model_name
        self.budget_guard = budget_guard
        self.max_retries = max_retries
        self.initial_backoff_s = initial_backoff_s
        self.backoff_multiplier = backoff_multiplier

        # Instantiate genai Client with SecretStr value
        secret_val = api_key.get_secret_value()
        self._client = genai.Client(api_key=secret_val if secret_val else None)

    def _prepare_contents(
        self, messages: list[Message]
    ) -> tuple[str | None, list[types.Content]]:
        system_instruction_parts: list[str] = []
        contents: list[types.Content] = []

        for msg in messages:
            if msg.role == "system":
                system_instruction_parts.append(msg.content)
            else:
                role = "model" if msg.role == "model" else "user"
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=msg.content)],
                    )
                )

        system_instruction = (
            "\n\n".join(system_instruction_parts) if system_instruction_parts else None
        )
        return system_instruction, contents

    def generate_structured(
        self,
        messages: list[Message],
        response_schema: type[T],
        *,
        max_output_tokens: int = 2048,
        timeout_s: float = 30.0,
    ) -> StructuredResult[T]:
        start = time.perf_counter()
        system_instruction, contents = self._prepare_contents(messages)

        logger.info(
            "Generating structured output",
            model=self.model_name,
            schema=response_schema.__name__,
            message_count=len(messages),
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            http_options=types.HttpOptions(
                timeout=int(timeout_s * 1000),
            ),
        )

        response: types.GenerateContentResponse | None = None
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
                break
            except Exception as e:
                last_error = e
                if attempt < self.max_retries and _is_transient_error(e):
                    backoff = self.initial_backoff_s * (self.backoff_multiplier**attempt)
                    logger.warning(
                        "Transient error during generate_structured; retrying",
                        attempt=attempt + 1,
                        backoff_s=backoff,
                        error=str(e),
                    )
                    time.sleep(backoff)
                    continue
                break

        latency_ms = (time.perf_counter() - start) * 1000.0
        if response is not None:
            return self._parse_structured_response(
                response, response_schema, latency_ms
            )

        return self._handle_structured_exception(last_error, latency_ms)

    def _parse_structured_response(
        self,
        response: types.GenerateContentResponse,
        response_schema: type[T],
        latency_ms: float,
    ) -> StructuredResult[T]:
        token_usage = self._extract_token_usage(response)
        if self.budget_guard:
            self.budget_guard.check_and_consume(token_usage.total_tokens)

        # Check safety block
        if self._is_safety_blocked(response):
            return StructuredResult[T](
                token_usage=token_usage,
                latency_ms=latency_ms,
                model=self.model_name,
                provider_request_id=response.response_id,
                error=LLMError(
                    error_type="safety_blocked",
                    message="Generation was blocked by safety filters",
                    details={"prompt_feedback": str(response.prompt_feedback)},
                ),
            )

        # Check parsed response from SDK
        raw_text = response.text or ""
        parsed_val: T | None = None

        if hasattr(response, "parsed") and isinstance(response.parsed, response_schema):
            parsed_val = response.parsed
        else:
            try:
                parsed_val = response_schema.model_validate_json(raw_text)
            except ValidationError as val_err:
                return StructuredResult[T](
                    raw_text=raw_text,
                    token_usage=token_usage,
                    latency_ms=latency_ms,
                    model=self.model_name,
                    provider_request_id=response.response_id,
                    error=LLMError(
                        error_type="schema_validation_failed",
                        message=f"Schema validation failed: {val_err}",
                        details={"errors": val_err.errors(), "raw_text": raw_text},
                    ),
                )

        return StructuredResult[T](
            parsed=parsed_val,
            raw_text=raw_text,
            token_usage=token_usage,
            latency_ms=latency_ms,
            model=self.model_name,
            provider_request_id=response.response_id,
        )

    def _handle_structured_exception(
        self, exc: Exception | None, latency_ms: float
    ) -> StructuredResult[T]:
        if exc is None:
            return StructuredResult[T](
                latency_ms=latency_ms,
                model=self.model_name,
                error=LLMError(error_type="provider_error", message="Unknown failure"),
            )

        if _is_timeout_error(exc):
            return StructuredResult[T](
                latency_ms=latency_ms,
                model=self.model_name,
                error=LLMError(error_type="timeout", message=str(exc)),
            )

        return StructuredResult[T](
            latency_ms=latency_ms,
            model=self.model_name,
            error=LLMError(
                error_type="provider_error",
                message=str(exc),
                details={"exception_type": type(exc).__name__},
            ),
        )

    def generate_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_output_tokens: int = 2048,
        timeout_s: float = 30.0,
    ) -> ToolCallResult:
        start = time.perf_counter()
        system_instruction, contents = self._prepare_contents(messages)

        function_declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.parameters_schema,
            )
            for t in tools
        ]

        logger.info(
            "Generating with tools",
            model=self.model_name,
            tool_count=len(tools),
            message_count=len(messages),
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[types.Tool(function_declarations=function_declarations)],
            max_output_tokens=max_output_tokens,
            http_options=types.HttpOptions(
                timeout=int(timeout_s * 1000),
            ),
        )

        response: types.GenerateContentResponse | None = None
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
                break
            except Exception as e:
                last_error = e
                if attempt < self.max_retries and _is_transient_error(e):
                    backoff = self.initial_backoff_s * (self.backoff_multiplier**attempt)
                    logger.warning(
                        "Transient error during generate_with_tools; retrying",
                        attempt=attempt + 1,
                        backoff_s=backoff,
                        error=str(e),
                    )
                    time.sleep(backoff)
                    continue
                break

        latency_ms = (time.perf_counter() - start) * 1000.0
        if response is not None:
            return self._parse_tools_response(response, latency_ms)

        return self._handle_tools_exception(last_error, latency_ms)

    def _parse_tools_response(
        self,
        response: types.GenerateContentResponse,
        latency_ms: float,
    ) -> ToolCallResult:
        token_usage = self._extract_token_usage(response)
        if self.budget_guard:
            self.budget_guard.check_and_consume(token_usage.total_tokens)

        if self._is_safety_blocked(response):
            return ToolCallResult(
                token_usage=token_usage,
                latency_ms=latency_ms,
                model=self.model_name,
                provider_request_id=response.response_id,
                error=LLMError(
                    error_type="safety_blocked",
                    message="Generation was blocked by safety filters",
                    details={"prompt_feedback": str(response.prompt_feedback)},
                ),
            )

        tool_calls: list[ToolCall] = []
        if response.function_calls:
            for i, fc in enumerate(response.function_calls):
                call_id = fc.id if getattr(fc, "id", None) else f"call_{i}_{uuid.uuid4().hex[:8]}"
                tool_calls.append(
                    ToolCall(
                        id=call_id,
                        name=fc.name or "",
                        args=fc.args or {},
                    )
                )

        return ToolCallResult(
            tool_calls=tool_calls,
            raw_text=response.text,
            token_usage=token_usage,
            latency_ms=latency_ms,
            model=self.model_name,
            provider_request_id=response.response_id,
        )

    def _handle_tools_exception(
        self, exc: Exception | None, latency_ms: float
    ) -> ToolCallResult:
        if exc is None:
            return ToolCallResult(
                latency_ms=latency_ms,
                model=self.model_name,
                error=LLMError(error_type="provider_error", message="Unknown failure"),
            )

        if _is_timeout_error(exc):
            return ToolCallResult(
                latency_ms=latency_ms,
                model=self.model_name,
                error=LLMError(error_type="timeout", message=str(exc)),
            )

        return ToolCallResult(
            latency_ms=latency_ms,
            model=self.model_name,
            error=LLMError(
                error_type="provider_error",
                message=str(exc),
                details={"exception_type": type(exc).__name__},
            ),
        )

    def _extract_token_usage(self, response: types.GenerateContentResponse) -> TokenUsage:
        if response.usage_metadata:
            return TokenUsage(
                input_tokens=response.usage_metadata.prompt_token_count or 0,
                output_tokens=response.usage_metadata.candidates_token_count or 0,
                total_tokens=response.usage_metadata.total_token_count or 0,
            )
        return TokenUsage()

    def _is_safety_blocked(self, response: types.GenerateContentResponse) -> bool:
        if response.prompt_feedback and getattr(
            response.prompt_feedback, "block_reason", None
        ):
            return True
        if response.candidates:
            first_candidate = response.candidates[0]
            if first_candidate.finish_reason in ("SAFETY", "RECITATION", "BLOCKLIST"):
                return True
        return False

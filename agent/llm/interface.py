from abc import ABC, abstractmethod
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)

LLMErrorType = Literal["provider_error", "timeout", "schema_validation_failed", "safety_blocked"]


class Message(BaseModel):
    role: Literal["user", "model", "system"]
    content: str


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any]


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class LLMError(BaseModel):
    error_type: LLMErrorType
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class StructuredResult(BaseModel, Generic[T]):
    parsed: T | None = None
    raw_text: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    model: str = ""
    provider_request_id: str | None = None
    error: LLMError | None = None

    @property
    def is_success(self) -> bool:
        return self.error is None and self.parsed is not None


class ToolCallResult(BaseModel):
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw_text: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    model: str = ""
    provider_request_id: str | None = None
    error: LLMError | None = None

    @property
    def is_success(self) -> bool:
        return self.error is None


class BudgetExceededError(Exception):
    def __init__(self, current_usage: int, limit: int, requested: int = 0) -> None:
        self.current_usage = current_usage
        self.limit = limit
        self.requested = requested
        super().__init__(
            f"Token budget exceeded: current={current_usage}, limit={limit}, requested={requested}"
        )


class TokenBudgetGuard:
    def __init__(self, max_tokens: int | None = None) -> None:
        self.max_tokens = max_tokens
        self._consumed_tokens: int = 0

    @property
    def consumed_tokens(self) -> int:
        return self._consumed_tokens

    def check_and_consume(self, tokens: int) -> None:
        if self.max_tokens is not None and self.max_tokens > 0:
            if self._consumed_tokens + tokens > self.max_tokens:
                raise BudgetExceededError(
                    current_usage=self._consumed_tokens,
                    limit=self.max_tokens,
                    requested=tokens,
                )
        self._consumed_tokens += tokens


class LLMClient(ABC):
    @abstractmethod
    def generate_structured(
        self,
        messages: list[Message],
        response_schema: type[T],
        *,
        max_output_tokens: int = 2048,
        timeout_s: float = 30.0,
    ) -> StructuredResult[T]:
        """Generate a response structured according to response_schema."""
        ...

    @abstractmethod
    def generate_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_output_tokens: int = 2048,
        timeout_s: float = 30.0,
    ) -> ToolCallResult:
        """Generate response with tools, returning tool calls if invoked."""
        ...

"""tools/registry.py

Tool registry, schema generation, server-side budget enforcement, duplicate call caching,
and execution dispatch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tools.common.errors import budget_exhausted, invalid_argument, source_unavailable
from tools.common.evidence_store import EvidenceStore, get_default_evidence_store
from tools.common.models import ToolContext, ToolResult, ToolSuccess
from tools.read.deploys import get_deploy_diff, list_deploys
from tools.read.logs import search_logs
from tools.read.metrics import get_metrics
from tools.read.traces import get_trace

logger = logging.getLogger("opspilot.tools.registry")
TOOL_TIMEOUT_SECONDS: float = 15.0

# ---------------------------------------------------------------------------
# Model-Facing Input Schemas (Strictly no run_id, tenant_id, window or credentials)
# ---------------------------------------------------------------------------
class GetMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(description="Name of the service (e.g. orders, inventory)")
    metric: str = Field(description="Metric enum identifier (e.g. error_rate, latency_p95, db_pool_in_use)")
    start: datetime | None = Field(default=None, description="Start timestamp in UTC ISO format")
    end: datetime | None = Field(default=None, description="End timestamp in UTC ISO format")
    step_seconds: int | None = Field(default=None, description="Step resolution in seconds (default auto-calculated)")


class SearchLogsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(description="Name of the service (e.g. orders, inventory)")
    start: datetime | None = Field(default=None, description="Start timestamp in UTC ISO format")
    end: datetime | None = Field(default=None, description="End timestamp in UTC ISO format")
    level: str | None = Field(default=None, description="Log level filter: error, warn, info, or any")
    contains: str | None = Field(default=None, description="Plain text substring to search for (case-insensitive, max 64 chars)")
    limit: int = Field(default=20, description="Maximum number of log samples to return (max 50)")


class GetTraceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: str | None = Field(default=None, description="Full trace ID to inspect. Mutually exclusive with service.")
    service: str | None = Field(default=None, description="Service name for candidate search. Mutually exclusive with trace_id.")
    start: datetime | None = Field(default=None, description="Start timestamp in UTC ISO format")
    end: datetime | None = Field(default=None, description="End timestamp in UTC ISO format")
    min_duration_ms: float | None = Field(default=None, description="Minimum duration filter in milliseconds")
    errors_only: bool = Field(default=False, description="Filter only candidate traces containing errors")
    limit: int = Field(default=3, description="Maximum candidate traces to return (max 5)")


class ListDeploysInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str | None = Field(default=None, description="Target service name or omitted for all services")
    start: datetime | None = Field(default=None, description="Start timestamp in UTC ISO format")
    end: datetime | None = Field(default=None, description="End timestamp in UTC ISO format")
    limit: int = Field(default=10, description="Maximum deployment records to return")


class GetDeployDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(description="Target service name")
    old_version: str = Field(description="Base deployment version tag (e.g. v1.1.0)")
    new_version: str = Field(description="Target deployment version tag (e.g. v1.2.0)")


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallRecord(BaseModel):
    run_id: str
    tool: str
    args: dict[str, Any]
    duration_ms: float
    ok: bool
    error_code: str | None = None
    evidence_id: str | None = None
    response_chars: int = 0
    approx_tokens: int = 0


# In-memory execution cache for duplicate-call detection: key -> (ToolResult, ToolCallRecord)
_CALL_CACHE: dict[tuple[str, str, str], ToolResult] = {}
_DISPATCH_LOGS: list[ToolCallRecord] = []


def _load_spec_description(tool_name: str) -> str:
    spec_file = Path(__file__).parent / "read" / "specs" / f"{tool_name}.md"
    if spec_file.exists():
        return spec_file.read_text(encoding="utf-8").strip()
    return f"Investigation tool {tool_name}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[Type[BaseModel], Callable[..., Coroutine[Any, Any, ToolResult]]]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register("get_metrics", GetMetricsInput, get_metrics)
        self.register("search_logs", SearchLogsInput, search_logs)
        self.register("get_trace", GetTraceInput, get_trace)
        self.register("list_deploys", ListDeploysInput, list_deploys)
        self.register("get_deploy_diff", GetDeployDiffInput, get_deploy_diff)

    def register(
        self,
        name: str,
        input_model: Type[BaseModel],
        handler: Callable[..., Coroutine[Any, Any, ToolResult]],
    ) -> None:
        self._tools[name] = (input_model, handler)

    def get_specs(self) -> list[ToolSpec]:
        specs = []
        for name, (input_model, _) in self._tools.items():
            specs.append(
                ToolSpec(
                    name=name,
                    description=_load_spec_description(name),
                    parameters=input_model.model_json_schema(),
                )
            )
        return specs

    async def dispatch(
        self,
        name: str,
        raw_args: dict[str, Any],
        ctx: ToolContext,
        store: EvidenceStore | None = None,
        backend: Any = None,
    ) -> ToolResult:
        start_time = time.monotonic()
        actual_store = store if store is not None else get_default_evidence_store()
        tool_entry = self._tools.get(name)

        # 1. Unknown tool check
        if tool_entry is None:
            err = invalid_argument(
                f"Unknown tool '{name}'. Available tools: {list(self._tools.keys())}",
                details={"available_tools": list(self._tools.keys())},
            )
            self._record_call(ctx, name, raw_args, start_time, err)
            return err

        input_model, handler = tool_entry

        # 2. Check remaining calls budget (enforced server-side)
        if ctx.remaining_calls <= 0:
            err = budget_exhausted(
                f"Investigation call budget exhausted (remaining_calls={ctx.remaining_calls}).",
                details={"remaining_calls": ctx.remaining_calls},
                remedy="Conclude diagnosis from captured evidence or request human escalation.",
            )
            self._record_call(ctx, name, raw_args, start_time, err)
            return err

        # Decrement remaining calls
        object.__setattr__(ctx, "remaining_calls", ctx.remaining_calls - 1)

        # 3. Validate input against model-facing schema (strictly forbids run_id/tenant_id)
        try:
            validated = input_model.model_validate(raw_args)
            call_kwargs = validated.model_dump()
        except ValidationError as val_err:
            first_err = val_err.errors()[0]
            field_name = ".".join(str(loc) for loc in first_err.get("loc", []))
            msg = first_err.get("msg", "Invalid argument")
            hint = f"Fix field '{field_name}': {msg}. Refer to tool parameter schema."
            err = invalid_argument(
                f"Validation error for tool '{name}': {msg}",
                details={"errors": val_err.errors(), "field": field_name},
                remedy=hint,
            )
            self._record_call(ctx, name, raw_args, start_time, err)
            return err

        # 4. Duplicate call check (normalized args hash)
        norm_args_str = json.dumps(call_kwargs, sort_keys=True, default=str)
        cache_key = (ctx.run_id, name, norm_args_str)
        if cache_key in _CALL_CACHE:
            cached_result = _CALL_CACHE[cache_key]
            # If successful, add note "duplicate_call" and restore 1 call budget
            if isinstance(cached_result, ToolSuccess):
                object.__setattr__(ctx, "remaining_calls", ctx.remaining_calls + 1)
                notes = list(cached_result.notes)
                if "duplicate_call" not in notes:
                    notes.append("duplicate_call")
                dup_result = ToolSuccess(
                    evidence_id=cached_result.evidence_id,
                    tool=cached_result.tool,
                    captured_at=cached_result.captured_at,
                    window=cached_result.window,
                    source=cached_result.source,
                    units=cached_result.units,
                    data=cached_result.data,
                    truncated=cached_result.truncated,
                    notes=notes,
                )
                self._record_call(ctx, name, call_kwargs, start_time, dup_result)
                return dup_result

        # 5. Dispatch with timeout enforcement
        try:
            result = await asyncio.wait_for(
                handler(ctx=ctx, **call_kwargs, backend=backend, store=actual_store),
                timeout=TOOL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            err = source_unavailable(
                f"Tool '{name}' execution timed out after {TOOL_TIMEOUT_SECONDS}s.",
                remedy="Check telemetry store response latency.",
            )
            self._record_call(ctx, name, call_kwargs, start_time, err)
            return err
        except Exception as exc:
            err = source_unavailable(
                f"Subsystem error executing tool '{name}': {exc}",
                remedy="Query an alternative evidence source.",
            )
            self._record_call(ctx, name, call_kwargs, start_time, err)
            return err

        # Cache valid result
        if isinstance(result, ToolSuccess):
            _CALL_CACHE[cache_key] = result

        self._record_call(ctx, name, call_kwargs, start_time, result)
        return result

    def _record_call(
        self,
        ctx: ToolContext,
        tool: str,
        args: dict[str, Any],
        start_time: float,
        result: ToolResult,
    ) -> None:
        dur_ms = round((time.monotonic() - start_time) * 1000.0, 2)
        resp_json = result.model_dump_json()
        chars = len(resp_json)
        tokens = max(1, chars // 4)

        rec = ToolCallRecord(
            run_id=ctx.run_id,
            tool=tool,
            args=args,
            duration_ms=dur_ms,
            ok=result.ok,
            error_code=result.error.code.value if not result.ok else None,
            evidence_id=getattr(result, "evidence_id", None),
            response_chars=chars,
            approx_tokens=tokens,
        )
        _DISPATCH_LOGS.append(rec)


registry = ToolRegistry()

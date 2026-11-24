"""Tests for tools/registry.py: budget exhaustion, validation, timeout, caching, forbidden fields."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import pytest

from tools.common.evidence_store import InMemoryEvidenceStore
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolSuccess
from tools.read.backends.snapshot import SnapshotBackend
from tools.registry import ToolRegistry, registry
from tests.test_metrics_tool import _build_scenario_snapshot


@pytest.fixture
def registry_env(tmp_path: Path) -> tuple[ToolRegistry, ToolContext, SnapshotBackend, InMemoryEvidenceStore]:
    snap_path = _build_scenario_snapshot(tmp_path)
    backend = SnapshotBackend(snap_path)
    store = InMemoryEvidenceStore()
    ctx = ToolContext(
        run_id="run_reg_001",
        tenant_id="tenant_alpha",
        incident_id="inc_005",
        window_start=datetime(2026, 3, 30, 9, 45, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc),
        allowed_services={"orders", "inventory"},
        remaining_calls=8,
    )
    reg = ToolRegistry()
    return reg, ctx, backend, store


@pytest.mark.asyncio
async def test_budget_exhaustion_at_call_9(registry_env: Any) -> None:
    reg, ctx, backend, store = registry_env
    # ctx has 8 remaining calls. Execute 8 successful calls
    for i in range(8):
        assert ctx.remaining_calls == 8 - i
        res = await reg.dispatch(
            name="get_metrics",
            raw_args={"service": "orders", "metric": "db_pool_in_use", "step_seconds": 15 + i},
            ctx=ctx,
            backend=backend,
            store=store,
        )
        assert isinstance(res, ToolSuccess)

    # Call 9 must fail with budget_exhausted
    assert ctx.remaining_calls == 0
    res9 = await reg.dispatch(
        name="get_metrics",
        raw_args={"service": "orders", "metric": "db_pool_in_use"},
        ctx=ctx,
        backend=backend,
        store=store,
    )
    assert isinstance(res9, ToolError)
    assert res9.error.code == ErrorCode.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_forbidden_fields_rejected(registry_env: Any) -> None:
    reg, ctx, backend, store = registry_env

    # Model attempting to pass run_id or tenant_id
    res = await reg.dispatch(
        name="get_metrics",
        raw_args={
            "service": "orders",
            "metric": "db_pool_in_use",
            "run_id": "malicious_run",
            "tenant_id": "malicious_tenant",
        },
        ctx=ctx,
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolError)
    assert res.error.code == ErrorCode.INVALID_ARGUMENT
    assert "extra_forbidden" in str(res.error.details) or "field" in res.error.remedy.lower()


@pytest.mark.asyncio
async def test_duplicate_call_caching_and_budget_preservation(registry_env: Any) -> None:
    reg, ctx, backend, store = registry_env
    initial_budget = ctx.remaining_calls

    args = {"service": "orders", "metric": "db_pool_in_use", "step_seconds": 30}
    res1 = await reg.dispatch("get_metrics", args, ctx, store=store, backend=backend)
    assert isinstance(res1, ToolSuccess)
    assert ctx.remaining_calls == initial_budget - 1

    # Call 2 is identical -> should return cached result with duplicate_call note without decrementing budget
    res2 = await reg.dispatch("get_metrics", args, ctx, store=store, backend=backend)
    assert isinstance(res2, ToolSuccess)
    assert res2.evidence_id == res1.evidence_id
    assert "duplicate_call" in res2.notes
    assert ctx.remaining_calls == initial_budget - 1  # Budget preserved


@pytest.mark.asyncio
async def test_timeout_maps_to_source_unavailable(registry_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    reg, ctx, backend, store = registry_env

    async def slow_handler(*args: Any, **kwargs: Any) -> ToolResult:
        await asyncio.sleep(2.0)
        return ToolSuccess(
            evidence_id="ev_slow",
            tool="slow_tool",
            captured_at=datetime.now(timezone.utc),
            window=kwargs.get("ctx").window_start,
            source="slow",
            data={},
        )

    from tools.registry import TOOL_TIMEOUT_SECONDS
    import tools.registry as reg_module
    monkeypatch.setattr(reg_module, "TOOL_TIMEOUT_SECONDS", 0.05)

    res = await reg.dispatch(
        name="get_metrics",
        raw_args={"service": "orders", "metric": "db_pool_in_use"},
        ctx=ctx,
        backend=backend,
        store=store,
    )
    # Since timeout is 0.05s, if anything takes longer or we patch handler:
    reg._tools["get_metrics"] = (reg._tools["get_metrics"][0], slow_handler)
    res_timeout = await reg.dispatch(
        name="get_metrics",
        raw_args={"service": "orders", "metric": "db_pool_in_use", "step_seconds": 999},
        ctx=ctx,
        backend=backend,
        store=store,
    )
    assert isinstance(res_timeout, ToolError)
    assert res_timeout.error.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "timed out" in res_timeout.error.message

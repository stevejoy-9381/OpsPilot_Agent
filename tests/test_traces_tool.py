"""Tests for tools/read/traces.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tools.common.evidence_store import InMemoryEvidenceStore
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolSuccess
from tools.read.backends.snapshot import SnapshotBackend
from tools.read.traces import TracePayload, get_trace


def _build_traces_snapshot(base_dir: Path) -> Path:
    traces_dir = base_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)

    # 1. S1 Trace: db.insert waiting on connection pool
    s1_trace = {
        "trace_id": "tr_s1_pool_wait",
        "spans": [
            {
                "span_id": "sp_s1_root",
                "parent_span_id": None,
                "service_name": "orders",
                "name": "POST /orders",
                "start_time": base_time.isoformat(),
                "end_time": (base_time + timedelta(seconds=30)).isoformat(),
                "status_code": "ERROR",
                "attributes": {"http.status_code": 500, "peer.service": "lab-postgres"},
            },
            {
                "span_id": "sp_s1_db",
                "parent_span_id": "sp_s1_root",
                "service_name": "orders",
                "name": "db.insert orders",
                "start_time": (base_time + timedelta(milliseconds=10)).isoformat(),
                "end_time": (base_time + timedelta(seconds=30)).isoformat(),
                "status_code": "ERROR",
                "attributes": {
                    "db.system": "postgresql",
                    "db.operation": "insert",
                    "db.pool.wait_ms": 29990.0,
                    "error": True,
                },
            },
        ],
    }
    (traces_dir / "tr_s1_pool_wait.json").write_text(json.dumps(s1_trace), encoding="utf-8")

    # 2. S2 Trace: N+1 DB query regression on inventory (many sibling db.query spans)
    s2_spans = [
        {
            "span_id": "sp_s2_root",
            "parent_span_id": None,
            "service_name": "inventory",
            "name": "GET /inventory/items",
            "start_time": base_time.isoformat(),
            "end_time": (base_time + timedelta(milliseconds=850)).isoformat(),
            "status_code": "OK",
            "attributes": {"http.status_code": 200},
        }
    ]
    # Add 50 repeated db.query spans to test N+1 query and collapsing rule (> 40 spans)
    for i in range(50):
        s_time = base_time + timedelta(milliseconds=10 + (i * 15))
        e_time = s_time + timedelta(milliseconds=12)
        s2_spans.append(
            {
                "span_id": f"sp_s2_db_{i}",
                "parent_span_id": "sp_s2_root",
                "service_name": "inventory",
                "name": "db.query items",
                "start_time": s_time.isoformat(),
                "end_time": e_time.isoformat(),
                "status_code": "OK",
                "attributes": {"db.operation": "select", "db.table": "inventory_items"},
            }
        )

    s2_trace = {"trace_id": "tr_s2_nplus1", "spans": s2_spans}
    (traces_dir / "tr_s2_nplus1.json").write_text(json.dumps(s2_trace), encoding="utf-8")

    # 3. S3 Trace: downstream redis / warehouse timeout
    s3_trace = {
        "trace_id": "tr_s3_timeout",
        "spans": [
            {
                "span_id": "sp_s3_root",
                "parent_span_id": None,
                "service_name": "inventory",
                "name": "POST /reserve",
                "start_time": base_time.isoformat(),
                "end_time": (base_time + timedelta(seconds=5)).isoformat(),
                "status_code": "ERROR",
                "attributes": {"http.status_code": 504, "peer.service": "redis"},
            },
            {
                "span_id": "sp_s3_redis",
                "parent_span_id": "sp_s3_root",
                "service_name": "inventory",
                "name": "redis.get reservation_lock",
                "start_time": (base_time + timedelta(milliseconds=5)).isoformat(),
                "end_time": (base_time + timedelta(seconds=5)).isoformat(),
                "status_code": "ERROR",
                "attributes": {"peer.service": "redis", "timeout.ms": 5000, "error": True},
            },
        ],
    }
    (traces_dir / "tr_s3_timeout.json").write_text(json.dumps(s3_trace), encoding="utf-8")

    return base_dir


@pytest.fixture
def trace_env(tmp_path: Path) -> tuple[SnapshotBackend, ToolContext, InMemoryEvidenceStore]:
    snap_path = _build_traces_snapshot(tmp_path)
    backend = SnapshotBackend(snap_path)
    store = InMemoryEvidenceStore()
    ctx = ToolContext(
        run_id="run_trace_001",
        tenant_id="tenant_alpha",
        incident_id="inc_003",
        window_start=datetime(2026, 3, 30, 9, 45, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 30, 10, 30, 0, tzinfo=timezone.utc),
        allowed_services={"orders", "inventory"},
        remaining_calls=8,
    )
    return backend, ctx, store


@pytest.mark.asyncio
async def test_get_trace_full_s1_pool_wait(trace_env: Any) -> None:
    backend, ctx, store = trace_env
    res = await get_trace(
        ctx=ctx,
        trace_id="tr_s1_pool_wait",
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolSuccess)
    payload: TracePayload = res.data
    assert payload.mode == "full_trace"
    assert payload.summary is not None
    assert payload.summary.total_duration_ms >= 30000.0
    assert payload.summary.db_query_count == 1
    assert "orders:db.insert orders" in payload.summary.slowest_span

    # Check pool wait attribute was preserved and allowlisted
    db_span = next(s for s in payload.spans if s.name == "db.insert orders")
    assert db_span.attributes.get("db.pool.wait_ms") == 29990.0


@pytest.mark.asyncio
async def test_get_trace_s2_collapsing_and_nplus1(trace_env: Any) -> None:
    backend, ctx, store = trace_env
    res = await get_trace(
        ctx=ctx,
        trace_id="tr_s2_nplus1",
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolSuccess)
    payload: TracePayload = res.data
    assert payload.summary is not None
    # 50 db.query spans should be detected
    assert payload.summary.db_query_count == 50

    # Spans in output payload must be <= 40
    assert len(payload.spans) <= 40
    # Must have collapsed the 50 sibling spans into a single collapsed node
    collapsed_node = next(s for s in payload.spans if "x db.query items" in s.name)
    assert collapsed_node.collapsed_count == 50
    assert "50 x db.query items" in collapsed_node.name


@pytest.mark.asyncio
async def test_get_trace_s3_redis_timeout(trace_env: Any) -> None:
    backend, ctx, store = trace_env
    res = await get_trace(
        ctx=ctx,
        trace_id="tr_s3_timeout",
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolSuccess)
    payload: TracePayload = res.data
    assert payload.summary is not None
    assert "redis" in payload.summary.dependency_duration_ms
    assert len(payload.summary.error_spans) == 2


@pytest.mark.asyncio
async def test_get_trace_candidate_search_mode(trace_env: Any) -> None:
    backend, ctx, store = trace_env

    # Query candidates for inventory
    res = await get_trace(
        ctx=ctx,
        service="inventory",
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolSuccess)
    payload: TracePayload = res.data
    assert payload.mode == "candidates"
    assert payload.candidates is not None
    assert len(payload.candidates) >= 1

    candidate_ids = [c.trace_id for c in payload.candidates]
    assert "tr_s3_timeout" in candidate_ids or "tr_s2_nplus1" in candidate_ids


@pytest.mark.asyncio
async def test_get_trace_mutual_exclusion(trace_env: Any) -> None:
    backend, ctx, store = trace_env
    # Both provided -> invalid argument
    res = await get_trace(
        ctx=ctx,
        trace_id="tr_s1_pool_wait",
        service="orders",
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolError)
    assert res.error.code == ErrorCode.INVALID_ARGUMENT

    # Neither provided -> invalid argument
    res2 = await get_trace(
        ctx=ctx,
        backend=backend,
        store=store,
    )
    assert isinstance(res2, ToolError)
    assert res2.error.code == ErrorCode.INVALID_ARGUMENT

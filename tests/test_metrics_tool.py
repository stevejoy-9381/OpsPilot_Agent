"""Tests for tools/read/metrics.py across scenarios S1-S5 and edge cases."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tools.common.evidence_store import InMemoryEvidenceStore
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolSuccess
from tools.read.backends.snapshot import SnapshotBackend
from tools.read.metrics import MetricsPayload, get_metrics


def _build_scenario_snapshot(base_dir: Path) -> Path:
    """Build snapshot directory simulating telemetry for S1, S2, S3, S4, and S5."""
    metrics_dir = base_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)

    # 1. S1: orders db_pool_in_use rising (leak from 2 to 10 maxed out)
    s1_pool_points = []
    # Baseline 09:45 -> 10:00 is flat at 2
    for m in range(-15, 0):
        t = (base_time + timedelta(minutes=m)).isoformat()
        s1_pool_points.append({"timestamp": t, "value": 2.0})
    # Active window 10:00 -> 10:15 rises from 2 to 10
    for m in range(0, 16):
        t = (base_time + timedelta(minutes=m)).isoformat()
        val = min(10.0, 2.0 + (m * 0.55))
        s1_pool_points.append({"timestamp": t, "value": round(val, 2)})

    (metrics_dir / "orders_db_pool_in_use.json").write_text(
        json.dumps({"metric_name": "db_pool_in_use", "service": "orders", "points": s1_pool_points}),
        encoding="utf-8",
    )

    # S1 orders latency_p95 rising
    s1_p95_points = []
    for m in range(-15, 16):
        t = (base_time + timedelta(minutes=m)).isoformat()
        val = 0.05 if m < 5 else 0.05 + ((m - 5) * 0.12)
        s1_p95_points.append({"timestamp": t, "value": round(val, 3)})
    (metrics_dir / "orders_latency_p95.json").write_text(
        json.dumps({"metric_name": "latency_p95", "service": "orders", "points": s1_p95_points}),
        encoding="utf-8",
    )

    # 2. S5: orders db_pool_in_use transient spike then recovery
    s5_pool_points = []
    for m in range(-15, 16):
        t = (base_time + timedelta(minutes=m)).isoformat()
        if m < 2:
            val = 2.0
        elif 2 <= m <= 6:
            val = 8.0  # Transient spike
        else:
            val = 2.0  # Returned to baseline
        s5_pool_points.append({"timestamp": t, "value": val})
    (metrics_dir / "orders_s5_pool.json").write_text(
        json.dumps({"metric_name": "db_pool_in_use", "service": "orders", "points": s5_pool_points}),
        encoding="utf-8",
    )

    # 3. Stale data sample: last timestamp was 15 minutes before 10:15
    stale_points = [
        {"timestamp": (base_time + timedelta(minutes=m)).isoformat(), "value": 1.0}
        for m in range(-15, 1)  # ends at 10:00 (15 min before window_end 10:15)
    ]
    (metrics_dir / "orders_request_rate.json").write_text(
        json.dumps({"metric_name": "request_rate", "service": "orders", "points": stale_points}),
        encoding="utf-8",
    )

    return base_dir


@pytest.fixture
def snapshot_env(tmp_path: Path) -> tuple[SnapshotBackend, ToolContext, InMemoryEvidenceStore]:
    snap_path = _build_scenario_snapshot(tmp_path)
    backend = SnapshotBackend(snap_path)
    store = InMemoryEvidenceStore()
    ctx = ToolContext(
        run_id="run_test_001",
        tenant_id="tenant_alpha",
        incident_id="inc_001",
        window_start=datetime(2026, 3, 30, 9, 45, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc),
        allowed_services={"orders", "inventory", "payment"},
        remaining_calls=8,
    )
    return backend, ctx, store


@pytest.mark.asyncio
async def test_s1_pool_in_use_rising_and_p95(snapshot_env: Any) -> None:
    backend, ctx, store = snapshot_env

    # 1. Check orders db_pool_in_use in S1
    start = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc)
    res = await get_metrics(
        ctx=ctx,
        service="orders",
        metric="db_pool_in_use",
        start=start,
        end=end,
        backend=backend,
        store=store,
    )

    assert isinstance(res, ToolSuccess)
    data: MetricsPayload = res.data
    assert data.summary.trend == "rising"
    assert data.summary.first == 2.0
    assert data.summary.last == 10.0
    assert data.summary.max == 10.0
    assert any("baseline mean" in note or "rapid increase" in note for note in res.notes)
    assert len(data.points) <= 30
    assert store.exists(res.evidence_id, ctx.run_id)

    # 2. Check latency_p95 rising
    p95_res = await get_metrics(
        ctx=ctx,
        service="orders",
        metric="latency_p95",
        start=start,
        end=end,
        backend=backend,
        store=store,
    )
    assert isinstance(p95_res, ToolSuccess)
    assert p95_res.data.summary.trend == "rising"


@pytest.mark.asyncio
async def test_s5_spike_and_return_to_baseline(snapshot_env: Any, tmp_path: Path) -> None:
    backend, ctx, store = snapshot_env
    # Swap S5 file in place for orders db_pool_in_use
    s5_data = (tmp_path / "metrics" / "orders_s5_pool.json").read_text(encoding="utf-8")
    (tmp_path / "metrics" / "orders_db_pool_in_use.json").write_text(s5_data, encoding="utf-8")

    start = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc)
    res = await get_metrics(
        ctx=ctx,
        service="orders",
        metric="db_pool_in_use",
        start=start,
        end=end,
        backend=backend,
        store=store,
    )

    assert isinstance(res, ToolSuccess)
    data: MetricsPayload = res.data
    # First was 2.0, peak was 8.0, but returned to 2.0 (flat overall trend)
    assert data.summary.first == 2.0
    assert data.summary.max == 8.0
    assert data.summary.last == 2.0
    assert data.summary.trend == "flat"


@pytest.mark.asyncio
async def test_out_of_window_rejection(snapshot_env: Any) -> None:
    backend, ctx, store = snapshot_env
    # Request start after ctx.window_end (10:15)
    future_start = datetime(2026, 3, 30, 10, 20, 0, tzinfo=timezone.utc)
    future_end = datetime(2026, 3, 30, 10, 25, 0, tzinfo=timezone.utc)

    res = await get_metrics(
        ctx=ctx,
        service="orders",
        metric="db_pool_in_use",
        start=future_start,
        end=future_end,
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolError)
    assert res.error.code == ErrorCode.OUT_OF_WINDOW


@pytest.mark.asyncio
async def test_stale_data_detection(snapshot_env: Any) -> None:
    backend, ctx, store = snapshot_env
    # orders request_rate ends at 10:00 while window_end is 10:15 (>120s old)
    start = datetime(2026, 3, 30, 9, 50, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc)

    res = await get_metrics(
        ctx=ctx,
        service="orders",
        metric="request_rate",
        start=start,
        end=end,
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolError)
    assert res.error.code == ErrorCode.STALE_DATA
    assert "old" in res.error.message


@pytest.mark.asyncio
async def test_not_found_for_missing_service_metric(snapshot_env: Any) -> None:
    backend, ctx, store = snapshot_env
    start = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc)

    res = await get_metrics(
        ctx=ctx,
        service="inventory",
        metric="db_pool_wait_seconds",
        start=start,
        end=end,
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolError)
    assert res.error.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_identical_call_reproducibility(snapshot_env: Any) -> None:
    backend, ctx, store = snapshot_env
    start = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc)

    res1 = await get_metrics(
        ctx=ctx,
        service="orders",
        metric="db_pool_in_use",
        start=start,
        end=end,
        step_seconds=30,
        backend=backend,
        store=store,
    )
    res2 = await get_metrics(
        ctx=ctx,
        service="orders",
        metric="db_pool_in_use",
        start=start,
        end=end,
        step_seconds=30,
        backend=backend,
        store=store,
    )

    assert isinstance(res1, ToolSuccess) and isinstance(res2, ToolSuccess)
    assert res1.evidence_id == res2.evidence_id
    assert res1.data.model_dump() == res2.data.model_dump()

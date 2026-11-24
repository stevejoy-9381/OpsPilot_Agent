"""Tests for tools/read/deploys.py across scenarios S1-S4 and edge cases."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tools.common.evidence_store import InMemoryEvidenceStore
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolSuccess
from tools.read.backends.snapshot import SnapshotBackend
from tools.read.deploys import DeployDiffPayload, get_deploy_diff, list_deploys


def _build_deploys_snapshot(base_dir: Path) -> Path:
    deploys_dir = base_dir / "deploys"
    diffs_dir = deploys_dir / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)

    # 1. S1: culprit deploy for orders (pool leak regression)
    s1_deploy = {
        "deploy_id": "dep_s1_pool",
        "service": "orders",
        "version": "v1.2.0",
        "deployed_at": (base_time - timedelta(minutes=10)).isoformat(),  # 09:50
        "git_commit": "commit_s1",
        "status": "completed",
        "environment": "lab",
    }
    s1_diff_text = (
        "--- a/orders/db.py\n+++ b/orders/db.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-pool = ConnectionPool(max_size=20, timeout=5)\n"
        "+pool = ConnectionPool(max_size=5, timeout=30)\n"  # Connection leak/exhaustion
    )
    (diffs_dir / "dep_s1_pool.diff").write_text(s1_diff_text, encoding="utf-8")
    (diffs_dir / "dep_s1_pool.json").write_text(
        json.dumps({"service": "orders", "previous_version": "v1.1.0", "current_version": "v1.2.0"}),
        encoding="utf-8",
    )

    # 2. S2: culprit deploy for inventory (N+1 query regression)
    s2_deploy = {
        "deploy_id": "dep_s2_nplus1",
        "service": "inventory",
        "version": "v2.1.0",
        "deployed_at": (base_time - timedelta(minutes=5)).isoformat(),  # 09:55
        "git_commit": "commit_s2",
        "status": "completed",
        "environment": "lab",
    }
    s2_diff_text = (
        "--- a/inventory/items.py\n+++ b/inventory/items.py\n"
        "@@ -15,2 +15,5 @@\n"
        "+for item in items:\n"
        "+    db.query(f'SELECT * FROM item_details WHERE id={item.id}')\n"
    )
    (diffs_dir / "dep_s2_nplus1.diff").write_text(s2_diff_text, encoding="utf-8")
    (diffs_dir / "dep_s2_nplus1.json").write_text(
        json.dumps({"service": "inventory", "previous_version": "v2.0.0", "current_version": "v2.1.0"}),
        encoding="utf-8",
    )

    # 3. S4: Benign decoy deploy on orders (frontend banner update, completely unrelated to Redis)
    s4_deploy = {
        "deploy_id": "dep_s4_decoy",
        "service": "orders",
        "version": "v1.2.1",
        "deployed_at": (base_time - timedelta(minutes=2)).isoformat(),  # 09:58
        "git_commit": "commit_s4_decoy",
        "status": "completed",
        "environment": "lab",
    }
    s4_diff_text = (
        "--- a/orders/templates/banner.html\n+++ b/orders/templates/banner.html\n"
        "@@ -1,2 +1,2 @@\n"
        "-<h1>Spring Sale</h1>\n"
        "+<h1>Summer Sale - 20% Off</h1>\n"
    )
    (diffs_dir / "dep_s4_decoy.diff").write_text(s4_diff_text, encoding="utf-8")
    (diffs_dir / "dep_s4_decoy.json").write_text(
        json.dumps({"service": "orders", "previous_version": "v1.2.0", "current_version": "v1.2.1"}),
        encoding="utf-8",
    )

    # 4. Invisible deploy: deployed AFTER window_end (10:15)
    future_deploy = {
        "deploy_id": "dep_future",
        "service": "orders",
        "version": "v1.3.0",
        "deployed_at": (base_time + timedelta(minutes=20)).isoformat(),  # 10:20
        "git_commit": "commit_future",
        "status": "completed",
        "environment": "lab",
    }

    all_deploys = [s1_deploy, s2_deploy, s4_deploy, future_deploy]
    (deploys_dir / "deploys.json").write_text(json.dumps(all_deploys), encoding="utf-8")

    return base_dir


@pytest.fixture
def deploys_env(tmp_path: Path) -> tuple[SnapshotBackend, ToolContext, InMemoryEvidenceStore]:
    snap_path = _build_deploys_snapshot(tmp_path)
    backend = SnapshotBackend(snap_path)
    store = InMemoryEvidenceStore()
    ctx = ToolContext(
        run_id="run_dep_001",
        tenant_id="tenant_alpha",
        incident_id="inc_004",
        window_start=datetime(2026, 3, 30, 9, 30, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc),
        allowed_services={"orders", "inventory", "payment"},
        remaining_calls=8,
    )
    return backend, ctx, store


@pytest.mark.asyncio
async def test_s1_and_s2_culprit_deploys_found(deploys_env: Any) -> None:
    backend, ctx, store = deploys_env

    # S1 orders deploy
    res_s1 = await list_deploys(
        ctx=ctx,
        service="orders",
        backend=backend,
        store=store,
    )
    assert isinstance(res_s1, ToolSuccess)
    s1_ids = [d.deploy_id for d in res_s1.data.deploys]
    assert "dep_s1_pool" in s1_ids

    # S2 inventory deploy
    res_s2 = await list_deploys(
        ctx=ctx,
        service="inventory",
        backend=backend,
        store=store,
    )
    assert isinstance(res_s2, ToolSuccess)
    s2_ids = [d.deploy_id for d in res_s2.data.deploys]
    assert "dep_s2_nplus1" in s2_ids


@pytest.mark.asyncio
async def test_s3_no_relevant_deploys(deploys_env: Any) -> None:
    backend, ctx, store = deploys_env
    # S3 dependency timeout is on payment service, which had no deploys
    res_s3 = await list_deploys(
        ctx=ctx,
        service="payment",
        backend=backend,
        store=store,
    )
    assert isinstance(res_s3, ToolSuccess)
    assert res_s3.data.total_found == 0
    assert len(res_s3.data.deploys) == 0


@pytest.mark.asyncio
async def test_s4_decoy_deploy_and_diff(deploys_env: Any) -> None:
    backend, ctx, store = deploys_env

    # List deploys for orders in S4
    res = await list_deploys(
        ctx=ctx,
        service="orders",
        backend=backend,
        store=store,
    )
    assert isinstance(res, ToolSuccess)
    deploy_ids = [d.deploy_id for d in res.data.deploys]
    assert "dep_s4_decoy" in deploy_ids

    # Fetch diff for S4 decoy deploy (v1.2.0 -> v1.2.1)
    diff_res = await get_deploy_diff(
        ctx=ctx,
        service="orders",
        old_version="v1.2.0",
        new_version="v1.2.1",
        backend=backend,
        store=store,
    )
    assert isinstance(diff_res, ToolSuccess)
    payload: DeployDiffPayload = diff_res.data
    assert "Summer Sale" in payload.diff_text
    # Unrelated to redis
    assert "redis" not in payload.diff_text.lower()


@pytest.mark.asyncio
async def test_invisible_after_window_end_and_unknown_version(deploys_env: Any) -> None:
    backend, ctx, store = deploys_env

    # 1. Version v1.3.0 was deployed at 10:20 (after window_end 10:15) -> invisible (not_found)
    res_future = await get_deploy_diff(
        ctx=ctx,
        service="orders",
        old_version="v1.2.1",
        new_version="v1.3.0",
        backend=backend,
        store=store,
    )
    assert isinstance(res_future, ToolError)
    assert res_future.error.code == ErrorCode.NOT_FOUND

    # 2. Unknown version -> not_found
    res_unknown = await get_deploy_diff(
        ctx=ctx,
        service="orders",
        old_version="v1.0.0",
        new_version="v9.9.9",
        backend=backend,
        store=store,
    )
    assert isinstance(res_unknown, ToolError)
    assert res_unknown.error.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_stale_deploy_metadata_hides_newest_deploy(deploys_env: Any) -> None:
    backend, ctx, store = deploys_env

    # Normal list
    normal_res = await list_deploys(ctx=ctx, service="orders", backend=backend, store=store)
    assert isinstance(normal_res, ToolSuccess)
    normal_first = normal_res.data.deploys[0].deploy_id

    # Degraded list (stale_metadata=True)
    degraded_res = await list_deploys(
        ctx=ctx,
        service="orders",
        backend=backend,
        store=store,
        stale_metadata=True,
    )
    assert isinstance(degraded_res, ToolSuccess)
    degraded_ids = [d.deploy_id for d in degraded_res.data.deploys]
    # The newest deploy (dep_s4_decoy) should be absent
    assert normal_first not in degraded_ids

"""Tests for target-side compare-and-set, deduplication, atomicity, and isolation (Feature 9)."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from lab.common import (
    FaultState,
    LabMetrics,
    LabSettings,
    PrometheusMiddleware,
    VersionProfile,
    VersionProfileRegistry,
    create_admin_router,
    create_metrics_router,
)
from lab.seed.init_db import apply_schema, setup_roles

PG_URL = os.environ.get(
    "TEST_LAB_DB_URL",
    "postgresql://opspilot@127.0.0.1:54333/lab",
)


def _lab_pg_available() -> bool:
    try:
        with psycopg.connect(PG_URL, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _lab_pg_available(), reason="Live lab PostgreSQL not reachable")


def _make_app(
    *,
    hooks_enabled: bool = False,
    log_stream: io.StringIO | None = None,
) -> tuple[FastAPI, FaultState, VersionProfileRegistry, list[str], list[bool]]:
    settings = LabSettings(
        service_name="orders",
        version="v1.3.0",
        lab_admin_token=SecretStr("super-secret-admin-token"),
        telemetry_log_dir="tests/scratch_logs",
        admin_database_url=PG_URL,
        lab_test_hooks_enabled=hooks_enabled,
    )
    fault_state = FaultState()
    reg = VersionProfileRegistry(default_version="v1.3.0")
    reg.register(
        VersionProfile(
            version="v1.0.0",
            features={"release_on_conflict": True},
            commit_sha="0000001",
            changelog="Baseline",
        )
    )
    reg.register(
        VersionProfile(
            version="v1.3.0",
            features={"release_on_conflict": False},
            commit_sha="0000002",
            changelog="Order flow update",
        )
    )

    metrics = LabMetrics()
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware, service_name="orders", metrics=metrics)

    deploy_calls: list[str] = []
    restart_calls: list[bool] = []

    def on_deploy(ver: str) -> None:
        deploy_calls.append(ver)

    def on_restart() -> None:
        restart_calls.append(True)

    if log_stream is not None:
        lab_logger = logging.getLogger("lab.admin")
        handler = logging.StreamHandler(log_stream)
        lab_logger.addHandler(handler)
        lab_logger.setLevel(logging.INFO)

    app.include_router(
        create_admin_router(
            settings=settings,
            fault_state=fault_state,
            version_registry=reg,
            on_deploy=on_deploy,
            on_restart=on_restart,
            admin_db_url=PG_URL,
        )
    )
    app.include_router(create_metrics_router(metrics))
    return app, fault_state, reg, deploy_calls, restart_calls


@pytest.fixture(autouse=True)
def _clean_lab_tables():
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        apply_schema(conn)
        setup_roles(conn, db_name="lab")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE lab.applied_actions, lab.deploys RESTART IDENTITY")


@pytest.mark.asyncio
async def test_rollback_cas_dedup_precondition_and_get():
    """Verifies applied, already_applied, precondition_failed, GET before/after, and unknown fields."""
    app, _, reg, deploy_calls, _ = _make_app()
    headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}
    action_key = f"ak-{uuid4().hex}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://orders:8000") as client:
        # 1. Unknown fields rejected (422)
        bad = await client.post(
            "/admin/actions/rollback",
            headers=headers,
            json={
                "action_key": action_key,
                "expected_current_version": "v1.3.0",
                "to_version": "v1.0.0",
                "extra_field": "nope",
            },
        )
        assert bad.status_code == 422

        # 2. Test hooks disabled by default (404)
        hooks_resp = await client.post("/admin/test-hooks", headers=headers, json={})
        assert hooks_resp.status_code == 404

        # 3. GET before apply -> not_applied
        before = await client.get(f"/admin/actions/{action_key}", headers=headers)
        assert before.status_code == 200
        assert before.json() == {"status": "not_applied"}

        # 4. Precondition failed (wrong expected_current_version)
        wrong_ver = await client.post(
            "/admin/actions/rollback",
            headers=headers,
            json={
                "action_key": action_key,
                "expected_current_version": "v1.1.0",
                "to_version": "v1.0.0",
            },
        )
        assert wrong_ver.status_code == 409
        assert wrong_ver.json()["status"] == "precondition_failed"
        assert reg.active_version == "v1.3.0"
        assert deploy_calls == []

        # 5. First valid rollback -> applied
        applied = await client.post(
            "/admin/actions/rollback",
            headers=headers,
            json={
                "action_key": action_key,
                "expected_current_version": "v1.3.0",
                "to_version": "v1.0.0",
            },
        )
        assert applied.status_code == 200
        body1 = applied.json()
        assert body1["status"] == "applied"
        assert body1["from_version"] == "v1.3.0"
        assert body1["to_version"] == "v1.0.0"
        assert reg.active_version == "v1.0.0"
        assert deploy_calls == ["v1.0.0"]

        # 6. Double call with same action_key -> already_applied (no side effects)
        dup = await client.post(
            "/admin/actions/rollback",
            headers=headers,
            json={
                "action_key": action_key,
                "expected_current_version": "v1.3.0",
                "to_version": "v1.0.0",
            },
        )
        assert dup.status_code == 200
        body2 = dup.json()
        assert body2["status"] == "already_applied"
        assert body2["result"] == body1["result"]
        assert deploy_calls == ["v1.0.0"]

        # 7. GET after apply -> applied
        after = await client.get(f"/admin/actions/{action_key}", headers=headers)
        assert after.status_code == 200
        assert after.json()["status"] == "applied"
        assert after.json()["result"] == body1["result"]

    with psycopg.connect(PG_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM lab.deploys WHERE service = 'orders'")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM lab.applied_actions WHERE action_key = %s", (action_key,))
            assert cur.fetchone()[0] == 1


@pytest.mark.asyncio
async def test_restart_option_a_semantics_and_dedup():
    """Restart clears dependency client faults (Option A), preserves version, and deduplicates."""
    app, fault_state, reg, _, restart_calls = _make_app()
    await fault_state.set_dependency_latency("redis", 300.0)
    await fault_state.set_dependency_error_rate("redis", 0.5)
    await fault_state.set_leak_on_conflict(True)

    headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}
    action_key = f"ak-rst-{uuid4().hex}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://orders:8000") as client:
        res1 = await client.post(
            "/admin/actions/restart",
            headers=headers,
            json={"action_key": action_key, "expected_current_version": "v1.3.0"},
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "applied"
        assert reg.active_version == "v1.3.0"
        assert restart_calls == [True]

        # Option A: dependency latency/error cleared, leak_on_conflict & version preserved
        snap = await fault_state.get_state()
        assert snap["dependency_latency_ms"] == {}
        assert snap["dependency_error_rate"] == {}
        assert snap["leak_on_conflict"] is True

        # State shows restart_count = 1
        state = await client.get("/admin/state", headers=headers)
        assert state.json()["restart_count"] == 1
        assert state.json()["last_restarted_at"] is not None

        # Duplicate restart with same action_key -> already_applied, restart_count stays 1
        res2 = await client.post(
            "/admin/actions/restart",
            headers=headers,
            json={"action_key": action_key, "expected_current_version": "v1.3.0"},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "already_applied"
        assert restart_calls == [True]


@pytest.mark.asyncio
async def test_apply_then_drop_response_still_recorded():
    """drop_response_after_apply simulates transport drop after durable commit."""
    app, _, reg, _, _ = _make_app(hooks_enabled=True)
    headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}
    action_key = f"ak-drop-{uuid4().hex}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://orders:8000") as client:
        cfg = await client.post(
            "/admin/test-hooks",
            headers=headers,
            json={"drop_response_after_apply": True},
        )
        assert cfg.status_code == 200

        with pytest.raises(ConnectionResetError):
            await client.post(
                "/admin/actions/rollback",
                headers=headers,
                json={
                    "action_key": action_key,
                    "expected_current_version": "v1.3.0",
                    "to_version": "v1.0.0",
                },
            )

        assert reg.active_version == "v1.0.0"

        # GET by key confirms it WAS applied durably
        check = await client.get(f"/admin/actions/{action_key}", headers=headers)
        assert check.status_code == 200
        assert check.json()["status"] == "applied"


@pytest.mark.asyncio
async def test_atomicity_failure_between_deploy_and_record():
    """Failure between lab.deploys and lab.applied_actions rolls back BOTH and version."""
    app, _, reg, _, _ = _make_app(hooks_enabled=True)
    headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}
    action_key = f"ak-atom-{uuid4().hex}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://orders:8000") as client:
        await client.post(
            "/admin/test-hooks",
            headers=headers,
            json={"fail_between_deploy_and_record": True},
        )

        with pytest.raises(RuntimeError, match="Simulated failure between deploy and record"):
            await client.post(
                "/admin/actions/rollback",
                headers=headers,
                json={
                    "action_key": action_key,
                    "expected_current_version": "v1.3.0",
                    "to_version": "v1.0.0",
                },
            )

        assert reg.active_version == "v1.3.0"

    with psycopg.connect(PG_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM lab.deploys")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM lab.applied_actions")
            assert cur.fetchone()[0] == 0


@pytest.mark.asyncio
async def test_no_leakage_in_logs_or_metrics():
    """Logs and Prometheus metrics contain zero mentions of broker, action_key, or incident."""
    log_stream = io.StringIO()
    app, _, _, _, _ = _make_app(log_stream=log_stream)
    headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://orders:8000") as client:
        await client.post(
            "/admin/actions/rollback",
            headers=headers,
            json={
                "action_key": "secret_ak_123",
                "expected_current_version": "v1.3.0",
                "to_version": "v1.0.0",
            },
        )
        await client.post(
            "/admin/actions/restart",
            headers=headers,
            json={
                "action_key": "secret_ak_456",
                "expected_current_version": "v1.0.0",
            },
        )
        metrics_resp = await client.get("/metrics")
        assert metrics_resp.status_code == 200

    combined = (log_stream.getvalue() + "\n" + metrics_resp.text).lower()
    for forbidden in ("broker", "action_key", "incident", "secret_ak_", "/admin/actions", "x-lab-admin-token"):
        assert forbidden not in combined, f"Leakage term '{forbidden}' found in logs/metrics"


def test_lab_db_roles_and_compose_network_isolation():
    """Verifies evidence_ro read-only access on lab.applied_actions and Compose network boundaries."""
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE evidence_ro")
            cur.execute("SELECT count(*) FROM lab.applied_actions")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM lab.deploys")
            assert cur.fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO lab.applied_actions (action_key, service, action, from_version) VALUES ('x', 'orders', 'restart_service', 'v1.0.0')"
                )
            cur.execute("RESET ROLE")

            for app_role in ("inventory_ro", "orders_rw"):
                cur.execute(f"SET ROLE {app_role}")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT count(*) FROM lab.applied_actions")
                cur.execute("RESET ROLE")

    # Compose network isolation: core/evidence never join lab-admin
    compose_core = yaml.safe_load(Path("infra/docker-compose.yml").read_text(encoding="utf-8"))
    compose_lab = yaml.safe_load(Path("infra/docker-compose.lab.yml").read_text(encoding="utf-8"))

    for svc_name, svc in compose_core.get("services", {}).items():
        nets = svc.get("networks", [])
        assert "lab-admin" not in nets, f"Core service {svc_name} must not join lab-admin"

    for svc_name in ("gateway", "orders", "inventory"):
        nets = compose_lab["services"][svc_name]["networks"]
        assert "core" not in nets
        assert "evidence" not in nets
        assert "lab-admin" in nets

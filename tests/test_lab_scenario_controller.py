"""Unit and integration tests for OpsPilot Lab Controller, Vault, and Security."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import status
from fastapi.testclient import TestClient

from lab.controller.api import create_controller_app
from lab.controller.models import (
    HealthThresholds,
    TruthRecord,
)
from lab.controller.oracle import Oracle
from lab.controller.scenarios import get_scenario
from lab.controller.service import ControllerService
from lab.controller.settings import ControllerSettings
from lab.controller.vault import Vault


@pytest.fixture
def temp_vault_path(tmp_path: Any) -> str:
    """Fixture providing a clean SQLite vault file path."""
    return str(tmp_path / "test_vault.db")


@pytest.fixture
def test_vault(temp_vault_path: str) -> Vault:
    return Vault(db_path=temp_vault_path)


@pytest.fixture
def test_settings(temp_vault_path: str) -> ControllerSettings:
    return ControllerSettings(
        controller_token="test-controller-token",
        harness_token="test-harness-token",
        lab_admin_token="test-admin-token",
        vault_db_path=temp_vault_path,
        gateway_url="http://mock-gateway:8000",
        orders_url="http://mock-orders:8000",
        inventory_url="http://mock-inventory:8000",
        prometheus_url="http://mock-prometheus:9090",
    )


# -----------------------------------------------------------------------------
# 1. Vault Persistence Tests
# -----------------------------------------------------------------------------
def test_vault_crud(test_vault: Vault) -> None:
    """Test saving and retrieving truth records in the SQLite Vault."""
    truth = TruthRecord(
        run_id="run-123456",
        scenario_id="F1",
        family="connection_leak",
        root_cause={"service": "orders", "component": "db_pool", "mechanism": "leak_on_conflict"},
        culprit_deploy_id="dep-789",
        injected_at="2026-11-23T12:00:00Z",
        expected_action={"action": "rollback", "service": "orders"},
        decoys=[{"service": "inventory", "action": "ping"}],
        health_thresholds={"error_rate": 0.05, "p95_latency_ms": 1000.0},
        split_tag="eval",
        created_at="2026-11-23T12:00:00Z",
        status="running",
    )
    test_vault.record_run(truth)

    retrieved = test_vault.get_run("run-123456")
    assert retrieved is not None
    assert retrieved.run_id == "run-123456"
    assert retrieved.scenario_id == "F1"
    assert retrieved.family == "connection_leak"
    assert retrieved.root_cause["service"] == "orders"
    assert retrieved.culprit_deploy_id == "dep-789"
    assert retrieved.status == "running"

    test_vault.update_status("run-123456", "stopped")
    updated = test_vault.get_run("run-123456")
    assert updated is not None
    assert updated.status == "stopped"


# -----------------------------------------------------------------------------
# 2. Token Security Boundary Tests
# -----------------------------------------------------------------------------
def test_truth_requires_harness_token_only(
    test_settings: ControllerSettings, test_vault: Vault
) -> None:
    """CRITICAL: Truth record is retrievable ONLY with the harness token.
    
    Controllers or agents presenting CONTROLLER_TOKEN or invalid tokens must be rejected.
    """
    app = create_controller_app(settings=test_settings, vault=test_vault)
    client = TestClient(app)

    # Pre-seed a truth record in the vault
    truth = TruthRecord(
        run_id="run-secure-01",
        scenario_id="F1",
        family="connection_leak",
        root_cause={"service": "orders", "component": "db_pool", "mechanism": "leak_on_conflict"},
        culprit_deploy_id="dep-01",
        injected_at="2026-11-23T12:00:00Z",
        expected_action={"action": "rollback", "service": "orders"},
        decoys=[],
        health_thresholds={"error_rate": 0.05, "p95_latency_ms": 1000.0},
        split_tag="eval",
        created_at="2026-11-23T12:00:00Z",
        status="running",
    )
    test_vault.record_run(truth)

    # 1. No token -> 403 Forbidden
    resp = client.get("/runs/run-secure-01/truth")
    assert resp.status_code == status.HTTP_403_FORBIDDEN

    # 2. Wrong token -> 403 Forbidden
    resp = client.get("/runs/run-secure-01/truth", headers={"X-Harness-Token": "invalid-token"})
    assert resp.status_code == status.HTTP_403_FORBIDDEN

    # 3. CONTROLLER_TOKEN (agent/controller credentials) -> 403 Forbidden
    resp = client.get(
        "/runs/run-secure-01/truth",
        headers={"X-Controller-Token": "test-controller-token"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN

    # 4. Valid HARNESS_TOKEN -> 200 OK with full ground truth
    resp = client.get(
        "/runs/run-secure-01/truth",
        headers={"X-Harness-Token": "test-harness-token"},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["run_id"] == "run-secure-01"
    assert data["root_cause"]["mechanism"] == "leak_on_conflict"


def test_runs_endpoint_returns_run_id_only(
    test_settings: ControllerSettings, test_vault: Vault
) -> None:
    """Assert POST /runs returns only run_id and never discloses ground truth."""
    app = create_controller_app(settings=test_settings, vault=test_vault)
    client = TestClient(app)

    with patch.object(ControllerService, "reset_world", new_callable=AsyncMock) as mock_reset, \
         patch.object(ControllerService, "_run_background_load", new_callable=AsyncMock), \
         patch("lab.controller.service.deploy", new_callable=AsyncMock) as mock_deploy:
        mock_reset.return_value = {"status": "reset"}
        mock_deploy.return_value.deploy_id = "dep-mock-123"

        resp = client.post(
            "/runs",
            json={"scenario_id": "F1", "seed": 42, "warmup_s": 0.0},
            headers={"X-Controller-Token": "test-controller-token"},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        # Verify returned schema: strictly run_id, scenario_id, status
        assert "run_id" in data
        assert data["scenario_id"] == "F1"
        assert data["status"] == "running"
        # Assert ground truth fields are NEVER exposed to the caller
        assert "root_cause" not in data
        assert "expected_action" not in data
        assert "culprit_deploy_id" not in data
        assert "family" not in data


# -----------------------------------------------------------------------------
# 3. Idempotent Reset Tests
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reset_is_idempotent(test_settings: ControllerSettings, test_vault: Vault) -> None:
    """Assert repeated world resets execute without side effects and maintain baseline."""
    service = ControllerService(settings=test_settings, vault=test_vault)

    # Mock admin calls
    mock_post = AsyncMock()
    mock_resp = httpx.Response(200, json={"status": "reset"})
    mock_post.return_value = mock_resp

    with patch.object(httpx.AsyncClient, "post", mock_post):
        res1 = await service.reset_world()
        res2 = await service.reset_world()
        res3 = await service.reset_world()

        assert res1["status"] == "world_reset_complete"
        assert res2["status"] == "world_reset_complete"
        assert res3["status"] == "world_reset_complete"
        # Called 3 times for each of the 3 services = 9 calls total
        assert mock_post.call_count == 9


# -----------------------------------------------------------------------------
# 4. Scenario Determinism Tests
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_runs_of_same_scenario_produce_identical_truth(
    test_settings: ControllerSettings, test_vault: Vault
) -> None:
    """Assert two runs of the same scenario produce the identical truth record."""
    service = ControllerService(settings=test_settings, vault=test_vault)

    with patch.object(ControllerService, "reset_world", new_callable=AsyncMock), \
         patch.object(ControllerService, "_run_background_load", new_callable=AsyncMock), \
         patch("lab.controller.service.deploy", new_callable=AsyncMock) as mock_deploy:
        mock_deploy.return_value.deploy_id = "dep-fixed-456"

        run_id_1 = await service.start_run("F1", seed=100, warmup_s=0.0)
        run_id_2 = await service.start_run("F1", seed=100, warmup_s=0.0)

        truth_1 = service.get_truth(run_id_1)
        truth_2 = service.get_truth(run_id_2)

        assert truth_1 is not None and truth_2 is not None
        assert truth_1.scenario_id == truth_2.scenario_id == "F1"
        assert truth_1.family == truth_2.family == "connection_leak"
        assert truth_1.root_cause == truth_2.root_cause
        assert truth_1.expected_action == truth_2.expected_action
        assert truth_1.health_thresholds == truth_2.health_thresholds
        assert truth_1.split_tag == truth_2.split_tag
        assert truth_1.decoys == truth_2.decoys


# -----------------------------------------------------------------------------
# 5. Multiple Simultaneous Faults Injection (Requirement 4)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multiple_simultaneous_faults_injection(
    test_settings: ControllerSettings, test_vault: Vault
) -> None:
    """Verify that multiple simultaneous faults in one scenario run are applied concurrently."""
    scenario = get_scenario("M1_MULTI_FAULT")
    assert len(scenario.faults) == 2
    assert scenario.faults[0].service == "orders"
    assert scenario.faults[1].service == "inventory"

    service = ControllerService(settings=test_settings, vault=test_vault)

    posted_faults: list[dict[str, Any]] = []

    async def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        if "/admin/faults" in url:
            posted_faults.append({"url": url, "payload": kwargs.get("json")})
        return httpx.Response(200, json={"status": "faults_updated"})

    with patch.object(ControllerService, "reset_world", new_callable=AsyncMock), \
         patch.object(ControllerService, "_run_background_load", new_callable=AsyncMock), \
         patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
        run_id = await service.start_run("M1_MULTI_FAULT", seed=7, warmup_s=0.0)

        assert len(posted_faults) == 2
        # Check orders DB fault
        assert any(
            "orders" in f["url"] and f["payload"].get("dependency_error_rate") == {"db": 0.15}
            for f in posted_faults
        )
        # Check inventory Redis latency fault
        assert any(
            "inventory" in f["url"]
            and f["payload"].get("dependency_latency_ms") == {"redis": 250.0}
            for f in posted_faults
        )

        truth = service.get_truth(run_id)
        assert truth is not None
        assert truth.family == "multi_fault"


# -----------------------------------------------------------------------------
# 6. Oracle Evaluator Tests
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_oracle_evaluates_healthy_vs_unhealthy() -> None:
    """Assert Oracle correctly flags healthy within thresholds and unhealthy when breached."""
    oracle = Oracle(prometheus_url="http://mock-prometheus:9090")
    thresholds = HealthThresholds(error_rate=0.05, p95_latency_ms=500.0)

    # Case A: Healthy metrics (0.01 error rate, 120ms p95 latency)
    with patch.object(Oracle, "query_promql", side_effect=[0.01, 120.0]):
        result_healthy = await oracle.oracle_health(thresholds, window="1m")
        assert result_healthy.healthy is True
        assert result_healthy.error_rate == 0.01
        assert result_healthy.p95_latency_ms == 120.0

    # Case B: Unhealthy error rate breach (0.12 > 0.05)
    with patch.object(Oracle, "query_promql", side_effect=[0.12, 180.0]):
        result_error_breach = await oracle.oracle_health(thresholds, window="1m")
        assert result_error_breach.healthy is False
        assert result_error_breach.details["error_rate_breached"] is True
        assert result_error_breach.details["p95_latency_breached"] is False

    # Case C: Unhealthy latency breach (650ms > 500ms)
    with patch.object(Oracle, "query_promql", side_effect=[0.02, 650.0]):
        result_latency_breach = await oracle.oracle_health(thresholds, window="1m")
        assert result_latency_breach.healthy is False
        assert result_latency_breach.details["error_rate_breached"] is False
        assert result_latency_breach.details["p95_latency_breached"] is True


# -----------------------------------------------------------------------------
# 7. Network Isolation & Boundary Tests
# -----------------------------------------------------------------------------
def test_network_isolation_configuration() -> None:
    """Verifies docker-compose.lab.yml configuration ensures the controller is ONLY on lab-admin."""
    import yaml  # type: ignore[import-untyped]

    compose_path = os.path.join(os.path.dirname(__file__), "..", "infra", "docker-compose.lab.yml")
    assert os.path.exists(compose_path)

    with open(compose_path, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    controller = services.get("controller")
    assert controller is not None, "controller service must be defined in docker-compose.lab.yml"

    # Controller must attach ONLY to lab-admin network
    controller_networks = controller.get("networks", [])
    assert controller_networks == ["lab-admin"], (
        f"Controller must be attached strictly to ['lab-admin'], found: {controller_networks}"
    )

    # Controller must never publish host ports
    assert "ports" not in controller, "Controller must never publish ports to host"

    # Vault volume must be mounted ONLY into controller
    vault_mounted_services = [
        name for name, svc in services.items()
        if any("vault" in str(v) for v in svc.get("volumes", []))
    ]
    assert vault_mounted_services == ["controller"], (
        f"Vault volume must be mounted strictly into controller, found: {vault_mounted_services}"
    )

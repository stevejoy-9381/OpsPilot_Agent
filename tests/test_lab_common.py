import io
import json
import logging
from pathlib import Path

import pytest
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
    setup_lab_logging,
    validate_neutral_version,
)


@pytest.fixture
def lab_settings() -> LabSettings:
    return LabSettings(
        service_name="test-orders",
        version="v1.0.0",
        lab_admin_token=SecretStr("super-secret-admin-token"),
        telemetry_log_dir="tests/scratch_logs",
    )


@pytest.fixture
def fault_state() -> FaultState:
    return FaultState()


@pytest.fixture
def version_registry() -> VersionProfileRegistry:
    reg = VersionProfileRegistry(default_version="v1.0.0")
    reg.register(
        VersionProfile(
            version="v1.1.0",
            features={"batch_mode": True},
            commit_sha="a1b2c3d",
            changelog="Update batch mode",
        )
    )
    return reg


@pytest.fixture
def test_app(
    lab_settings: LabSettings,
    fault_state: FaultState,
    version_registry: VersionProfileRegistry,
) -> FastAPI:
    app = FastAPI()
    metrics = LabMetrics()
    app.add_middleware(
        PrometheusMiddleware,
        service_name=lab_settings.service_name,
        metrics=metrics,
    )

    deploy_invocations: list[str] = []
    reset_invocations: list[bool] = []

    def on_deploy(ver: str) -> None:
        deploy_invocations.append(ver)

    def on_reset() -> None:
        reset_invocations.append(True)

    admin_router = create_admin_router(
        settings=lab_settings,
        fault_state=fault_state,
        version_registry=version_registry,
        on_deploy=on_deploy,
        on_reset=on_reset,
    )
    metrics_router = create_metrics_router(metrics)

    app.include_router(admin_router)
    app.include_router(metrics_router)

    # Add a normal application route to verify metrics collection
    @app.get("/items/{item_id}")
    def get_item(item_id: str) -> dict[str, str]:
        # Emulate some dependency and pool activity
        metrics.dependency_call_duration_seconds.labels(dependency="redis").observe(0.012)
        metrics.dependency_errors_total.labels(dependency="redis").inc()
        metrics.db_pool_in_use.labels(service="test-orders").set(3)
        metrics.db_pool_size.labels(service="test-orders").set(10)
        metrics.db_pool_wait_seconds.labels(service="test-orders").observe(0.005)
        return {"item": item_id}

    # Store references for inspection in tests
    app.state.deploy_invocations = deploy_invocations
    app.state.reset_invocations = reset_invocations
    app.state.metrics = metrics

    return app


@pytest.mark.asyncio
async def test_admin_auth_rejects_bad_missing_token(test_app: FastAPI) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Missing header
        res_missing = await client.get("/admin/state")
        assert res_missing.status_code == 401
        assert "Invalid or missing" in res_missing.json()["detail"]

        # 2. Bad token header
        res_bad = await client.get(
            "/admin/state",
            headers={"X-Lab-Admin-Token": "completely-wrong-token"},
        )
        assert res_bad.status_code == 401
        assert "Invalid or missing" in res_bad.json()["detail"]

        # 3. Correct token
        res_ok = await client.get(
            "/admin/state",
            headers={"X-Lab-Admin-Token": "super-secret-admin-token"},
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["service"] == "test-orders"
        assert data["version"] == "v1.0.0"


@pytest.mark.asyncio
async def test_deploy_switches_profile(
    test_app: FastAPI,
    version_registry: VersionProfileRegistry,
) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}

        # Attempt unknown version
        res_unknown = await client.post(
            "/admin/deploy",
            json={"version": "v9.9.9"},
            headers=headers,
        )
        assert res_unknown.status_code == 400

        # Deploy registered version
        res_deploy = await client.post(
            "/admin/deploy",
            json={"version": "v1.1.0"},
            headers=headers,
        )
        assert res_deploy.status_code == 200
        assert res_deploy.json()["version"] == "v1.1.0"
        assert version_registry.active_version == "v1.1.0"
        assert test_app.state.deploy_invocations == ["v1.1.0"]

        # Query state to confirm
        res_state = await client.get("/admin/state", headers=headers)
        assert res_state.json()["version"] == "v1.1.0"
        assert res_state.json()["profile"]["commit_sha"] == "a1b2c3d"


@pytest.mark.asyncio
async def test_reset_clears_faults(
    test_app: FastAPI,
    fault_state: FaultState,
    version_registry: VersionProfileRegistry,
) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}

        # Switch version and inject faults
        await client.post("/admin/deploy", json={"version": "v1.1.0"}, headers=headers)
        res_fault = await client.post(
            "/admin/faults",
            json={
                "dependency_latency_ms": {"redis": 250.0},
                "dependency_error_rate": {"redis": 0.5},
                "leak_on_conflict": True,
            },
            headers=headers,
        )
        assert res_fault.status_code == 200

        # Verify active faults
        state_before = (await client.get("/admin/state", headers=headers)).json()
        assert state_before["faults"]["dependency_latency_ms"]["redis"] == 250.0
        assert state_before["faults"]["dependency_error_rate"]["redis"] == 0.5
        assert state_before["faults"]["leak_on_conflict"] is True
        assert state_before["version"] == "v1.1.0"

        # Execute Reset
        res_reset = await client.post("/admin/reset", headers=headers)
        assert res_reset.status_code == 200
        assert test_app.state.reset_invocations == [True]

        # Verify clean state
        state_after = (await client.get("/admin/state", headers=headers)).json()
        assert state_after["faults"]["dependency_latency_ms"] == {}
        assert state_after["faults"]["dependency_error_rate"] == {}
        assert state_after["faults"]["leak_on_conflict"] is False
        assert state_after["version"] == "v1.0.0"


@pytest.mark.asyncio
async def test_log_output_contains_no_fault_keywords_after_injection(
    test_app: FastAPI,
    tmp_path: Path,
) -> None:
    # Setup custom logging stream to capture log lines
    log_dir = str(tmp_path)
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    setup_lab_logging(
        service_name="test-orders",
        version="v1.0.0",
        log_dir=log_dir,
        log_level="INFO",
    )
    root = logging.getLogger()
    root.addHandler(handler)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Lab-Admin-Token": "super-secret-admin-token"}

        # Inject faults and deploy
        await client.post(
            "/admin/faults",
            json={
                "dependency_latency_ms": {"redis": 3000.0},
                "dependency_error_rate": {"redis": 0.8},
                "leak_on_conflict": True,
            },
            headers=headers,
        )
        await client.post("/admin/deploy", json={"version": "v1.1.0"}, headers=headers)
        await client.post("/admin/reset", headers=headers)

    # Verify structured JSON file was written
    jsonl_file = tmp_path / "test-orders.jsonl"
    assert jsonl_file.exists()
    file_content = jsonl_file.read_text(encoding="utf-8")
    assert len(file_content.strip()) > 0

    lines = [json.loads(line) for line in file_content.strip().split("\n") if line.strip()]
    for entry in lines:
        assert "ts" in entry
        assert "service" in entry
        assert "version" in entry
        assert "level" in entry
        assert "event" in entry
        assert "msg" in entry

    # Verify no tell-tale fault keywords leaked into any log line or file
    forbidden_leak_words = [
        "leak",
        "pool_leak",
        "latency_regression",
        "dependency_timeout",
        "false_alarm",
        "buggy",
        "bad",
        "defect",
        "broken",
    ]
    file_lower = file_content.lower()
    for word in forbidden_leak_words:
        assert word not in file_lower, (
            f"Forbidden leak keyword '{word}' detected in log file: {file_content}"
        )

    # Clean up file handler to release file locks
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_all_metric_names(test_app: FastAPI) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Trigger an application request to generate data
        item_res = await client.get("/items/sku-12345")
        assert item_res.status_code == 200

        # Scrape metrics
        metrics_res = await client.get("/metrics")
        assert metrics_res.status_code == 200
        content = metrics_res.text

        # All metric names explicitly required by specification
        required_metrics = [
            "http_requests_total",
            "http_request_duration_seconds",
            "dependency_call_duration_seconds",
            "dependency_errors_total",
            "db_pool_in_use",
            "db_pool_size",
            "db_pool_wait_seconds",
        ]

        for metric in required_metrics:
            assert metric in content, (
                f"Required metric '{metric}' missing from /metrics output:\n{content}"
            )

        # Verify admin routes are excluded from metrics labels
        assert 'route="/admin' not in content


def test_neutral_version_validation() -> None:
    # Valid neutral versions
    assert validate_neutral_version("v1.0.0") == "v1.0.0"
    assert validate_neutral_version("v1.2.3") == "v1.2.3"
    assert validate_neutral_version("2026.11.1") == "2026.11.1"

    # Non-neutral versions must raise ValueError
    with pytest.raises(ValueError, match="forbidden non-neutral term 'bad'"):
        validate_neutral_version("v1.1.0-bad")

    with pytest.raises(ValueError, match="forbidden non-neutral term 'leak'"):
        validate_neutral_version("v1.0.0-pool-leak")

    with pytest.raises(ValueError, match="forbidden non-neutral term 'buggy'"):
        validate_neutral_version("v1.0.1-buggy")

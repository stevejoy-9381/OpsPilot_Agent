from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator, Generator
from urllib.parse import urlparse, urlsplit, urlunsplit

import psycopg
import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apps.api.settings import get_settings
from lab.common.faults import FaultState
from lab.common.metrics import LabMetrics
from lab.seed.seed import seed_database
from lab.services.inventory.client import (
    InventoryStore,
    create_default_version_registry,
)
from lab.services.inventory.main import create_inventory_app
from lab.services.inventory.settings import InventorySettings


@pytest.fixture(scope="module")
def seeded_db_url() -> Generator[str, None, None]:
    """Provisions and seeds a test database with deterministic seed 42."""
    admin_url = os.getenv("DATABASE_URL")
    if not admin_url:
        admin_url = get_settings().database_url.get_secret_value()
    clean_admin_url = admin_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(clean_admin_url)
    test_db_name = "lab_inventory_test_db"

    # Connect with autocommit to create clean test database
    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
            cur.execute(f"CREATE DATABASE {test_db_name};")

    test_db_url = urlunsplit(parsed._replace(path=f"/{test_db_name}"))

    # Seed the database
    seed_database(test_db_url, seed=42, init_roles=True)

    yield test_db_url

    # Teardown database
    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{test_db_name}' AND pid <> pg_backend_pid();
                """
            )
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name};")


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
async def inventory_app(
    seeded_db_url: str,
    span_exporter: InMemorySpanExporter,
) -> AsyncGenerator[FastAPI, None]:
    # Construct inventory_ro URL
    parsed = urlparse(seeded_db_url)
    inv_pw = os.getenv("LAB_INVENTORY_RO_PASSWORD", "inv-ro-local-pw")
    db_name = parsed.path.lstrip("/")
    inv_db_url = f"postgresql://inventory_ro:{inv_pw}@{parsed.hostname}:{parsed.port}/{db_name}"

    redis_url = os.getenv("LAB_REDIS_URL", "redis://127.0.0.1:6379/1")

    # Clear test Redis keys
    r: aioredis.Redis[str] = aioredis.from_url(redis_url, decode_responses=True)
    await r.flushdb()
    await r.close()

    settings = InventorySettings(
        service_name="inventory",
        version="v1.0.0",
        database_url=inv_db_url,
        redis_url=redis_url,
        db_pool_size=10,
        redis_timeout_s=0.150,
        cache_ttl_s=60,
    )
    metrics = LabMetrics()
    fault_state = FaultState()
    registry = create_default_version_registry()

    store = InventoryStore(
        settings=settings,
        fault_state=fault_state,
        metrics=metrics,
        version_registry=registry,
    )
    await store.open()

    app = create_inventory_app(settings=settings, store=store, metrics=metrics)
    app.state.span_exporter = span_exporter

    yield app

    await store.close()


@pytest.mark.asyncio
async def test_cache_miss_then_hit_path(inventory_app: FastAPI) -> None:
    transport = ASGITransport(app=inventory_app)
    span_exporter: InMemorySpanExporter = inventory_app.state.span_exporter

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Request 1: Cache Miss -> reads DB and sets cache
        span_exporter.clear()
        res1 = await client.get("/stock/1")
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["product_id"] == 1
        assert "qty" in data1

        spans_req1 = [s.name for s in span_exporter.get_finished_spans()]
        assert "redis.get" in spans_req1
        assert "db.query" in spans_req1
        assert "redis.set" in spans_req1

        # Request 2: Cache Hit -> reads Redis, zero DB queries
        span_exporter.clear()
        res2 = await client.get("/stock/1")
        assert res2.status_code == 200
        assert res2.json()["qty"] == data1["qty"]

        spans_req2 = [s.name for s in span_exporter.get_finished_spans()]
        assert "redis.get" in spans_req2
        assert "db.query" not in spans_req2
        assert "redis.set" not in spans_req2


@pytest.mark.asyncio
async def test_stock_not_found_returns_404(inventory_app: FastAPI) -> None:
    transport = ASGITransport(app=inventory_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Non-existent product ID
        res = await client.get("/stock/999999")
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_v120_regression_bypasses_cache_and_issues_extra_db_queries(
    inventory_app: FastAPI,
) -> None:
    transport = ASGITransport(app=inventory_app)
    span_exporter: InMemorySpanExporter = inventory_app.state.span_exporter
    admin_headers = {"X-Lab-Admin-Token": "lab-admin-dev-token"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Warm cache on v1.0.0
        await client.get("/stock/2")
        span_exporter.clear()

        # Query on v1.0.0 (warm cache) produces 0 db.query spans
        res_v100 = await client.get("/stock/2")
        assert res_v100.status_code == 200
        spans_v100 = [s.name for s in span_exporter.get_finished_spans()]
        db_queries_v100 = spans_v100.count("db.query")
        assert db_queries_v100 == 0

        # 2. Deploy v1.2.0 (neutral profile name carrying F2 regression)
        res_deploy = await client.post(
            "/admin/deploy",
            json={"version": "v1.2.0"},
            headers=admin_headers,
        )
        assert res_deploy.status_code == 200

        # Query on v1.2.0: cache is bypassed, and extra query is executed
        span_exporter.clear()
        res_v120 = await client.get("/stock/2")
        assert res_v120.status_code == 200

        spans_v120 = [s.name for s in span_exporter.get_finished_spans()]
        db_queries_v120 = spans_v120.count("db.query")

        # Measurably more db.query spans per request than v1.0.0 (2 vs 0)
        assert db_queries_v120 > db_queries_v100
        assert db_queries_v120 == 2
        assert "redis.get" not in spans_v120

        # Reset back to v1.0.0
        await client.post("/admin/reset", headers=admin_headers)


@pytest.mark.asyncio
async def test_redis_latency_fault_and_timeout_fallback(inventory_app: FastAPI) -> None:
    transport = ASGITransport(app=inventory_app)
    admin_headers = {"X-Lab-Admin-Token": "lab-admin-dev-token"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Warm cache for product 3
        await client.get("/stock/3")

        # 2. Inject 60ms latency on Redis (< 150ms timeout)
        await client.post(
            "/admin/faults",
            json={"dependency_latency_ms": {"redis": 60.0}},
            headers=admin_headers,
        )

        t0 = time.perf_counter()
        res_fast = await client.get("/stock/3")
        duration = time.perf_counter() - t0
        assert res_fast.status_code == 200
        assert duration >= 0.050  # Latency measurably observed

        # 3. Inject 200ms latency on Redis (> 150ms timeout)
        await client.post(
            "/admin/faults",
            json={"dependency_latency_ms": {"redis": 200.0}},
            headers=admin_headers,
        )

        # Request must NOT fail; it falls back to DB and returns HTTP 200
        res_fallback = await client.get("/stock/3")
        assert res_fallback.status_code == 200

        # Verify dependency error was recorded for Redis timeout
        metrics_res = await client.get("/metrics")
        assert 'dependency_errors_total{dependency="redis"}' in metrics_res.text

        # Reset faults
        await client.post("/admin/reset", headers=admin_headers)


@pytest.mark.asyncio
async def test_healthz_endpoint(inventory_app: FastAPI) -> None:
    transport = ASGITransport(app=inventory_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/healthz")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["service"] == "inventory"
        assert data["version"] == "v1.0.0"

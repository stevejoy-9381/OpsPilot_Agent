from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator, Callable, Generator
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import pytest
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
from lab.services.orders.client import OrdersStore, create_default_version_registry
from lab.services.orders.main import create_orders_app
from lab.services.orders.settings import OrdersSettings


@pytest.fixture(scope="module")
def seeded_db_url() -> Generator[str, None, None]:
    """Provisions and seeds a test database for orders testing."""
    admin_url = os.getenv("DATABASE_URL")
    if not admin_url:
        admin_url = get_settings().database_url.get_secret_value()
    clean_admin_url = admin_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(clean_admin_url)
    test_db_name = "lab_orders_test_db"

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


def make_mock_inventory_handler(
    inventory_catalog: dict[int, int],
    captured_headers: list[dict[str, str]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Returns an httpx.MockTransport handler simulating the inventory service."""
    def handler(request: httpx.Request) -> httpx.Response:
        if captured_headers is not None:
            captured_headers.append(dict(request.headers))

        path = request.url.path
        if path.startswith("/stock/"):
            try:
                product_id = int(path.split("/")[-1])
            except ValueError:
                return httpx.Response(400, json={"detail": "Invalid product ID"})

            if product_id not in inventory_catalog:
                return httpx.Response(404, json={"detail": "Product not found"})

            return httpx.Response(
                200,
                json={"product_id": product_id, "qty": inventory_catalog[product_id]},
            )
        return httpx.Response(404, json={"detail": "Not found"})
    return handler


@pytest.fixture
async def orders_app_fixture(
    seeded_db_url: str,
) -> AsyncGenerator[tuple[FastAPI, OrdersStore, dict[int, int], list[dict[str, str]]], None]:
    """Provisions a test FastAPI orders app configured with orders_rw role and mock inventory."""
    parsed = urlsplit(seeded_db_url)
    user_pass = "orders_rw:orders-rw-local-pw"
    db_url_with_role = urlunsplit(
        parsed._replace(netloc=f"{user_pass}@{parsed.hostname}:{parsed.port}")
    )

    settings = OrdersSettings(
        database_url=db_url_with_role,
        inventory_url="http://mock-inventory",
        db_pool_size=3,
        db_pool_timeout_s=0.25,
        inventory_timeout_s=0.5,
        inventory_retries=1,
    )
    metrics = LabMetrics()
    fault_state = FaultState()
    version_registry = create_default_version_registry()

    catalog = {1: 50, 2: 0, 3: 5}
    captured_headers: list[dict[str, str]] = []
    mock_transport = httpx.MockTransport(
        make_mock_inventory_handler(catalog, captured_headers)
    )
    mock_inventory_client = httpx.AsyncClient(
        transport=mock_transport,
        base_url=settings.inventory_url,
    )

    store = OrdersStore(
        settings=settings,
        fault_state=fault_state,
        metrics=metrics,
        version_registry=version_registry,
        inventory_client=mock_inventory_client,
    )

    app = create_orders_app(
        settings=settings,
        store=store,
        metrics=metrics,
    )

    await store.open()
    yield app, store, catalog, captured_headers
    await store.close()


@pytest.mark.asyncio
async def test_orders_201_path_and_spans(
    orders_app_fixture: tuple[FastAPI, OrdersStore, dict[int, int], list[dict[str, str]]],
    span_exporter: InMemorySpanExporter,
) -> None:
    """Verifies 201 path: stock >= qty inserts order and returns 201 with exact spans."""
    app, store, _, _ = orders_app_fixture
    store.version_registry.set_active_version("v1.0.0")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/orders",
            json={"product_id": 1, "qty": 2},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["product_id"] == 1
        assert data["qty"] == 2
        assert data["status"] == "CONFIRMED"
        assert "id" in data
        assert "created_at" in data

    # Verify exact spans: orders.handler, inventory.call, db.insert
    spans = span_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "orders.handler" in span_names
    assert "inventory.call" in span_names
    assert "db.insert" in span_names

    # Verify connection returned to pool (pool in use is 0)
    assert store.metrics.db_pool_in_use.labels(service="orders")._value.get() == 0


@pytest.mark.asyncio
async def test_orders_409_insufficient_stock_path(
    orders_app_fixture: tuple[FastAPI, OrdersStore, dict[int, int], list[dict[str, str]]],
) -> None:
    """Verifies 409 path: stock < qty returns 409 Conflict."""
    app, store, _, _ = orders_app_fixture
    store.version_registry.set_active_version("v1.0.0")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Product 3 has stock 5, request qty 10
        response = await client.post(
            "/orders",
            json={"product_id": 3, "qty": 10},
        )
        assert response.status_code == 409
        assert "insufficient" in response.json()["detail"].lower()

    # Under v1.0.0, connection is released in finally block
    assert store.metrics.db_pool_in_use.labels(service="orders")._value.get() == 0


@pytest.mark.asyncio
async def test_v100_never_exhausts_pool_on_repeated_409s(
    orders_app_fixture: tuple[FastAPI, OrdersStore, dict[int, int], list[dict[str, str]]],
) -> None:
    """Verifies that under v1.0.0, repeated 409s always release connections cleanly."""
    app, store, _, _ = orders_app_fixture
    store.version_registry.set_active_version("v1.0.0")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Pool size is 3; send 10 consecutive requests on the 409 path
        for _ in range(10):
            res = await client.post("/orders", json={"product_id": 2, "qty": 1})
            assert res.status_code == 409

        # In-use connections stay flat at 0
        assert store.metrics.db_pool_in_use.labels(service="orders")._value.get() == 0
        assert len(store._leaked_connections) == 0

        # Subsequent 201 order succeeds immediately
        ok_res = await client.post("/orders", json={"product_id": 1, "qty": 1})
        assert ok_res.status_code == 201


@pytest.mark.asyncio
async def test_v130_exhausts_pool_on_409s_and_produces_503_pool_timeout(
    orders_app_fixture: tuple[FastAPI, OrdersStore, dict[int, int], list[dict[str, str]]],
) -> None:
    """Verifies that under v1.3.0, 409s leak connections until pool exhaustion produces 503."""
    app, store, _, _ = orders_app_fixture
    store.version_registry.set_active_version("v1.3.0")
    assert store.version_registry.get_active_profile().features["release_on_conflict"] is False

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Pool size is 3; send 3 requests that hit the 409 path (product 2 has qty 0)
        for i in range(3):
            res = await client.post("/orders", json={"product_id": 2, "qty": 1})
            assert res.status_code == 409
            assert len(store._leaked_connections) == i + 1
            assert store.metrics.db_pool_in_use.labels(service="orders")._value.get() == i + 1

        # Pool is now 3/3 exhausted.
        # Next request must wait for timeout (0.25s) and return 503 ("pool timeout")
        t0 = time.perf_counter()
        resp_503 = await client.post("/orders", json={"product_id": 1, "qty": 1})
        dur = time.perf_counter() - t0

        assert resp_503.status_code == 503
        assert dur >= 0.20
        assert "pool timeout" in resp_503.json()["detail"].lower()


@pytest.mark.asyncio
async def test_trace_context_propagates_to_inventory(
    orders_app_fixture: tuple[FastAPI, OrdersStore, dict[int, int], list[dict[str, str]]],
) -> None:
    """Verifies W3C traceparent header propagates to downstream inventory call."""
    app, store, _, captured_headers = orders_app_fixture
    store.version_registry.set_active_version("v1.0.0")
    captured_headers.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/orders", json={"product_id": 1, "qty": 1})
        assert res.status_code == 201

    assert len(captured_headers) > 0
    last_header = captured_headers[-1]
    assert "traceparent" in last_header
    assert last_header["traceparent"].startswith("00-")


@pytest.mark.asyncio
async def test_deploy_resets_pool(
    orders_app_fixture: tuple[FastAPI, OrdersStore, dict[int, int], list[dict[str, str]]],
) -> None:
    """Verifies that an authenticated deploy / rollback resets the pool state."""
    app, store, _, _ = orders_app_fixture
    store.version_registry.set_active_version("v1.3.0")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Leak 2 connections on 409 path
        await client.post("/orders", json={"product_id": 2, "qty": 1})
        await client.post("/orders", json={"product_id": 2, "qty": 1})
        assert len(store._leaked_connections) == 2
        assert store.metrics.db_pool_in_use.labels(service="orders")._value.get() == 2

        # Deploy v1.0.0
        headers = {"X-Lab-Admin-Token": store.settings.lab_admin_token.get_secret_value()}
        deploy_res = await client.post(
            "/admin/deploy",
            headers=headers,
            json={"version": "v1.0.0"},
        )
        assert deploy_res.status_code == 200

        # Pool is drained and reset
        assert len(store._leaked_connections) == 0
        assert store.metrics.db_pool_in_use.labels(service="orders")._value.get() == 0

        # Subsequent order works cleanly
        res = await client.post("/orders", json={"product_id": 1, "qty": 1})
        assert res.status_code == 201

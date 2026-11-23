from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
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
from lab.services.gateway.client import GatewayClient, create_default_version_registry
from lab.services.gateway.main import create_gateway_app
from lab.services.gateway.settings import GatewaySettings
from lab.services.inventory.client import InventoryStore
from lab.services.inventory.main import create_inventory_app
from lab.services.inventory.settings import InventorySettings
from lab.services.orders.client import OrdersStore
from lab.services.orders.main import create_orders_app
from lab.services.orders.settings import OrdersSettings


@pytest.fixture(scope="module")
def seeded_db_url() -> Generator[str, None, None]:
    """Provisions and seeds a test database for 3-service integration testing."""
    admin_url = os.getenv("DATABASE_URL")
    if not admin_url:
        admin_url = get_settings().database_url.get_secret_value()
    clean_admin_url = admin_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(clean_admin_url)
    test_db_name = "lab_gateway_test_db"

    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
            cur.execute(f"CREATE DATABASE {test_db_name};")

    test_db_url = urlunsplit(parsed._replace(path=f"/{test_db_name}"))
    seed_database(test_db_url, seed=42, init_roles=True)

    yield test_db_url

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
async def full_lab_stack_fixture(
    seeded_db_url: str,
) -> AsyncGenerator[tuple[FastAPI, FastAPI, FastAPI], None]:
    """Wires all 3 services in-memory: gateway -> orders -> inventory."""
    parsed = urlsplit(seeded_db_url)

    # 1. Inventory Service
    inv_role = "inventory_ro:inv-ro-local-pw"
    inv_db_url = urlunsplit(
        parsed._replace(netloc=f"{inv_role}@{parsed.hostname}:{parsed.port}")
    )
    inv_settings = InventorySettings(database_url=inv_db_url)
    inv_store = InventoryStore(
        settings=inv_settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
    )
    inv_app = create_inventory_app(settings=inv_settings, store=inv_store)
    await inv_store.open()

    inv_transport = ASGITransport(app=inv_app)
    orders_inv_client = AsyncClient(transport=inv_transport, base_url="http://inventory")

    # 2. Orders Service
    orders_role = "orders_rw:orders-rw-local-pw"
    orders_db_url = urlunsplit(
        parsed._replace(netloc=f"{orders_role}@{parsed.hostname}:{parsed.port}")
    )
    orders_settings = OrdersSettings(
        database_url=orders_db_url,
        inventory_url="http://inventory",
    )
    orders_store = OrdersStore(
        settings=orders_settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
        inventory_client=orders_inv_client,
    )
    orders_app = create_orders_app(settings=orders_settings, store=orders_store)
    await orders_store.open()

    gw_orders_client = AsyncClient(
        transport=ASGITransport(app=orders_app),
        base_url="http://orders",
    )

    # 3. Gateway Service
    gw_settings = GatewaySettings(orders_url="http://orders")
    gw_client = GatewayClient(
        settings=gw_settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
        orders_client=gw_orders_client,
    )
    gw_app = create_gateway_app(settings=gw_settings, client=gw_client)
    await gw_client.open()

    yield gw_app, orders_app, inv_app

    await gw_client.close()
    await orders_store.close()
    await inv_store.close()


@pytest.mark.asyncio
async def test_checkout_produces_single_trace_spanning_all_3_services(
    full_lab_stack_fixture: tuple[FastAPI, FastAPI, FastAPI],
    span_exporter: InMemorySpanExporter,
) -> None:
    """Verifies that POST /checkout propagates W3C traceparent across all 3 services."""
    gw_app, _, _ = full_lab_stack_fixture

    transport = ASGITransport(app=gw_app)
    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        resp = await client.post("/checkout", json={"product_id": 1, "qty": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"
        assert data["checkout_id"].startswith("chk_")
        assert data["order"]["product_id"] == 1

    spans = span_exporter.get_finished_spans()
    span_map = {s.name: s for s in spans}

    # Verify presence of spans from all 3 services
    assert "gateway.checkout" in span_map
    assert "orders.handler" in span_map
    assert "inventory.call" in span_map
    assert "inventory.handler" in span_map
    assert "db.insert" in span_map

    # Crucial: Assert identical trace_id across all 3 services
    root_trace_id = span_map["gateway.checkout"].context.trace_id
    assert span_map["orders.handler"].context.trace_id == root_trace_id
    assert span_map["inventory.call"].context.trace_id == root_trace_id
    assert span_map["inventory.handler"].context.trace_id == root_trace_id
    assert span_map["db.insert"].context.trace_id == root_trace_id


@pytest.mark.asyncio
async def test_gateway_maps_upstream_timeout_to_504() -> None:
    """Verifies that an upstream orders timeout maps to HTTP 504."""
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timeout waiting for orders response")

    mock_orders = AsyncClient(transport=httpx.MockTransport(timeout_handler), base_url="http://orders")
    settings = GatewaySettings(orders_url="http://orders", checkout_timeout_s=0.1)
    client = GatewayClient(
        settings=settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
        orders_client=mock_orders,
    )
    app = create_gateway_app(settings=settings, client=client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gateway") as c:
        resp = await c.post("/checkout", json={"product_id": 1, "qty": 1})
        assert resp.status_code == 504
        assert "timed out" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_gateway_maps_upstream_503_to_502() -> None:
    """Verifies that upstream server errors (such as 503 pool timeout) map to HTTP 502."""
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "pool timeout"})

    mock_orders = AsyncClient(transport=httpx.MockTransport(error_handler), base_url="http://orders")
    settings = GatewaySettings(orders_url="http://orders")
    client = GatewayClient(
        settings=settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
        orders_client=mock_orders,
    )
    app = create_gateway_app(settings=settings, client=client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gateway") as c:
        resp = await c.post("/checkout", json={"product_id": 1, "qty": 1})
        assert resp.status_code == 502
        assert "503" in resp.json()["detail"] or "orders" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_gateway_passes_through_409_conflict() -> None:
    """Verifies that upstream 409 (insufficient stock) is passed through directly."""
    def conflict_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Insufficient inventory stock"})

    mock_orders = AsyncClient(transport=httpx.MockTransport(conflict_handler), base_url="http://orders")
    settings = GatewaySettings(orders_url="http://orders")
    client = GatewayClient(
        settings=settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
        orders_client=mock_orders,
    )
    app = create_gateway_app(settings=settings, client=client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gateway") as c:
        resp = await c.post("/checkout", json={"product_id": 1, "qty": 100})
        assert resp.status_code == 409
        assert "insufficient" in resp.json()["detail"].lower()

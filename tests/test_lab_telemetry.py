from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger, setup_lab_logging
from lab.common.metrics import LabMetrics
from lab.services.gateway.client import GatewayClient, create_default_version_registry
from lab.services.gateway.main import create_gateway_app
from lab.services.gateway.settings import GatewaySettings
from lab.services.inventory.main import create_inventory_app
from lab.services.inventory.settings import InventorySettings
from lab.services.orders.main import create_orders_app
from lab.services.orders.settings import OrdersSettings


def calculate_histogram_p95(buckets: list[tuple[float, int]]) -> float:
    """Emulates Prometheus histogram_quantile(0.95, ...) linear interpolation.

    buckets: sorted list of (le, cumulative_count) where le is upper bound.
    """
    if not buckets:
        return 0.0

    total_count = buckets[-1][1]
    if total_count == 0:
        return 0.0

    target_rank = 0.95 * total_count

    prev_bound = 0.0
    prev_count = 0

    for le, count in buckets:
        if count >= target_rank:
            if math.isinf(le):
                return prev_bound
            count_in_bucket = count - prev_count
            if count_in_bucket == 0:
                return prev_bound
            fraction = (target_rank - prev_count) / count_in_bucket
            return prev_bound + fraction * (le - prev_bound)
        prev_bound = le
        prev_count = count

    return buckets[-1][0]


def export_spans_to_otel_jsonl(spans: list[Any], output_path: Path) -> None:
    """Writes spans to a JSONL file mimicking OpenTelemetry Collector file exporter schema."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as f:
        for span in spans:
            span_record = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {
                                        "stringValue": getattr(
                                            span.resource,
                                            "attributes",
                                            {},
                                        ).get(SERVICE_NAME, "unknown")
                                    },
                                },
                                {
                                    "key": "service.version",
                                    "value": {
                                        "stringValue": getattr(
                                            span.resource,
                                            "attributes",
                                            {},
                                        ).get(SERVICE_VERSION, "v1.0.0")
                                    },
                                },
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {"name": getattr(span.instrumentation_scope, "name", "")},
                                "spans": [
                                    {
                                        "traceId": format(span.context.trace_id, "032x"),
                                        "spanId": format(span.context.span_id, "016x"),
                                        "parentSpanId": (
                                            format(span.parent.span_id, "016x")
                                            if span.parent and span.parent.span_id
                                            else None
                                        ),
                                        "name": span.name,
                                        "kind": (
                                            span.kind.value
                                            if hasattr(span.kind, "value")
                                            else 1
                                        ),
                                        "startTimeUnixNano": str(span.start_time),
                                        "endTimeUnixNano": str(span.end_time),
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
            f.write(json.dumps(span_record) + "\n")


@pytest.fixture
def memory_span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: "test-gateway", SERVICE_VERSION: "v1.0.0"})
        )
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def mock_telemetry_stack(
    tmp_path: Path,
) -> tuple[FastAPI, FastAPI, FastAPI, LabMetrics, LabMetrics, LabMetrics, Path]:
    """Wires mock 3-service stack configured to log to a temporary telemetry volume."""
    log_dir = tmp_path / "telemetry" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    gw_metrics = LabMetrics()
    orders_metrics = LabMetrics()
    inv_metrics = LabMetrics()

    # Configure logging for each service
    setup_lab_logging("inventory", version="v1.0.0", log_dir=str(log_dir))
    setup_lab_logging("orders", version="v1.0.0", log_dir=str(log_dir))
    setup_lab_logging("gateway", version="v1.0.0", log_dir=str(log_dir))

    # 1. Inventory mock
    inv_app = FastAPI(title="inventory")

    @inv_app.get("/stock/{product_id}")
    async def get_stock(product_id: int) -> dict[str, Any]:
        inv_logger = get_lab_logger("inventory.stock")
        inv_metrics.http_requests_total.labels(
            service="inventory", route="/stock/{product_id}", status="200"
        ).inc()
        inv_logger.info("stock_checked", product_id=product_id, in_stock=True)
        return {"product_id": product_id, "sku": f"SKU-{product_id}", "qty": 100}

    @inv_app.get("/metrics")
    async def inv_get_metrics() -> str:
        return "inventory_metric_sample"

    inv_transport = ASGITransport(app=inv_app)
    inv_client = AsyncClient(transport=inv_transport, base_url="http://inventory")

    # 2. Orders mock
    orders_app = FastAPI(title="orders")

    @orders_app.post("/orders", status_code=201)
    async def create_order(payload: dict[str, Any]) -> dict[str, Any]:
        orders_logger = get_lab_logger("orders.service")
        tracer = trace.get_tracer("orders")
        with tracer.start_as_current_span("orders.handler"):
            pid = payload["product_id"]
            qty = payload["qty"]

            with tracer.start_as_current_span("inventory.call"):
                resp = await inv_client.get(f"/stock/{pid}")
                assert resp.status_code == 200

            with tracer.start_as_current_span("db.insert"):
                pass

            orders_metrics.http_requests_total.labels(
                service="orders", route="/orders", status="201"
            ).inc()
            orders_logger.info("order_confirmed", product_id=pid, qty=qty)
            return {"id": 101, "product_id": pid, "qty": qty, "status": "CONFIRMED"}

    orders_transport = ASGITransport(app=orders_app)
    orders_client = AsyncClient(transport=orders_transport, base_url="http://orders")

    # 3. Gateway mock
    gw_settings = GatewaySettings(service_name="gateway", telemetry_log_dir=str(log_dir))
    gw_client = GatewayClient(
        settings=gw_settings,
        fault_state=FaultState(),
        metrics=gw_metrics,
        version_registry=create_default_version_registry(),
        orders_client=orders_client,
    )
    gw_app = create_gateway_app(settings=gw_settings, client=gw_client, metrics=gw_metrics)

    return gw_app, orders_app, inv_app, gw_metrics, orders_metrics, inv_metrics, tmp_path


@pytest.mark.asyncio
async def test_prometheus_metrics_exposed_for_all_services() -> None:
    """Requirement (a): Verifies Prometheus metrics endpoint is exposed and functioning."""
    inv_metrics = LabMetrics()
    inv_settings = InventorySettings()
    inv_app = create_inventory_app(settings=inv_settings, metrics=inv_metrics)

    orders_metrics = LabMetrics()
    orders_settings = OrdersSettings()
    orders_app = create_orders_app(settings=orders_settings, metrics=orders_metrics)

    gw_metrics = LabMetrics()
    gw_settings = GatewaySettings()
    gw_app = create_gateway_app(settings=gw_settings, metrics=gw_metrics)

    # Scrape /metrics from all 3 services
    async with AsyncClient(
        transport=ASGITransport(app=inv_app), base_url="http://inventory"
    ) as c:
        resp = await c.get("/metrics")
        assert resp.status_code == 200
        assert "http_requests_total" in resp.text
        assert "db_pool_in_use" in resp.text

    async with AsyncClient(transport=ASGITransport(app=orders_app), base_url="http://orders") as c:
        resp = await c.get("/metrics")
        assert resp.status_code == 200
        assert "http_requests_total" in resp.text
        assert "db_pool_in_use" in resp.text

    async with AsyncClient(
        transport=ASGITransport(app=gw_app), base_url="http://gateway"
    ) as c:
        resp = await c.get("/metrics")
        assert resp.status_code == 200
        assert "http_requests_total" in resp.text


@pytest.mark.asyncio
async def test_trace_id_matches_between_logs_and_spans(
    mock_telemetry_stack: tuple[
        FastAPI, FastAPI, FastAPI, LabMetrics, LabMetrics, LabMetrics, Path
    ],
    memory_span_exporter: InMemorySpanExporter,
) -> None:
    """Requirements (b) & (c):

    - (b) A trace_id from a log line matches a span in the traces file.
    - (c) Parent/child span links connect gateway -> orders -> inventory.
    """
    gw_app, _, _, _, _, _, telemetry_root = mock_telemetry_stack
    traces_file = telemetry_root / "telemetry" / "traces" / "spans.jsonl"
    logs_dir = telemetry_root / "telemetry" / "logs"

    # Send a checkout request through the gateway
    gw_transport = ASGITransport(app=gw_app)
    async with AsyncClient(transport=gw_transport, base_url="http://gateway") as client:
        resp = await client.post("/checkout", json={"product_id": 1, "qty": 1})
        assert resp.status_code == 200

    # Retrieve finished OpenTelemetry spans
    spans = memory_span_exporter.get_finished_spans()
    assert len(spans) > 0

    # Export spans into collector file format
    export_spans_to_otel_jsonl(spans, traces_file)

    # Read and parse spans.jsonl
    exported_spans: dict[str, dict[str, Any]] = {}
    with open(traces_file, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            doc = json.loads(line)
            for rspan in doc.get("resourceSpans", []):
                for sspan in rspan.get("scopeSpans", []):
                    for span_obj in sspan.get("spans", []):
                        exported_spans[span_obj["name"]] = span_obj

    assert "gateway.checkout" in exported_spans
    assert "orders.handler" in exported_spans
    assert "inventory.call" in exported_spans

    root_span = exported_spans["gateway.checkout"]
    orders_span = exported_spans["orders.handler"]
    inv_call_span = exported_spans["inventory.call"]

    root_trace_id = root_span["traceId"]

    # Assertion (c): Parent/child span links connect gateway -> orders -> inventory
    assert orders_span["traceId"] == root_trace_id
    assert inv_call_span["traceId"] == root_trace_id
    assert orders_span["parentSpanId"] == root_span["spanId"]
    assert inv_call_span["parentSpanId"] == orders_span["spanId"]

    # Assertion (b): A trace_id from a service log line matches a span in traces file
    found_log_trace_match = False
    for log_path in sorted(logs_dir.glob("*.jsonl")):
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    entry = json.loads(line_str)
                except json.JSONDecodeError:
                    continue
                if entry.get("event") == "order_confirmed":
                    log_trace_id = entry.get("trace_id")
                    assert log_trace_id == root_trace_id
                    assert log_trace_id in [s["traceId"] for s in exported_spans.values()]
                    found_log_trace_match = True
                    break
        if found_log_trace_match:
            break

    assert found_log_trace_match, "Expected correlated trace_id between log and traces file"


def test_promql_p95_calculation() -> None:
    """Verifies PromQL histogram_quantile(0.95, ...) calculation."""
    # Synthetic latency buckets (le, cumulative_count)
    buckets = [
        (0.005, 10),
        (0.010, 45),
        (0.025, 80),
        (0.050, 92),
        (0.100, 96),
        (0.250, 99),
        (0.500, 100),
        (float("inf"), 100),
    ]

    p95 = calculate_histogram_p95(buckets)
    # Target count is 95, which falls between 0.050 (count 92) and 0.100 (count 96)
    # (95 - 92) / (96 - 92) = 3/4 = 0.75 -> 0.050 + 0.75 * (0.100 - 0.050) = 0.0875s (87.5ms)
    assert 0.050 <= p95 <= 0.100
    assert abs(p95 - 0.0875) < 1e-4

"""Demonstrates PromQL p95 calculation and trace_id correlation between logs and spans."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

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


def calculate_promql_p95(buckets: list[tuple[float, int]]) -> float:
    """Emulates Prometheus histogram_quantile(0.95, ...) linear interpolation."""
    total_count = buckets[-1][1]
    if total_count == 0:
        return 0.0

    target = 0.95 * total_count
    prev_le = 0.0
    prev_cnt = 0

    for le, cnt in buckets:
        if cnt >= target:
            if math.isinf(le):
                return prev_le
            bucket_cnt = cnt - prev_cnt
            if bucket_cnt == 0:
                return prev_le
            return prev_le + ((target - prev_cnt) / bucket_cnt) * (le - prev_le)
        prev_le = le
        prev_cnt = cnt
    return buckets[-1][0]


async def main() -> None:
    telemetry_dir = Path("/tmp/telemetry")
    logs_dir = telemetry_dir / "logs"
    traces_dir = telemetry_dir / "traces"
    logs_dir.mkdir(parents=True, exist_ok=True)
    traces_dir.mkdir(parents=True, exist_ok=True)

    spans_file = traces_dir / "spans.jsonl"
    if spans_file.exists():
        spans_file.unlink()
    for f in logs_dir.glob("*.jsonl"):
        f.unlink()

    # 1. Setup OpenTelemetry & logging
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: "demo-gateway", SERVICE_VERSION: "v1.0.0"})
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    setup_lab_logging("gateway", version="v1.0.0", log_dir=str(logs_dir))
    setup_lab_logging("orders", version="v1.0.0", log_dir=str(logs_dir))
    setup_lab_logging("inventory", version="v1.0.0", log_dir=str(logs_dir))

    gw_metrics = LabMetrics()
    orders_metrics = LabMetrics()

    # 2. Wire mock microservices
    # Inventory
    inv_app = FastAPI()

    @inv_app.get("/stock/{product_id}")
    async def get_stock(product_id: int) -> dict[str, Any]:
        return {"product_id": product_id, "sku": f"SKU-{product_id}", "qty": 500}

    inv_client = AsyncClient(transport=ASGITransport(app=inv_app), base_url="http://inventory")

    # Orders
    orders_app = FastAPI()

    @orders_app.post("/orders", status_code=201)
    async def create_order(payload: dict[str, Any]) -> dict[str, Any]:
        tracer = trace.get_tracer("orders")
        logger = get_lab_logger("orders.service")
        with tracer.start_as_current_span("orders.handler"):
            with tracer.start_as_current_span("inventory.call"):
                await inv_client.get(f"/stock/{payload['product_id']}")
            with tracer.start_as_current_span("db.insert"):
                pass
            orders_metrics.http_requests_total.labels(
                service="orders", route="/orders", status="201"
            ).inc()
            orders_metrics.http_request_duration_seconds.labels(
                service="orders", route="/orders", status="201"
            ).observe(0.042)
            logger.info("order_created", product_id=payload["product_id"], qty=payload["qty"])
            return {"id": 1001, "status": "CONFIRMED"}

    orders_client = AsyncClient(transport=ASGITransport(app=orders_app), base_url="http://orders")

    # Gateway
    gw_settings = GatewaySettings(service_name="gateway", telemetry_log_dir=str(logs_dir))
    gw_client = GatewayClient(
        settings=gw_settings,
        fault_state=FaultState(),
        metrics=gw_metrics,
        version_registry=create_default_version_registry(),
        orders_client=orders_client,
    )
    gw_app = create_gateway_app(settings=gw_settings, client=gw_client, metrics=gw_metrics)

    active_provider = trace.get_tracer_provider()
    active_provider.add_span_processor(SimpleSpanProcessor(exporter))

    # 3. Simulate requests
    async with AsyncClient(
        transport=ASGITransport(app=gw_app), base_url="http://gateway"
    ) as client:
        for i in range(1, 21):
            dur = 0.015 + (i * 0.003)
            gw_metrics.http_request_duration_seconds.labels(
                service="gateway", route="/checkout", status="200"
            ).observe(dur)
            resp = await client.post("/checkout", json={"product_id": i, "qty": 2})
            assert resp.status_code == 200

    # 4. Export spans to spans.jsonl
    finished_spans = exporter.get_finished_spans()
    with open(spans_file, "w", encoding="utf-8") as f:
        for span in finished_spans:
            record = {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
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
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
            f.write(json.dumps(record) + "\n")

    # 5. Extract correlated trace_id from log and span
    correlated_log_entry = None
    log_source_file = ""
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
                if entry.get("trace_id"):
                    correlated_log_entry = entry
                    log_source_file = log_path.name
                    break
        if correlated_log_entry:
            break

    assert correlated_log_entry is not None, "No log entry with trace_id found"
    target_trace_id = correlated_log_entry["trace_id"]

    matching_span = None
    with open(spans_file, encoding="utf-8") as f:
        for line in f:
            doc = json.loads(line)
            span = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            if span["traceId"] == target_trace_id:
                matching_span = span
                break

    print("\n" + "=" * 60)
    print("      CORRELATED TRACE_ID DEMONSTRATION (LOG <-> SPAN)      ")
    print("=" * 60)
    print(f"Correlated W3C trace_id: {target_trace_id}")
    print(f"\nLog Line from /telemetry/logs/{log_source_file}:")
    print(json.dumps(correlated_log_entry, indent=2))
    print("\nSpan Record from /telemetry/traces/spans.jsonl:")
    print(json.dumps(matching_span, indent=2))

    # 6. PromQL p95 calculation
    buckets: list[tuple[float, int]] = []
    # Extract histogram buckets from gateway metric
    metric_family = gw_metrics.registry.collect()
    for metric in metric_family:
        if metric.name == "http_request_duration_seconds":
            for sample in metric.samples:
                if sample.name.endswith("_bucket") and sample.labels.get("service") == "gateway":
                    le_val = float(sample.labels["le"])
                    buckets.append((le_val, int(sample.value)))

    buckets.sort(key=lambda b: b[0])
    p95_val = calculate_promql_p95(buckets)

    print("\n" + "=" * 60)
    print("            PROMQL P95 LATENCY CALCULATION                  ")
    print("=" * 60)
    promql_expr = (
        "histogram_quantile(0.95, "
        "sum(rate(http_request_duration_seconds_bucket[1m])) by (le, service))"
    )
    print(f"PromQL Expression:  {promql_expr}")
    print("Service:            gateway")
    print(f"Buckets Evaluated:  {len(buckets)} histogram intervals")
    print(f"Computed p95:       {p95_val:.4f}s ({p95_val * 1000:.1f}ms)")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())

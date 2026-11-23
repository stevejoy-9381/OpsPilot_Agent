from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx
from fastapi import HTTPException, status
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger
from lab.common.metrics import LabMetrics
from lab.common.profiles import VersionProfile, VersionProfileRegistry
from lab.services.gateway.settings import GatewaySettings

logger = get_lab_logger("gateway.client")
tracer = trace.get_tracer("gateway")
propagator = TraceContextTextMapPropagator()


def create_default_version_registry() -> VersionProfileRegistry:
    """Creates the neutral version profile registry for the gateway service."""
    registry = VersionProfileRegistry(default_version="v1.0.0")

    registry.register(
        VersionProfile(
            version="v1.0.0",
            features={"log_style": "standard"},
            commit_sha="e1f2a3b",
            changelog="Initial gateway release with checkout routing",
        )
    )

    registry.register(
        VersionProfile(
            version="v1.1.0",
            features={"log_style": "extended"},
            commit_sha="f2a3b4c",
            changelog="Update checkout response formatting and error propagation",
        )
    )

    return registry


class GatewayClient:
    """Manages routing checkout requests to upstream orders service under tracing."""

    def __init__(
        self,
        settings: GatewaySettings,
        fault_state: FaultState,
        metrics: LabMetrics,
        version_registry: VersionProfileRegistry,
        orders_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.fault_state = fault_state
        self.metrics = metrics
        self.version_registry = version_registry
        self.orders_client = orders_client or httpx.AsyncClient(
            base_url=settings.orders_url,
            timeout=settings.checkout_timeout_s,
        )

    async def open(self) -> None:
        """Opens client resources."""
        pass

    async def close(self) -> None:
        """Closes client connections."""
        await self.orders_client.aclose()

    async def checkout(self, product_id: int, qty: int) -> dict[str, Any]:
        """Routes checkout to orders service with timeout 2.0s, no retries, mapping errors."""
        with tracer.start_as_current_span("gateway.checkout") as span:
            span.set_attribute("product.id", product_id)
            span.set_attribute("order.qty", qty)
            span.set_attribute("service.version", self.version_registry.active_version)

            # Apply injected latency if configured on orders dependency
            latency_ms = await self.fault_state.get_dependency_latency("orders")
            if latency_ms > 0:
                await asyncio.sleep(latency_ms / 1000.0)

            # Apply injected error rate if configured on orders dependency
            error_rate = await self.fault_state.get_dependency_error_rate("orders")
            if error_rate > 0 and random.random() < error_rate:
                self.metrics.dependency_errors_total.labels(dependency="orders").inc()
                logger.error(
                    event="upstream_error",
                    msg="Upstream orders connection dropped",
                    product_id=product_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Upstream orders service connection failed",
                )

            headers: dict[str, str] = {}
            propagator.inject(headers)

            start = time.perf_counter()
            timeout_ms = int(self.settings.checkout_timeout_s * 1000)
            try:
                resp = await self.orders_client.post(
                    "/orders",
                    json={"product_id": product_id, "qty": qty},
                    headers=headers,
                    timeout=self.settings.checkout_timeout_s,
                )
                duration = time.perf_counter() - start
                self.metrics.dependency_call_duration_seconds.labels(
                    dependency="orders"
                ).observe(duration)

                # Upstream success: 201 Created
                if resp.status_code == status.HTTP_201_CREATED:
                    order_data = resp.json()
                    return {
                        "checkout_id": f"chk_{order_data['id']}",
                        "status": "SUCCESS",
                        "order": order_data,
                    }

                # Upstream conflict: 409 Insufficient Stock (pass through)
                if resp.status_code == status.HTTP_409_CONFLICT:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=resp.json().get("detail", "Insufficient inventory stock"),
                    )

                # Upstream not found: 404 (pass through)
                if resp.status_code == status.HTTP_404_NOT_FOUND:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=resp.json().get("detail", "Product not found"),
                    )

                # Upstream 503 or server error: map to 502 Bad Gateway
                self.metrics.dependency_errors_total.labels(dependency="orders").inc()
                err_detail = resp.json().get("detail", f"status {resp.status_code}")
                logger.error(
                    event="upstream_error",
                    msg=f"Upstream orders returned error {resp.status_code}: {err_detail}",
                    status_code=resp.status_code,
                    product_id=product_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Upstream orders service error: {err_detail}",
                )

            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                duration = time.perf_counter() - start
                self.metrics.dependency_call_duration_seconds.labels(
                    dependency="orders"
                ).observe(duration)
                self.metrics.dependency_errors_total.labels(dependency="orders").inc()
                logger.error(
                    event="upstream_timeout",
                    msg=f"Upstream orders timed out after {timeout_ms}ms",
                    product_id=product_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail="Upstream orders service timed out",
                ) from exc

            except httpx.RequestError as exc:
                duration = time.perf_counter() - start
                self.metrics.dependency_call_duration_seconds.labels(
                    dependency="orders"
                ).observe(duration)
                self.metrics.dependency_errors_total.labels(dependency="orders").inc()
                logger.error(
                    event="upstream_error",
                    msg=f"Upstream orders service unreachable: {exc}",
                    product_id=product_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Upstream orders service unreachable: {exc}",
                ) from exc

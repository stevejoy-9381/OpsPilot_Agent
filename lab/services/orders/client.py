from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx
from fastapi import HTTPException, status
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger
from lab.common.metrics import LabMetrics
from lab.common.profiles import VersionProfile, VersionProfileRegistry
from lab.services.orders.settings import OrdersSettings

logger = get_lab_logger("orders.client")
tracer = trace.get_tracer("orders")
propagator = TraceContextTextMapPropagator()


def create_default_version_registry() -> VersionProfileRegistry:
    """Creates the neutral version profile registry for the orders service."""
    registry = VersionProfileRegistry(default_version="v1.0.0")

    # Baseline: connection always released in finally block
    registry.register(
        VersionProfile(
            version="v1.0.0",
            features={"release_on_conflict": True, "log_style": "standard"},
            commit_sha="a2b4c6e",
            changelog="Initial orders service release with stock check and connection pooling",
        )
    )

    # Benign refactor: identical performance and clean connection hygiene
    registry.register(
        VersionProfile(
            version="v1.1.0",
            features={"release_on_conflict": True, "log_style": "extended"},
            commit_sha="b3c5d7f",
            changelog="Refactor receipt formatting and diagnostic logging",
        )
    )

    # Behaviour change: connection retained on stock conflict branch (F1 regression)
    registry.register(
        VersionProfile(
            version="v1.3.0",
            features={"release_on_conflict": False, "log_style": "extended"},
            commit_sha="d5e6f7a",
            changelog="Update stock reservation and order processing pipeline",
        )
    )

    return registry


class OrdersStore:
    """Manages database connection pool, downstream inventory client, and order lifecycle."""

    def __init__(
        self,
        settings: OrdersSettings,
        fault_state: FaultState,
        metrics: LabMetrics,
        version_registry: VersionProfileRegistry,
        pool: AsyncConnectionPool | None = None,
        inventory_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.fault_state = fault_state
        self.metrics = metrics
        self.version_registry = version_registry

        # Normalise postgresql+psycopg to standard postgresql connection string
        db_conninfo = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
        self.pool = pool or AsyncConnectionPool(
            conninfo=db_conninfo,
            min_size=2,
            max_size=settings.db_pool_size,
            timeout=settings.db_pool_timeout_s,
            open=False,
        )
        self.inventory_client = inventory_client or httpx.AsyncClient(
            base_url=settings.inventory_url,
            timeout=settings.inventory_timeout_s,
        )
        self._pool_opened = False
        self._leaked_connections: list[Any] = []

    @property
    def pool_in_use(self) -> int:
        """Returns the number of active retained/leaked connections."""
        return len(self._leaked_connections)

    async def open(self) -> None:
        """Opens background connection pool."""
        if not self._pool_opened:
            await self.pool.open()
            self._pool_opened = True
            self.metrics.db_pool_size.labels(service=self.settings.service_name).set(
                self.settings.db_pool_size
            )

    async def close(self) -> None:
        """Closes connection pools, drains any retained connections, and closes HTTP client."""
        await self.drain_leaks()
        if self._pool_opened:
            await self.pool.close()
            self._pool_opened = False
        await self.inventory_client.aclose()

    async def drain_leaks(self) -> None:
        """Recovers any retained connections back into the pool."""
        for conn in self._leaked_connections:
            try:
                await self.pool.putconn(conn)
                self.metrics.db_pool_in_use.labels(service=self.settings.service_name).dec()
            except Exception as e:
                logger.warning(
                    event="pool_drain_error",
                    msg=f"Error draining retained connection: {e}",
                )
        self._leaked_connections.clear()

    async def _call_inventory(self, product_id: int) -> tuple[int, int]:
        """Calls inventory service with timeout and 1 retry under span inventory.call."""
        with tracer.start_as_current_span("inventory.call") as span:
            span.set_attribute("product.id", product_id)
            span.set_attribute("service.version", self.version_registry.active_version)

            latency_ms = await self.fault_state.get_dependency_latency("inventory")
            if latency_ms > 0:
                await asyncio.sleep(latency_ms / 1000.0)

            error_rate = await self.fault_state.get_dependency_error_rate("inventory")

            headers: dict[str, str] = {}
            propagator.inject(headers)

            max_attempts = 1 + self.settings.inventory_retries
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                start = time.perf_counter()
                try:
                    if error_rate > 0 and random.random() < error_rate:
                        raise httpx.RequestError("inventory connection error")

                    resp = await self.inventory_client.get(
                        f"/stock/{product_id}",
                        headers=headers,
                        timeout=self.settings.inventory_timeout_s,
                    )
                    duration = time.perf_counter() - start
                    self.metrics.dependency_call_duration_seconds.labels(
                        dependency="inventory"
                    ).observe(duration)

                    if resp.status_code == status.HTTP_404_NOT_FOUND:
                        return 404, 0
                    if resp.status_code == status.HTTP_200_OK:
                        data = resp.json()
                        return 200, int(data.get("qty", 0))

                    if attempt == max_attempts - 1:
                        self.metrics.dependency_errors_total.labels(
                            dependency="inventory"
                        ).inc()
                        return resp.status_code, 0

                except (httpx.TimeoutException, httpx.RequestError, Exception) as exc:
                    duration = time.perf_counter() - start
                    self.metrics.dependency_call_duration_seconds.labels(
                        dependency="inventory"
                    ).observe(duration)
                    last_exc = exc
                    if attempt == max_attempts - 1:
                        self.metrics.dependency_errors_total.labels(
                            dependency="inventory"
                        ).inc()
                        logger.error(
                            event="inventory_dependency_error",
                            msg=f"Failed to check downstream inventory: {last_exc}",
                            product_id=product_id,
                        )
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Downstream inventory service unreachable: {last_exc}",
                        ) from exc
                    await asyncio.sleep(0.01)

            return 502, 0

    async def create_order(
        self,
        product_id: int,
        qty: int,
        status_name: str = "CONFIRMED",
    ) -> dict[str, Any]:
        """Processes an order under pool bounds, inventory validation, and profile rules."""
        active_profile = self.version_registry.get_active_profile()
        release_on_conflict = active_profile.features.get("release_on_conflict", True)
        if await self.fault_state.should_leak_on_conflict():
            release_on_conflict = False

        # Step 1: Acquire DB connection from bounded pool
        wait_start = time.perf_counter()
        conn = None
        try:
            conn = await asyncio.wait_for(
                self.pool.getconn(),
                timeout=self.settings.db_pool_timeout_s,
            )
            wait_duration = time.perf_counter() - wait_start
            self.metrics.db_pool_wait_seconds.labels(
                service=self.settings.service_name
            ).observe(wait_duration)
            self.metrics.db_pool_in_use.labels(service=self.settings.service_name).inc()
        except (TimeoutError, asyncio.TimeoutError, PoolTimeout):
            wait_duration = time.perf_counter() - wait_start
            self.metrics.db_pool_wait_seconds.labels(
                service=self.settings.service_name
            ).observe(wait_duration)
            self.metrics.dependency_errors_total.labels(dependency="db").inc()
            logger.error(
                event="db_pool_timeout",
                msg="pool timeout",
                service=self.settings.service_name,
                pool_size=self.settings.db_pool_size,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="pool timeout",
            ) from None

        released = False
        try:
            # Step 2: Call inventory GET /stock/{id} (timeout 500ms, 1 retry)
            inv_status, available_qty = await self._call_inventory(product_id)

            if inv_status == status.HTTP_404_NOT_FOUND:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product {product_id} not found in inventory catalog",
                )
            if inv_status != status.HTTP_200_OK:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Downstream inventory check failed with status {inv_status}",
                )

            # Step 3: Check stock availability
            if available_qty < qty:
                if not release_on_conflict:
                    # In v1.3.0, connection is not released on conflict path
                    self._leaked_connections.append(conn)
                    released = True
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Insufficient inventory stock",
                )

            # Step 4: INSERT into orders using acquired connection under span db.insert
            with tracer.start_as_current_span("db.insert") as span:
                span.set_attribute("db.system", "postgresql")
                span.set_attribute("db.operation", "INSERT")
                span.set_attribute("service.version", self.version_registry.active_version)

                db_latency_ms = await self.fault_state.get_dependency_latency("db")
                if db_latency_ms > 0:
                    await asyncio.sleep(db_latency_ms / 1000.0)

                db_error_rate = await self.fault_state.get_dependency_error_rate("db")
                if db_error_rate > 0 and random.random() < db_error_rate:
                    self.metrics.dependency_errors_total.labels(dependency="db").inc()
                    raise RuntimeError("Database connection reset during order insertion")

                query_start = time.perf_counter()
                query = """
                    INSERT INTO orders (product_id, qty, status, created_at)
                    VALUES (%s, %s, %s, NOW())
                    RETURNING id, product_id, qty, status, created_at;
                """
                async with conn.cursor() as cur:
                    await cur.execute(query, (product_id, qty, status_name))
                    row = await cur.fetchone()

                await conn.commit()

                query_duration = time.perf_counter() - query_start
                self.metrics.dependency_call_duration_seconds.labels(dependency="db").observe(
                    query_duration
                )

                if row is None:
                    raise RuntimeError("Failed to retrieve generated order record")

                created_at_str = (
                    row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4])
                )
                order_record = {
                    "id": row[0],
                    "product_id": row[1],
                    "qty": row[2],
                    "status": row[3],
                    "created_at": created_at_str,
                }

                logger.info(
                    event="order_created",
                    msg="Order successfully placed and recorded",
                    order_id=order_record["id"],
                    product_id=product_id,
                    qty=qty,
                    status=order_record["status"],
                )

                return order_record

        finally:
            if not released and conn is not None:
                try:
                    await self.pool.putconn(conn)
                finally:
                    self.metrics.db_pool_in_use.labels(service=self.settings.service_name).dec()

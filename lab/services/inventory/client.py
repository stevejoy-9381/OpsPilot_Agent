from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import redis.asyncio as aioredis
from opentelemetry import trace
from psycopg_pool import AsyncConnectionPool

from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger
from lab.common.metrics import LabMetrics
from lab.common.profiles import VersionProfile, VersionProfileRegistry
from lab.services.inventory.settings import InventorySettings

logger = get_lab_logger("inventory.client")
tracer = trace.get_tracer("inventory")


def create_default_version_registry() -> VersionProfileRegistry:
    """Creates the neutral version profile registry for the inventory service."""
    registry = VersionProfileRegistry(default_version="v1.0.0")

    # Baseline: cache-aside enabled, single DB query on miss
    registry.register(
        VersionProfile(
            version="v1.0.0",
            features={"cache_enabled": True, "extra_query": False, "log_style": "standard"},
            commit_sha="7f8a9b1",
            changelog="Initial inventory service release",
        )
    )

    # Benign refactor: identical performance and caching
    registry.register(
        VersionProfile(
            version="v1.1.0",
            features={"cache_enabled": True, "extra_query": False, "log_style": "extended"},
            commit_sha="9b2c3d4",
            changelog="Refactor stock retrieval handler and logging structure",
        )
    )

    # Behaviour change: bypasses cache on hot path and executes secondary query (F2 regression)
    registry.register(
        VersionProfile(
            version="v1.2.0",
            features={"cache_enabled": False, "extra_query": True, "log_style": "extended"},
            commit_sha="e5f6a7b",
            changelog="Update stock verification and auditing pipeline",
        )
    )

    return registry


class InventoryStore:
    """Manages database connection pool, Redis cache, and fault handling for inventory."""

    def __init__(
        self,
        settings: InventorySettings,
        fault_state: FaultState,
        metrics: LabMetrics,
        version_registry: VersionProfileRegistry,
        pool: AsyncConnectionPool | None = None,
        redis_client: aioredis.Redis[str] | None = None,
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
            open=False,
        )
        self.redis = redis_client or aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        self._pool_opened = False

    async def open(self) -> None:
        """Opens background connection pools."""
        if not self._pool_opened:
            await self.pool.open()
            self._pool_opened = True
            self.metrics.db_pool_size.labels(service=self.settings.service_name).set(
                self.settings.db_pool_size
            )

    async def close(self) -> None:
        """Closes connection pools and Redis clients."""
        if self._pool_opened:
            await self.pool.close()
            self._pool_opened = False
        await self.redis.close()

    async def flush_cache(self) -> None:
        """Flushes all keys in the inventory Redis database."""
        try:
            await self.redis.flushdb()
            logger.info(event="cache_flushed", msg="Inventory Redis cache flushed")
        except Exception as e:
            logger.warning(event="cache_flush_failed", error=str(e))

    async def _redis_get(self, key: str) -> str | None:
        """Reads a key from Redis under timeout and active fault constraints."""
        with tracer.start_as_current_span("redis.get") as span:
            span.set_attribute("db.system", "redis")
            span.set_attribute("db.operation", "get")
            span.set_attribute("db.redis.key", key)

            start = time.perf_counter()
            try:
                async def _timed_get() -> str | None:
                    # 1. Apply injected latency
                    latency_ms = await self.fault_state.get_dependency_latency("redis")
                    if latency_ms > 0:
                        await asyncio.sleep(latency_ms / 1000.0)

                    # 2. Apply injected error rate
                    error_rate = await self.fault_state.get_dependency_error_rate("redis")
                    if error_rate > 0 and random.random() < error_rate:
                        raise aioredis.ConnectionError(
                            "Redis connection dropped"
                        )

                    val = await self.redis.get(key)
                    return str(val) if val is not None else None

                # 3. Execute with strict 150ms timeout
                val = await asyncio.wait_for(
                    _timed_get(),
                    timeout=self.settings.redis_timeout_s,
                )
                duration = time.perf_counter() - start
                self.metrics.dependency_call_duration_seconds.labels(dependency="redis").observe(
                    duration
                )
                return str(val) if val is not None else None

            except (TimeoutError, asyncio.TimeoutError):
                duration = time.perf_counter() - start
                self.metrics.dependency_call_duration_seconds.labels(dependency="redis").observe(
                    duration
                )
                self.metrics.dependency_errors_total.labels(dependency="redis").inc()
                # Log natural symptom: timeout occurred
                logger.warning(
                    event="redis_timeout",
                    msg="Redis command timed out after 150ms, falling back to database",
                    key=key,
                )
                return None

            except Exception as e:
                duration = time.perf_counter() - start
                self.metrics.dependency_call_duration_seconds.labels(dependency="redis").observe(
                    duration
                )
                self.metrics.dependency_errors_total.labels(dependency="redis").inc()
                # Log natural symptom: error occurred
                logger.warning(
                    event="redis_error",
                    msg=f"Redis cache lookup failed: {e}, falling back to database",
                    key=key,
                )
                return None

    async def _redis_set(self, key: str, value: int, ttl_s: int) -> None:
        """Sets a key in Redis with TTL under a span."""
        with tracer.start_as_current_span("redis.set") as span:
            span.set_attribute("db.system", "redis")
            span.set_attribute("db.operation", "set")
            span.set_attribute("db.redis.key", key)

            try:
                await asyncio.wait_for(
                    self.redis.setex(key, ttl_s, str(value)),
                    timeout=self.settings.redis_timeout_s,
                )
            except Exception as e:
                self.metrics.dependency_errors_total.labels(dependency="redis").inc()
                logger.warning(
                    event="redis_set_failed",
                    msg=f"Failed to populate Redis cache: {e}",
                    key=key,
                )

    async def _db_query(self, query: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
        """Executes a database query with connection pooling, latency, and error faults."""
        with tracer.start_as_current_span("db.query") as span:
            span.set_attribute("db.system", "postgresql")
            span.set_attribute("db.statement", query)

            wait_start = time.perf_counter()
            self.metrics.db_pool_in_use.labels(service=self.settings.service_name).inc()
            try:
                async with self.pool.connection() as conn:
                    wait_duration = time.perf_counter() - wait_start
                    self.metrics.db_pool_wait_seconds.labels(
                        service=self.settings.service_name
                    ).observe(wait_duration)

                    # Apply injected latency
                    latency_ms = await self.fault_state.get_dependency_latency("db")
                    if latency_ms > 0:
                        await asyncio.sleep(latency_ms / 1000.0)

                    # Apply injected error rate
                    error_rate = await self.fault_state.get_dependency_error_rate("db")
                    if error_rate > 0 and random.random() < error_rate:
                        self.metrics.dependency_errors_total.labels(dependency="db").inc()
                        raise RuntimeError(
                            "Database query execution failed due to connection error"
                        )

                    query_start = time.perf_counter()
                    async with conn.cursor() as cur:
                        await cur.execute(query, params)
                        row = await cur.fetchone()

                    query_duration = time.perf_counter() - query_start
                    self.metrics.dependency_call_duration_seconds.labels(dependency="db").observe(
                        query_duration
                    )
                    return row

            except Exception as e:
                self.metrics.dependency_errors_total.labels(dependency="db").inc()
                raise e

            finally:
                self.metrics.db_pool_in_use.labels(service=self.settings.service_name).dec()

    async def get_stock(self, product_id: int) -> int | None:
        """Retrieves stock quantity for product_id using active profile's cache strategy."""
        profile = self.version_registry.get_active_profile()
        cache_enabled = profile.features.get("cache_enabled", True)
        extra_query = profile.features.get("extra_query", False)

        cache_key = f"inventory:{product_id}"

        # 1. Attempt Cache if enabled in active profile
        if cache_enabled:
            cached = await self._redis_get(cache_key)
            if cached is not None:
                return int(cached)

        # 2. Cache miss or cache bypassed: query lab-postgres
        query = "SELECT qty FROM inventory WHERE product_id = %s;"
        row = await self._db_query(query, (product_id,))
        if row is None:
            return None

        qty = int(row[0])

        # 3. v1.2.0 Behaviour change: issue secondary query
        if extra_query:
            secondary_query = "SELECT name, price_cents FROM products WHERE id = %s;"
            await self._db_query(secondary_query, (product_id,))

        # 4. Fill cache if caching is active
        if cache_enabled:
            await self._redis_set(cache_key, qty, ttl_s=self.settings.cache_ttl_s)

        return qty

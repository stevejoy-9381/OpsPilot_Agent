from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, status
from opentelemetry import trace

from lab.common.admin import create_admin_router
from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger, setup_lab_logging
from lab.common.metrics import LabMetrics, PrometheusMiddleware, create_metrics_router
from lab.common.tracing import instrument_app, instrument_httpx, setup_tracing
from lab.services.inventory.client import InventoryStore, create_default_version_registry
from lab.services.inventory.settings import InventorySettings

logger = get_lab_logger("inventory.api")
tracer = trace.get_tracer("inventory")


def create_inventory_app(
    settings: InventorySettings | None = None,
    store: InventoryStore | None = None,
    metrics: LabMetrics | None = None,
) -> FastAPI:
    """Factory creating the configured FastAPI inventory service."""
    app_settings = settings or InventorySettings()
    app_metrics = metrics or LabMetrics()
    fault_state = store.fault_state if store else FaultState()
    version_registry = store.version_registry if store else create_default_version_registry()

    # Telemetry and Observability setup
    setup_lab_logging(
        service_name=app_settings.service_name,
        version=app_settings.version,
        log_dir=app_settings.telemetry_log_dir,
        log_level=app_settings.log_level,
    )
    setup_tracing(
        service_name=app_settings.service_name,
        version=app_settings.version,
        otlp_endpoint=app_settings.otel_exporter_otlp_endpoint,
    )
    instrument_httpx()

    store_instance = store or InventoryStore(
        settings=app_settings,
        fault_state=fault_state,
        metrics=app_metrics,
        version_registry=version_registry,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await store_instance.open()
        yield
        await store_instance.close()

    app = FastAPI(
        title="OpsPilot Inventory Service",
        version=app_settings.version,
        lifespan=lifespan,
    )

    # Middleware: Prometheus HTTP tracking excluding admin/metrics
    app.add_middleware(
        PrometheusMiddleware,
        service_name=app_settings.service_name,
        metrics=app_metrics,
    )

    # Instrument application endpoints with OpenTelemetry
    instrument_app(app)

    # Register admin router
    async def on_deploy(version: str) -> None:
        # Clear local cache or flush in-memory state on deploy if needed
        logger.info(
            event="inventory_profile_deployed",
            msg="Inventory profile switched",
            target_version=version,
        )

    async def on_reset() -> None:
        await store_instance.flush_cache()
        logger.info(
            event="inventory_state_reset",
            msg="Inventory state reset and cache flushed",
        )

    admin_router = create_admin_router(
        settings=app_settings,
        fault_state=fault_state,
        version_registry=version_registry,
        on_deploy=on_deploy,
        on_reset=on_reset,
    )
    metrics_router = create_metrics_router(app_metrics)

    app.include_router(admin_router)
    app.include_router(metrics_router)

    # Service business endpoints
    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "service": app_settings.service_name,
            "version": version_registry.active_version,
        }

    @app.get("/stock/{product_id}", tags=["inventory"])
    async def get_stock(product_id: int) -> dict[str, int]:
        with tracer.start_as_current_span("inventory.handler") as span:
            span.set_attribute("product.id", product_id)
            span.set_attribute("service.version", version_registry.active_version)

            qty = await store_instance.get_stock(product_id)
            if qty is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product {product_id} not found in inventory catalog",
                )

            return {"product_id": product_id, "qty": qty}

    # Store state references on app for testing inspection
    app.state.store = store_instance
    app.state.settings = app_settings
    app.state.metrics = app_metrics
    app.state.fault_state = fault_state
    app.state.version_registry = version_registry

    return app


# Default ASGI application instance
app = create_inventory_app()

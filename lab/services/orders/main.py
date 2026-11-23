from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, status
from opentelemetry import trace
from pydantic import BaseModel, Field

from lab.common.admin import create_admin_router
from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger, setup_lab_logging
from lab.common.metrics import LabMetrics, PrometheusMiddleware, create_metrics_router
from lab.common.tracing import instrument_app, instrument_httpx, setup_tracing
from lab.services.orders.client import OrdersStore, create_default_version_registry
from lab.services.orders.settings import OrdersSettings

logger = get_lab_logger("orders.api")
tracer = trace.get_tracer("orders")


class CreateOrderRequest(BaseModel):
    """Payload schema for order creation."""

    product_id: int = Field(..., description="ID of the catalog product to purchase", gt=0)
    qty: int = Field(default=1, description="Quantity requested", gt=0)


class OrderResponse(BaseModel):
    """Response schema for confirmed orders."""

    id: int
    product_id: int
    qty: int
    status: str
    created_at: str


def create_orders_app(
    settings: OrdersSettings | None = None,
    store: OrdersStore | None = None,
    metrics: LabMetrics | None = None,
) -> FastAPI:
    """Factory creating the configured FastAPI orders service."""
    app_settings = settings or OrdersSettings()
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

    store_instance = store or OrdersStore(
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
        title="OpsPilot Orders Service",
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

    # Register admin router with on_deploy and on_reset callbacks
    async def on_deploy(version: str) -> None:
        # Switching profiles (especially rollbacks) drains any leaked connections
        await store_instance.drain_leaks()
        logger.info(
            event="orders_profile_deployed",
            msg="Orders profile switched and active pool state reconciled",
            target_version=version,
        )

    async def on_reset() -> None:
        await store_instance.drain_leaks()
        logger.info(
            event="orders_state_reset",
            msg="Orders state reset and active pool state reconciled",
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

    @app.post(
        "/orders",
        response_model=OrderResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["orders"],
    )
    async def create_order(request: CreateOrderRequest) -> dict[str, str | int]:
        with tracer.start_as_current_span("orders.handler") as span:
            span.set_attribute("product.id", request.product_id)
            span.set_attribute("order.qty", request.qty)
            span.set_attribute("service.version", version_registry.active_version)

            return await store_instance.create_order(
                product_id=request.product_id,
                qty=request.qty,
            )

    # Store state references on app for testing inspection
    app.state.store = store_instance
    app.state.settings = app_settings
    app.state.metrics = app_metrics
    app.state.fault_state = fault_state
    app.state.version_registry = version_registry

    return app


# Default ASGI application instance
app = create_orders_app()

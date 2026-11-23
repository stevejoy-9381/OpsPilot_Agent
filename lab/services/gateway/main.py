from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, status
from pydantic import BaseModel, Field

from lab.common.admin import create_admin_router
from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger, setup_lab_logging
from lab.common.metrics import LabMetrics, PrometheusMiddleware, create_metrics_router
from lab.common.tracing import instrument_app, instrument_httpx, setup_tracing
from lab.services.gateway.client import GatewayClient, create_default_version_registry
from lab.services.gateway.settings import GatewaySettings

logger = get_lab_logger("gateway.api")


class CheckoutRequest(BaseModel):
    """Payload schema for customer checkout."""

    product_id: int = Field(..., description="ID of the catalog product to purchase", gt=0)
    qty: int = Field(default=1, description="Quantity to purchase", gt=0)


class CheckoutResponse(BaseModel):
    """Response schema for completed checkouts."""

    checkout_id: str
    status: str
    order: dict[str, Any]


def create_gateway_app(
    settings: GatewaySettings | None = None,
    client: GatewayClient | None = None,
    metrics: LabMetrics | None = None,
) -> FastAPI:
    """Factory creating the configured FastAPI gateway service."""
    app_settings = settings or GatewaySettings()
    app_metrics = metrics or LabMetrics()
    fault_state = client.fault_state if client else FaultState()
    version_registry = client.version_registry if client else create_default_version_registry()

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

    client_instance = client or GatewayClient(
        settings=app_settings,
        fault_state=fault_state,
        metrics=app_metrics,
        version_registry=version_registry,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await client_instance.open()
        yield
        await client_instance.close()

    app = FastAPI(
        title="OpsPilot API Gateway Service",
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

    admin_router = create_admin_router(
        settings=app_settings,
        fault_state=fault_state,
        version_registry=version_registry,
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
        "/checkout",
        response_model=CheckoutResponse,
        status_code=status.HTTP_200_OK,
        tags=["checkout"],
    )
    async def checkout(request: CheckoutRequest) -> dict[str, Any]:
        return await client_instance.checkout(
            product_id=request.product_id,
            qty=request.qty,
        )

    # Store state references on app for testing inspection
    app.state.client = client_instance
    app.state.settings = app_settings
    app.state.metrics = app_metrics
    app.state.fault_state = fault_state
    app.state.version_registry = version_registry

    return app


# Default ASGI application instance
app = create_gateway_app()

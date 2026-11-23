import time

from fastapi import APIRouter, FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
POOL_WAIT_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0)


class LabMetrics:
    """Prometheus metrics collector container for OpsPilot lab services."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)

        self.http_requests_total: Counter = Counter(
            "http_requests_total",
            "Total count of HTTP requests",
            ["service", "route", "status"],
            registry=self.registry,
        )

        self.http_request_duration_seconds: Histogram = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["service", "route", "status"],
            buckets=DEFAULT_BUCKETS,
            registry=self.registry,
        )

        self.dependency_call_duration_seconds: Histogram = Histogram(
            "dependency_call_duration_seconds",
            "Duration of downstream dependency calls in seconds",
            ["dependency"],
            buckets=DEFAULT_BUCKETS,
            registry=self.registry,
        )

        self.dependency_errors_total: Counter = Counter(
            "dependency_errors_total",
            "Total count of downstream dependency errors",
            ["dependency"],
            registry=self.registry,
        )

        self.db_pool_in_use: Gauge = Gauge(
            "db_pool_in_use",
            "Database connections currently checked out / in use",
            ["service"],
            registry=self.registry,
        )

        self.db_pool_size: Gauge = Gauge(
            "db_pool_size",
            "Configured or active total size of database connection pool",
            ["service"],
            registry=self.registry,
        )

        self.db_pool_wait_seconds: Histogram = Histogram(
            "db_pool_wait_seconds",
            "Time spent waiting to acquire a connection from the database pool",
            ["service"],
            buckets=POOL_WAIT_BUCKETS,
            registry=self.registry,
        )


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Instruments HTTP requests with Prometheus metrics while excluding admin/metrics routes."""

    def __init__(
        self,
        app: FastAPI,
        service_name: str,
        metrics: LabMetrics,
    ) -> None:
        super().__init__(app)
        self.service_name = service_name
        self.metrics = metrics

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Exclude administrative and internal telemetry routes from metrics
        if path.startswith("/admin") or path == "/metrics":
            return await call_next(request)

        start_time = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - start_time
            # Try to resolve matched route path template (e.g. "/stock/{id}") if available
            route_template = path
            for r in request.app.routes:
                match, _ = r.matches(request.scope)
                if match:
                    route_template = getattr(r, "path", path)
                    break

            self.metrics.http_requests_total.labels(
                service=self.service_name,
                route=route_template,
                status=str(status_code),
            ).inc()

            self.metrics.http_request_duration_seconds.labels(
                service=self.service_name,
                route=route_template,
                status=str(status_code),
            ).observe(duration)


def create_metrics_router(metrics: LabMetrics) -> APIRouter:
    """Creates a router exposing GET /metrics for Prometheus scraping."""
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        body = generate_latest(metrics.registry)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    return router

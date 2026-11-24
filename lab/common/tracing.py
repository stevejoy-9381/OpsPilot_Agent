
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace

try:
    import opentelemetry.instrumentation.fastapi as _otel_fastapi

    FastAPIInstrumentor: Any = _otel_fastapi.FastAPIInstrumentor
except ImportError:
    FastAPIInstrumentor = None

try:
    import opentelemetry.instrumentation.httpx as _otel_httpx

    HTTPXClientInstrumentor: Any = _otel_httpx.HTTPXClientInstrumentor
except ImportError:
    HTTPXClientInstrumentor = None
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_httpx_instrumented: bool = False


def setup_tracing(
    service_name: str,
    version: str = "v1.0.0",
    otlp_endpoint: str | None = None,
) -> TracerProvider:
    """Sets up OpenTelemetry TracerProvider with W3C traceparent propagation and OTLP exporter."""
    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: version,
        }
    )

    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        # Configure OTLP exporter if collector endpoint is specified
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    set_global_textmap(TraceContextTextMapPropagator())
    return provider


def instrument_app(app: FastAPI) -> None:
    """Instruments a FastAPI application with OpenTelemetry, excluding admin & metrics routes."""
    if FastAPIInstrumentor is not None:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=r".*admin.*,.*metrics.*",
            tracer_provider=trace.get_tracer_provider(),
        )


def instrument_httpx() -> None:
    """Instruments httpx client globally to propagate W3C trace context."""
    global _httpx_instrumented
    if not _httpx_instrumented and HTTPXClientInstrumentor is not None:
        HTTPXClientInstrumentor().instrument()
        _httpx_instrumented = True

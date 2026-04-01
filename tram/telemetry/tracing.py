"""OpenTelemetry tracing initialisation for TRAM.

No-op if ``opentelemetry-sdk`` is not installed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_tracer = None


def init_tracing(service_name: str, otlp_endpoint: str) -> None:
    """Configure the OTel SDK with an OTLP gRPC exporter.

    Args:
        service_name: Service name reported to the collector (e.g. "tram").
        otlp_endpoint: OTLP gRPC endpoint, e.g. "http://jaeger:4317".
    """
    global _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
        logger.info(
            "OTel tracing initialised",
            extra={"service": service_name, "endpoint": otlp_endpoint},
        )
    except ImportError:
        logger.warning(
            "opentelemetry-sdk not installed — tracing disabled. "
            "Install with: pip install tram[otel]"
        )
        _tracer = _NoOpTracer()
    except Exception as exc:
        logger.warning("OTel init failed: %s — tracing disabled", exc)
        _tracer = _NoOpTracer()


def get_tracer():
    """Return the configured tracer (or a no-op tracer if OTel not available)."""
    global _tracer
    if _tracer is None:
        try:
            from opentelemetry import trace
            _tracer = trace.get_tracer("tram")
        except ImportError:
            _tracer = _NoOpTracer()
    return _tracer


class _NoOpSpan:
    """Minimal no-op span context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key: str, value) -> None:
        pass

    def set_status(self, status) -> None:
        pass

    def record_exception(self, exc) -> None:
        pass


class _NoOpTracer:
    """Minimal no-op tracer returned when OTel is unavailable."""

    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs):
        return _NoOpSpan()

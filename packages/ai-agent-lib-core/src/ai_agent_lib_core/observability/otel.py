"""Exporting traces and metrics with OpenTelemetry.

The library emits its signals through the OpenTelemetry API, which does
nothing until a host installs providers. This module is that installation for
a service that wants it: providers that send to an OpenTelemetry collector,
normally a sidecar on the same task.

It needs the ``otel`` extra. Where the collector is, and every other exporter
setting, is read by the OpenTelemetry SDK from its own standard variables.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ai_agent_lib_core.config import telemetry_enabled
from ai_agent_lib_core.contracts import ConfigurationError, ServiceConfig

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

__all__ = ["build_providers", "configure_telemetry", "start_telemetry"]

_INSTALL = "install it with: pip install 'ai-agent-lib-core[otel]'"


def build_providers(
    service: str,
    *,
    span_exporter: Any = None,
    metric_reader: Any = None,
) -> tuple[TracerProvider, MeterProvider]:
    """Return a tracer provider and a meter provider for ``service``.

    Args:
        service: The service name every span and metric carries.
        span_exporter: Where finished spans go. By default an OTLP exporter
            over HTTP, which sends to the collector the SDK is pointed at.
        metric_reader: How metrics are read. By default a periodic reader
            over an OTLP exporter.

    Raises:
        ConfigurationError: If the OpenTelemetry SDK or the exporter is not installed.
    """
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if span_exporter is None or metric_reader is None:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        raise ConfigurationError(f"telemetry export is not installed; {_INSTALL}") from exc

    resource = Resource.create({"service.name": service})
    tracers = TracerProvider(resource=resource)
    tracers.add_span_processor(
        BatchSpanProcessor(span_exporter if span_exporter is not None else OTLPSpanExporter())
    )
    reader = (
        metric_reader
        if metric_reader is not None
        else PeriodicExportingMetricReader(OTLPMetricExporter())
    )
    return tracers, MeterProvider(resource=resource, metric_readers=[reader])


def configure_telemetry(
    service: str,
    *,
    span_exporter: Any = None,
    metric_reader: Any = None,
) -> Callable[[], None]:
    """Install providers for ``service`` as the process-wide ones.

    Call it once, in a service's entry point, before the container starts. The
    library's signals reach the providers when the audit option ``tracing`` is on.

    Returns:
        A function that flushes and shuts both providers down. Call it when
        the service stops, so the last spans are not lost.
    """
    from opentelemetry import metrics, trace

    tracers, meters = build_providers(
        service, span_exporter=span_exporter, metric_reader=metric_reader
    )
    trace.set_tracer_provider(tracers)
    metrics.set_meter_provider(meters)

    def shutdown() -> None:
        tracers.shutdown()
        meters.shutdown()

    return shutdown


def _nothing_to_flush() -> None:
    return None


def start_telemetry(service: str, config: ServiceConfig | None) -> Callable[[], None]:
    """Install the providers when the configuration turns telemetry on; else do nothing.

    A service's entry point calls this once, after logging is set up, and
    calls what it returns when the service stops.

    Raises:
        ConfigurationError: If telemetry is on and the ``otel`` extra is missing.
    """
    if config is None or not telemetry_enabled(config):
        return _nothing_to_flush
    try:
        return configure_telemetry(service)
    except ConfigurationError as error:
        error.add_note("telemetry is turned on in the service's settings")
        raise

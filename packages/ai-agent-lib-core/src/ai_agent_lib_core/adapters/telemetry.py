"""Telemetry adapters: a no-op, and one for the OpenTelemetry API.

The library depends on the OpenTelemetry API only. The host installs the SDK,
exporters and collectors; without them every call here costs almost nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from opentelemetry import metrics, trace

from ai_agent_lib_core.contracts import AuditValue

__all__ = ["NullTelemetry", "OpenTelemetryTelemetry"]

_INSTRUMENTATION_NAME = "ai_agent_lib_core"


class NullTelemetry:
    """Telemetry that discards every signal."""

    def event(self, name: str, attributes: Mapping[str, AuditValue]) -> None:
        """Discard the event."""

    def duration(self, name: str, seconds: float, attributes: Mapping[str, AuditValue]) -> None:
        """Discard the duration."""


def _clean(attributes: Mapping[str, AuditValue]) -> dict[str, str | int | float | bool]:
    """Drop ``None`` values, which OpenTelemetry attributes cannot hold."""
    return {key: value for key, value in attributes.items() if value is not None}


class OpenTelemetryTelemetry:
    """Emits metadata-only events and metrics through the OpenTelemetry API.

    An event becomes a span event on the current span and an increment of the
    ``agentlib.events`` counter. A duration is recorded in the
    ``agentlib.duration`` histogram, in seconds.

    Args:
        tracer_provider: Optional. Defaults to the globally configured provider.
        meter_provider: Optional. Defaults to the globally configured provider.
    """

    def __init__(
        self,
        tracer_provider: trace.TracerProvider | None = None,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        self._tracer = trace.get_tracer(_INSTRUMENTATION_NAME, tracer_provider=tracer_provider)
        meter = metrics.get_meter(_INSTRUMENTATION_NAME, meter_provider=meter_provider)
        self._events = meter.create_counter(
            "agentlib.events", unit="1", description="Governed calls, by event and outcome."
        )
        self._durations = meter.create_histogram(
            "agentlib.duration", unit="s", description="Duration of governed calls."
        )

    def event(self, name: str, attributes: Mapping[str, AuditValue]) -> None:
        """Add a span event and count it."""
        cleaned = _clean(attributes)
        trace.get_current_span().add_event(name, cleaned)
        self._events.add(1, {"event": name, **cleaned})

    def duration(self, name: str, seconds: float, attributes: Mapping[str, AuditValue]) -> None:
        """Record how long the event took."""
        self._durations.record(seconds, {"event": name, **_clean(attributes)})

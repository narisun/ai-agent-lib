"""Telemetry adapters: a no-op, and one for the OpenTelemetry API.

The library depends on the OpenTelemetry API only. The host installs the SDK,
exporters and collectors; without them every call here costs almost nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

from ai_agent_lib_core.contracts import AuditValue, SpanNotes

__all__ = ["NullTelemetry", "OpenTelemetryTelemetry"]

_INSTRUMENTATION_NAME = "ai_agent_lib_core"


def _ignore(attributes: Mapping[str, AuditValue], error: BaseException | None = None) -> None:
    """Discard what a span would have been told."""


class NullTelemetry:
    """Telemetry that discards every signal."""

    @contextmanager
    def span(
        self,
        name: str,  # noqa: ARG002 - the port's signature
        attributes: Mapping[str, AuditValue],  # noqa: ARG002 - the port's signature
    ) -> Iterator[SpanNotes]:
        """Open no span."""
        yield _ignore

    def event(self, name: str, attributes: Mapping[str, AuditValue]) -> None:
        """Discard the event."""

    def duration(self, name: str, seconds: float, attributes: Mapping[str, AuditValue]) -> None:
        """Discard the duration."""


def _clean(attributes: Mapping[str, AuditValue]) -> dict[str, str | int | float | bool]:
    """Drop ``None`` values, which OpenTelemetry attributes cannot hold."""
    return {key: value for key, value in attributes.items() if value is not None}


def _notes_for(span: Span) -> SpanNotes:
    def note(attributes: Mapping[str, AuditValue], error: BaseException | None = None) -> None:
        span.set_attributes(_clean(attributes))
        if error is not None:
            # The error's type only: its message can repeat what a caller sent.
            span.set_attribute("error.type", type(error).__name__)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))

    return note


class OpenTelemetryTelemetry:
    """Emits metadata-only spans, events and metrics through the OpenTelemetry API.

    Every governed call is a span, a child of the current one, named and
    labelled after the OpenTelemetry GenAI conventions where they apply, for
    example ``chat claude-sonnet`` with ``gen_ai.request.model``. An event
    becomes a span event on the current span and an increment of the
    ``agentlib.events`` counter. A duration is recorded in the
    ``agentlib.duration`` histogram, in seconds. No span records an
    exception's message.

    Args:
        meter_provider: Optional. Defaults to the globally configured provider.
        tracer_provider: Optional. Defaults to the globally configured provider.
    """

    def __init__(
        self,
        meter_provider: metrics.MeterProvider | None = None,
        tracer_provider: trace.TracerProvider | None = None,
    ) -> None:
        self._tracer = trace.get_tracer(_INSTRUMENTATION_NAME, tracer_provider=tracer_provider)
        meter = metrics.get_meter(_INSTRUMENTATION_NAME, meter_provider=meter_provider)
        self._events = meter.create_counter(
            "agentlib.events", unit="1", description="Governed calls, by event and outcome."
        )
        self._durations = meter.create_histogram(
            "agentlib.duration", unit="s", description="Duration of governed calls."
        )

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, AuditValue]) -> Iterator[SpanNotes]:
        """Open a span around one call, as a child of the current span."""
        with self._tracer.start_as_current_span(
            name,
            kind=trace.SpanKind.CLIENT,
            attributes=_clean(attributes),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield _notes_for(span)

    def event(self, name: str, attributes: Mapping[str, AuditValue]) -> None:
        """Add a span event and count it."""
        cleaned = _clean(attributes)
        trace.get_current_span().add_event(name, cleaned)
        self._events.add(1, {"event": name, **cleaned})

    def duration(self, name: str, seconds: float, attributes: Mapping[str, AuditValue]) -> None:
        """Record how long the event took."""
        self._durations.record(seconds, {"event": name, **_clean(attributes)})

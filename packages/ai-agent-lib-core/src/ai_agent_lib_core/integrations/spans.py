"""Request spans that never carry an error's text.

OpenTelemetry records an exception that leaves a span as an event with its
message and stack, and puts the message in the span's status. A message can
repeat what a caller sent, so the entry points start their spans here instead:
an error is recorded by its type alone.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, Status, StatusCode

__all__ = ["content_free_span"]


@contextmanager
def content_free_span(
    instrumentation: str,
    name: str,
    *,
    attributes: Mapping[str, str | int | float | bool],
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
    context: Context | None = None,
) -> Iterator[Span]:
    """Start a span that records an escaping error by its type, never its message."""
    tracer = trace.get_tracer(instrumentation)
    with tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=dict(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            span.set_attribute("error.type", type(error).__name__)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            raise

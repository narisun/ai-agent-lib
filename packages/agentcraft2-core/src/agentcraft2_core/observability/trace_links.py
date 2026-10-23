"""Carry optional tracing metadata across a request's success or failure boundary."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from agentcraft2_core.contracts import RequestContext


@dataclass
class TraceResult:
    """One request-owned result, also shared with its child execution task."""

    context: RequestContext
    link: dict[str, str] | None = None


_result: ContextVar[TraceResult | None] = ContextVar("agentcraft2_trace_result", default=None)


@contextmanager
def capture_trace(context: RequestContext) -> Iterator[TraceResult]:
    """Capture a fresh result without retaining another request's trace."""
    result = TraceResult(context)
    token = _result.set(result)
    try:
        yield result
    finally:
        _result.reset(token)


def publish_trace(context: RequestContext, link: dict[str, str]) -> None:
    """Publish a server-created link only into the matching request's result."""
    result = _result.get()
    if result is not None and result.context == context:
        result.link = dict(link)

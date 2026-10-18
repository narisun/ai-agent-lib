"""Provider-specific span attributes; graph execution lives in the shared tracer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from opentelemetry.trace import Span

from ai_agent_lib_core.contracts import RequestContext
from ai_agent_lib_core.contracts.diagnostics import TokenUsage


class TraceSettings(Protocol):
    """Only the settings needed while observing an agent turn."""

    @property
    def capture_content(self) -> bool:
        """Whether diagnostic content may leave the agent."""
        ...

    def trace_url(self, trace_id: str) -> str:
        """Return a browser link without credentials."""
        ...


class SpanSchema(Protocol):
    """Map the same observed work to a tracing backend's semantic conventions."""

    def attributes(self, session: str, context: RequestContext) -> dict[str, str]: ...

    def kind(self, span: Span, kind: str) -> None: ...

    def metadata(self, span: Span, name: str, value: str | int | bool) -> None: ...

    def content(self, span: Span, kind: str, text: str) -> None: ...

    def model(self, span: Span, record: Mapping[str, object]) -> None: ...

    def usage(self, span: Span, usage: TokenUsage) -> None: ...

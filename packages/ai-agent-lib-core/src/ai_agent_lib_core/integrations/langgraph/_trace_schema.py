"""Provider-specific span attributes; graph execution lives in the shared tracer."""

from __future__ import annotations

import json
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


class LangfuseSchema:
    """Keep existing Langfuse attributes and token accounting unchanged."""

    def attributes(self, session: str, context: RequestContext) -> dict[str, str]:
        """Correlate requests while hashing the session's identity scope."""
        return {
            "langfuse.session.id": session,
            "agentlib.request_id": context.request_id,
            "langfuse.observation.metadata.request_id": context.request_id,
            "langfuse.observation.metadata.thread_id": context.thread_id,
        }

    def kind(self, span: Span, kind: str) -> None:
        """Classify agent, generation and tool observations."""
        span.set_attribute("langfuse.observation.type", kind)

    def metadata(self, span: Span, name: str, value: str | int | bool) -> None:
        """Attach request and checkpoint references."""
        span.set_attribute(f"langfuse.observation.metadata.{name}", value)

    def content(self, span: Span, kind: str, text: str) -> None:
        """Store already sanitized, bounded JSON."""
        span.set_attribute(f"langfuse.observation.{kind}", text)

    def model(self, span: Span, record: Mapping[str, object]) -> None:
        """Identify the actual model used by this provider attempt."""
        span.set_attribute("langfuse.observation.model.name", str(record["model_id"]))
        span.set_attribute("gen_ai.system", str(record["model_provider"]))

    def usage(self, span: Span, usage: TokenUsage) -> None:
        """Split input subcategories without counting cache tokens twice."""
        counts = {"input": usage.input_tokens, "output": usage.output_tokens}
        if usage.input_tokens is not None:
            counts.update(
                {
                    "input": usage.input_tokens
                    - (usage.cache_read_tokens or 0)
                    - (usage.cache_write_tokens or 0),
                    "cache_read_input_tokens": usage.cache_read_tokens,
                    "cache_creation_input_tokens": usage.cache_write_tokens,
                }
            )
        span.set_attribute(
            "langfuse.observation.usage_details",
            json.dumps({k: v for k, v in counts.items() if v is not None}),
        )

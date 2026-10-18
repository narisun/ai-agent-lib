"""Shared request, model-attempt, tool and checkpoint tracing lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from opentelemetry.context import Context
from opentelemetry.trace import Span, Status, StatusCode, set_span_in_context

from ai_agent_lib_core.contracts import (
    ConfigurationError,
    ModelTraceSink,
    RequestContext,
)
from ai_agent_lib_core.contracts.structured import model_safe_name
from ai_agent_lib_core.integrations.langgraph.context import current_diagnostic_links
from ai_agent_lib_core.integrations.langgraph.model_trace import diagnostic_content
from ai_agent_lib_core.integrations.langgraph.tools import current_native_tool_wrapper
from ai_agent_lib_core.observability.trace_links import publish_trace
from ai_agent_lib_core.observability.usage import normalize_usage
from ai_agent_lib_core.pipeline import bind_request_context

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

    from ai_agent_lib_core.di.container import ServiceContainer

from ai_agent_lib_core.integrations.langgraph._trace_schema import SpanSchema, TraceSettings

GraphResponse = Callable[[Any, "ServiceContainer", RequestContext, Any], Awaitable[dict[str, Any]]]

_LOG = logging.getLogger(__name__)
# The most serialized JSON text kept on one span, counted in characters, not bytes.
# Longer content is replaced by a ``truncated`` marker with a preview of this length.
_CONTENT_LIMIT = 262_144


@dataclass
class _Turn:
    owner: AgentTracing
    root: Span
    attributes: dict[str, str]
    tools: dict[str, tuple[str, str]]
    pending: dict[str, Span] = field(default_factory=dict)
    model_calls: int = 0
    tool_calls: int = 0


_current: ContextVar[_Turn | None] = ContextVar("agentlib_trace_turn", default=None)


def _content(schema: SpanSchema, span: Span, kind: str, value: object) -> None:
    if isinstance(value, BaseMessage):
        value = value.model_dump()
    text = json.dumps(diagnostic_content(value), ensure_ascii=False)
    if len(text) > _CONTENT_LIMIT:
        text = json.dumps({"truncated": True, "preview": text[:_CONTENT_LIMIT]})
        schema.metadata(span, f"{kind}_truncated", True)
    schema.content(span, kind, text)


class _ToolSpans(BaseCallbackHandler):
    """Observe governed tool execution, including denied or failed calls."""

    run_inline = True

    def __init__(self, tracing: AgentTracing) -> None:
        self.tracing = tracing

    def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Start one tool span in the trace of the request this call belongs to."""
        # The request comes from a context variable, so concurrent requests each keep
        # their own trace; nothing remembers "the latest trace" across requests.
        turn = _current.get()
        if turn is None or turn.owner is not self.tracing:
            return
        name = str(serialized.get("name", "tool"))
        # GovernedTool invokes a native tool with the same name underneath it.
        # Record the outer governed call once, including its final checked result.
        if current_native_tool_wrapper() == name:
            return
        registered = turn.tools.get(name)
        span = self.tracing.tracer.start_span(
            registered[1] if registered else name,
            context=set_span_in_context(turn.root),
            attributes=turn.attributes,
        )
        self.tracing.schema.kind(span, "tool")
        self.tracing.schema.metadata(span, "tool_run_id", str(run_id))
        if registered:
            self.tracing.schema.metadata(span, "mcp_server", registered[0])
        if self.tracing.settings.capture_content:
            _content(self.tracing.schema, span, "input", kwargs.get("inputs") or input_str)
        turn.pending[str(run_id)] = span
        turn.tool_calls += 1

    def on_tool_end(self, output: Any, *, run_id: UUID, **_kwargs: Any) -> None:
        """Keep tool result content opt-in; the next model input also shows its use."""
        turn = _current.get()
        if (
            turn is not None
            and turn.owner is self.tracing
            and (span := turn.pending.pop(str(run_id), None)) is not None
        ):
            if isinstance(output, ToolMessage):
                self.tracing.schema.metadata(span, "tool_call_id", output.tool_call_id)
            if self.tracing.settings.capture_content:
                _content(self.tracing.schema, span, "output", output)
            # ToolNode can return an error ToolMessage without raising an exception.
            if isinstance(output, BaseMessage) and getattr(output, "status", None) == "error":
                span.set_status(Status(StatusCode.ERROR))
            span.end()

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **_kwargs: Any) -> None:
        """Record only error type, never arbitrary exception text."""
        turn = _current.get()
        if (
            turn is not None
            and turn.owner is self.tracing
            and (span := turn.pending.pop(str(run_id), None)) is not None
        ):
            span.set_attribute("error.type", type(error).__name__)
            span.set_status(Status(StatusCode.ERROR))
            span.end()


class AgentTracing:
    """Own a trace exporter and, optionally, also feed the local JSONL model-trace sink.

    In a configured service that sink is the file at the configured model-trace
    path. It receives each model record beside the exporter, and the two fail
    independently.

    Injection of a SpanExporter makes the complete graph flow testable without a
    tracing server. An export failure cannot retry an agent or model operation.
    The service owns this adapter and calls close after draining requests.
    """

    def __init__(
        self,
        settings: TraceSettings,
        *,
        application: str,
        exporter: SpanExporter,
        provider_name: str,
        schema: SpanSchema,
        resource_attributes: dict[str, str] | None = None,
        local_sink: ModelTraceSink | None = None,
        checkpoint_timeout_seconds: float = 1.0,
    ) -> None:
        if not math.isfinite(checkpoint_timeout_seconds) or checkpoint_timeout_seconds <= 0:
            raise ValueError("checkpoint_timeout_seconds must be finite and positive")
        self.checkpoint_timeout_seconds = checkpoint_timeout_seconds
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import SpanLimits, TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError as error:
            raise ConfigurationError(
                'Agent tracing needs: python -m pip install "ai-agent-lib-core[otel]"'
            ) from error
        self.application = application
        self.settings = settings
        self.local_sink = local_sink
        self.provider_name = provider_name
        self.schema = schema
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": application, **(resource_attributes or {})}),
            span_limits=SpanLimits(max_attributes=256),
        )
        self.provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=1000))
        self.tracer = self.provider.get_tracer(f"ai_agent_lib.{provider_name}")
        self.tools = _ToolSpans(self)

    async def write(self, record: Mapping[str, object]) -> None:
        """Fan out sanitized provider events; each diagnostic destination fails independently."""
        try:
            self._model_event(record)
        except Exception:  # noqa: BLE001 - diagnostics must not discard an accepted answer
            _LOG.warning("agent_tracing.model_record_failed")
        if self.local_sink is not None:
            try:
                await self.local_sink.write(record)
            except Exception:  # noqa: BLE001 - destinations fail independently
                _LOG.warning("agent_tracing.local_record_failed")

    def _model_event(self, record: Mapping[str, object]) -> None:
        """Open a span for a model request, or finish it with its response or error."""
        turn = _current.get()
        if (
            turn is None
            or turn.owner is not self
            or record.get("request_id") != turn.attributes["agentlib.request_id"]
        ):
            return
        key = f"model:{record['call_id']}:{record['attempt']}"
        event = record.get("event")
        if event == "model.request":
            span = self.tracer.start_span(
                "model", context=set_span_in_context(turn.root), attributes=turn.attributes
            )
            self.schema.kind(span, "generation")
            self.schema.model(span, record)
            self.schema.metadata(span, "call_id", str(record["call_id"]))
            self.schema.metadata(span, "attempt", str(record["attempt"]))
            if node := current_diagnostic_links().node:
                self.schema.metadata(span, "node", node)
            if self.settings.capture_content:
                _content(
                    self.schema,
                    span,
                    "input",
                    {
                        k: record.get(k)
                        for k in ("messages", "tools", "binding_options", "invocation_options")
                    },
                )
            turn.pending[key] = span
            turn.model_calls += 1
        elif (finished := turn.pending.pop(key, None)) is not None:
            span = finished
            if event == "model.response":
                reply = record.get("response")
                if isinstance(reply, Mapping):
                    raw = reply.get("usage_metadata")
                    usage = normalize_usage(
                        raw if isinstance(raw, Mapping) else None, source="langchain"
                    )
                    self.schema.usage(span, usage)
                if self.settings.capture_content:
                    _content(self.schema, span, "output", reply)
            else:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", str(record.get("error_type", "unknown")))
            span.end()

    async def run(
        self,
        graph: Any,
        services: ServiceContainer,
        context: RequestContext,
        given: Any,
        *,
        respond: GraphResponse,
    ) -> dict[str, Any]:
        """Run the agent's response function once and add optional trace metadata.

        ``respond`` receives a graph with tool callbacks, the container, request
        context and input. It returns the agent's existing output dictionary.
        An ``answer`` field is used as root output when present; other output
        shapes are recorded as a whole. Graph state and business nodes stay unchanged.
        """
        session = hashlib.sha256(
            json.dumps(
                [
                    context.principal.tenant,
                    context.principal.subject,
                    context.application,
                    context.thread_id,
                ]
            ).encode()
        ).hexdigest()
        attributes = self.schema.attributes(session, context)
        with self.tracer.start_as_current_span(
            f"{self.application}.turn",
            context=Context(),
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as root:
            self.schema.kind(root, "agent")
            trace_id = format(root.get_span_context().trace_id, "032x")
            link = {
                "provider": self.provider_name,
                "trace_id": trace_id,
                "url": self.settings.trace_url(trace_id),
            }
            publish_trace(context, link)
            turn = _Turn(
                self,
                root,
                attributes,
                {
                    model_safe_name(tool.name): (server.id, tool.name)
                    for server in services.registry.tools.entries()
                    for tool in server.tools
                },
            )
            token = _current.set(turn)
            try:
                if self.settings.capture_content:
                    _content(self.schema, root, "input", given)
                # LangGraph merges these with the graph's existing callbacks.
                observed_graph = graph.with_config(callbacks=[self.tools])
                output = await respond(observed_graph, services, context, given)
                if self.settings.capture_content:
                    _content(self.schema, root, "output", output.get("answer", output))
                artifacts = output.get("artifacts")
                self.schema.metadata(
                    root,
                    "artifact_ids",
                    json.dumps(
                        [
                            artifact["id"]
                            for artifact in (artifacts if isinstance(artifacts, list) else [])
                            if isinstance(artifact, dict) and isinstance(artifact.get("id"), str)
                        ]
                    ),
                )
                await self._checkpoints(graph, services, context, root)
                output = dict(output)
                output["trace"] = link
                return output
            except BaseException as error:
                root.set_status(Status(StatusCode.ERROR))
                root.set_attribute("error.type", type(error).__name__)
                if isinstance(error, Exception):
                    # Failed nodes still leave useful input/router checkpoints.
                    # Cancellation must propagate without another diagnostic read.
                    await self._checkpoints(graph, services, context, root)
                raise
            finally:
                self.schema.metadata(root, "model_calls", turn.model_calls)
                self.schema.metadata(root, "tool_calls", turn.tool_calls)
                for span in turn.pending.values():
                    span.set_status(Status(StatusCode.ERROR))
                    self.schema.metadata(span, "incomplete", True)
                    span.end()
                _current.reset(token)

    async def _checkpoints(
        self, graph: Any, services: ServiceContainer, context: RequestContext, root: Span
    ) -> None:
        """Bound optional history reads; cancellation of the request still propagates."""
        try:
            references = []
            # Read one more reference than the 64 kept: a 65th proves history was cut
            # off, which ``checkpoints_truncated`` reports, and the read stops there.
            async with asyncio.timeout(self.checkpoint_timeout_seconds):
                with bind_request_context(context):
                    history = graph.aget_state_history(
                        services.invocation(context)["config"],
                        filter={"agentlib_request_id": context.request_id},
                        limit=65,
                    )
                    try:
                        async for snapshot in history:
                            references.append(
                                {
                                    "checkpoint_id": snapshot.config.get("configurable", {}).get(
                                        "checkpoint_id"
                                    ),
                                    "parent_id": (snapshot.parent_config or {})
                                    .get("configurable", {})
                                    .get("checkpoint_id"),
                                }
                            )
                            if len(references) == 65:
                                break
                    finally:
                        close = getattr(history, "aclose", None)
                        if close is not None:
                            await close()
            self.schema.metadata(root, "checkpoints", json.dumps(references[:64]))
            self.schema.metadata(root, "checkpoints_truncated", len(references) > 64)
        except TimeoutError:
            self.schema.metadata(root, "checkpoints_unavailable", True)
            self.schema.metadata(root, "checkpoints_timed_out", True)
        except Exception:  # noqa: BLE001 - checkpoint diagnostics are best effort
            self.schema.metadata(root, "checkpoints_unavailable", True)

    def close(self) -> None:
        """Flush once at service shutdown, never on the response path."""
        self.provider.shutdown()

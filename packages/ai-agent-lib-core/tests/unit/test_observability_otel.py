"""Telemetry export: providers for a collector, and a span for each agent request."""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai_agent_lib_core import Principal, RequestContext, ServiceContainer
from ai_agent_lib_core.adapters import (
    OpenTelemetryTelemetry,
    StaticIdentityOptions,
    StaticIdentityVerifier,
)
from ai_agent_lib_core.contracts import Classification, ConfigurationError, ServerEntry, ToolEntry
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app
from ai_agent_lib_core.integrations.mcp import META_REQUEST_ID
from ai_agent_lib_core.observability import build_providers, configure_telemetry
from ai_agent_lib_core.testing import FakeRegistry, Fakes, FrozenClock, SequentialIds


def test_providers_carry_the_service_name_and_export_what_the_library_emits() -> None:
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tracers, meters = build_providers("accounts-agent", span_exporter=spans, metric_reader=reader)
    telemetry = OpenTelemetryTelemetry(meters)
    with tracers.get_tracer("test").start_as_current_span("agent.invoke"):
        telemetry.event("model.call", {"outcome": "success", "skipped": None})
        telemetry.duration("model.call", 0.25, {"outcome": "success"})
    tracers.force_flush()

    (span,) = spans.get_finished_spans()
    assert span.resource.attributes["service.name"] == "accounts-agent"
    assert [(event.name, dict(event.attributes or {})) for event in span.events] == [
        ("model.call", {"outcome": "success"})
    ]
    data = reader.get_metrics_data()
    assert data is not None
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert names == {"agentlib.events", "agentlib.duration"}
    tracers.shutdown()
    meters.shutdown()


def test_the_default_exporters_send_to_a_collector() -> None:
    tracers, meters = build_providers("accounts-agent")
    processor: Any = tracers._active_span_processor._span_processors[0]
    assert type(processor.span_exporter).__name__ == "OTLPSpanExporter"
    tracers.shutdown()
    meters.shutdown()


def test_configuring_installs_the_providers_for_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[str, Any] = {}
    monkeypatch.setattr(trace, "set_tracer_provider", lambda p: installed.setdefault("traces", p))
    monkeypatch.setattr(metrics, "set_meter_provider", lambda p: installed.setdefault("metrics", p))
    shutdown = configure_telemetry(
        "accounts-agent", span_exporter=InMemorySpanExporter(), metric_reader=InMemoryMetricReader()
    )
    assert sorted(installed) == ["metrics", "traces"]
    shutdown()
    assert installed["traces"]._active_span_processor is not None


def test_without_the_sdk_the_fix_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    with pytest.raises(ConfigurationError, match=r"ai-agent-lib-core\[otel\]"):
        build_providers("accounts-agent")


class Services:
    ids = SequentialIds()

    async def authenticate(
        self, credential: str | None, *, application: str, thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:  # fmt: skip
        return RequestContext(
            principal=Principal(subject="ann", tenant="t-1"),
            application=application,
            request_id=request_id or "r-1",
            thread_id=thread_id,
        )


async def _ready() -> None:
    return None


async def test_an_agent_request_is_one_span_that_holds_the_governed_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = InMemorySpanExporter()
    tracers, meters = build_providers(
        "accounts-agent", span_exporter=spans, metric_reader=InMemoryMetricReader()
    )
    tracers.add_span_processor(SimpleSpanProcessor(spans))
    telemetry = OpenTelemetryTelemetry(meters)
    monkeypatch.setattr(trace, "get_tracer", lambda name, **_: tracers.get_tracer(name))

    async def run(context: RequestContext, given: Any) -> Any:
        telemetry.event("tool.call", {"outcome": "success"})
        return {"ok": True}

    lifecycle = ServiceLifecycle(_ready)
    await lifecycle.start()
    app = agent_app(Services(), run, application="accounts-agent", lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
        reply = await http.post(
            "/invoke", json={"input": "a private question"}, headers={"x-request-id": "r-9"}
        )
    assert reply.status_code == 200
    span = spans.get_finished_spans()[0]
    assert span.name == "agent.invoke"
    assert dict(span.attributes or {}) == {
        "agentlib.application": "accounts-agent",
        "agentlib.request_id": "r-9",
    }
    assert [event.name for event in span.events] == ["tool.call"]
    assert "private" not in str(span.to_json())
    tracers.shutdown()
    meters.shutdown()


async def test_a_tool_call_of_a_server_is_one_span_that_holds_its_governed_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = InMemorySpanExporter()
    tracers, meters = build_providers(
        "notes-mcp", span_exporter=spans, metric_reader=InMemoryMetricReader()
    )
    tracers.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(trace, "get_tracer", lambda name, **_: tracers.get_tracer(name))
    tool = ToolEntry(name="notes.read", version="1", classification=Classification.INTERNAL)
    entry = ServerEntry(
        id="notes", owner="tests", url="http://127.0.0.1:1/mcp", audience="notes-mcp", tools=(tool,)
    )
    clock = FrozenClock()
    fakes = Fakes(
        clock=clock,
        identity=StaticIdentityVerifier(StaticIdentityOptions(audience="notes-mcp"), clock),
        registry=FakeRegistry(servers=[entry]),
    )
    container = ServiceContainer(
        fakes.config(), fakes.providers(), clock=clock, telemetry=OpenTelemetryTelemetry(meters)
    )
    async with container as services:
        server: MCPServer[Any] = MCPServer(
            "notes-mcp", middleware=[services.mcp_middleware("notes", application="notes-mcp")]
        )

        @server.tool(name="notes.read")
        async def read(title: str) -> str:
            return f"the private note called {title}"

        async with Client(server) as client:
            meta: Any = {META_REQUEST_ID: "r-9"}
            await client.call_tool("notes.read", {"title": "salary review"}, meta=meta)

    (span,) = [s for s in spans.get_finished_spans() if s.name == "mcp.tool_call"]
    assert dict(span.attributes or {}) == {
        "agentlib.application": "notes-mcp",
        "agentlib.tool": "notes.read",
        "agentlib.request_id": "r-9",
    }
    # The governed call is a child span, named and labelled after the GenAI conventions.
    (child,) = [s for s in spans.get_finished_spans() if s.name == "execute_tool notes.read"]
    assert child.parent is not None
    assert child.parent.span_id == span.context.span_id
    assert dict(child.attributes or {}) == {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "notes.read",
        "agentlib.read_only": False,
        "agentlib.mcp_server": "notes",
        "agentlib.outcome": "success",
    }
    assert "tool.call" in [event.name for event in child.events]
    for finished in spans.get_finished_spans():
        assert "salary" not in str(finished.to_json())
        assert "private" not in str(finished.to_json())
    tracers.shutdown()
    meters.shutdown()


async def test_each_governed_call_is_a_span_with_its_outcome_and_usage() -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from ai_agent_lib_core import PolicyDenied, Principal, bind_request_context
    from ai_agent_lib_core.testing import FakeChatModelProvider, FakePolicyDecisionPoint, Fakes

    reply = AIMessage(
        content="fine",
        usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    )
    fakes = Fakes(
        model=FakeChatModelProvider([reply]),
        policy=FakePolicyDecisionPoint(
            lambda request: "no_rule" if request.resource.name == "secret_tool" else True
        ),
    )
    context = RequestContext(
        principal=Principal(subject="u-1", tenant="t-1"),
        application="accounts-agent",
        request_id="r-1",
        thread_id="th-1",
    )

    def secret_tool(note: str) -> str:
        """Never allowed."""
        return note

    async with fakes.container() as services:
        (tool,) = services.tools([secret_tool])
        with bind_request_context(context):
            await services.model().ainvoke([HumanMessage("a private question")])
            with pytest.raises(PolicyDenied):
                await tool.ainvoke({"note": "a private note"})

    chat, denied = fakes.telemetry.spans
    assert chat.name == "chat fake-model"
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.request.model"] == "fake-model"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 12
    assert chat.attributes["gen_ai.usage.output_tokens"] == 3
    assert chat.attributes["agentlib.outcome"] == "success"
    assert denied.name == "execute_tool secret_tool"
    assert denied.attributes["agentlib.outcome"] == "denied"
    assert denied.attributes["agentlib.reason_code"] == "no_rule"
    assert denied.error_type == "PolicyDenied"
    assert "private" not in str([chat, denied])


@pytest.mark.parametrize("route", ["/invoke", "/invoke/stream"])
async def test_r13_a_failed_request_span_holds_the_errors_type_never_its_text(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    spans = InMemorySpanExporter()
    tracers, meters = build_providers(
        "accounts-agent", span_exporter=spans, metric_reader=InMemoryMetricReader()
    )
    tracers.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(trace, "get_tracer", lambda name, **_: tracers.get_tracer(name))
    secret = "SSN 123-45-6789 password=hunter2"

    async def run(context: RequestContext, given: Any) -> Any:
        raise RuntimeError(secret)

    async def stream(context: RequestContext, given: Any) -> Any:
        yield {"step": 1}
        raise RuntimeError(secret)

    lifecycle = ServiceLifecycle(_ready)
    await lifecycle.start()
    app = agent_app(
        Services(), run, application="accounts-agent", lifecycle=lifecycle, stream=stream
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
        await http.post(route, json={"input": "q"})

    (span,) = [span for span in spans.get_finished_spans() if span.name.startswith("agent.")]
    exported = str(span.to_json())
    assert "123-45-6789" not in exported
    assert "hunter2" not in exported
    assert not any(event.name == "exception" for event in span.events)
    assert dict(span.attributes or {})["error.type"] == "RuntimeError"
    assert span.status.description == "RuntimeError"
    tracers.shutdown()
    meters.shutdown()


def test_r21_telemetry_starts_only_when_the_settings_turn_it_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_agent_lib_core.contracts import ServiceConfig, TelemetryMode
    from ai_agent_lib_core.observability import otel, start_telemetry

    started: list[str] = []

    def configured(service: str, **_: Any) -> Any:
        started.append(service)
        return lambda: started.append("flushed")

    monkeypatch.setattr(otel, "configure_telemetry", configured)
    start_telemetry("accounts-agent", None)()
    start_telemetry("accounts-agent", ServiceConfig.for_testing())()
    assert started == []

    on = ServiceConfig.for_testing(telemetry=TelemetryMode.OPENTELEMETRY)
    stop = start_telemetry("accounts-agent", on)
    stop()
    assert started == ["accounts-agent", "flushed"]


@pytest.mark.parametrize(
    ("mode", "tracing", "on"),
    [("off", False, False), ("off", True, True), ("opentelemetry", False, True)],
)
def test_f7_the_adapter_and_the_sdk_agree_on_whether_telemetry_is_on(
    monkeypatch: pytest.MonkeyPatch, mode: str, tracing: bool, on: bool
) -> None:
    from ai_agent_lib_core.adapters import NullTelemetry, OpenTelemetryTelemetry
    from ai_agent_lib_core.config import telemetry_enabled
    from ai_agent_lib_core.contracts import ProviderSelection, Section, ServiceConfig, TelemetryMode
    from ai_agent_lib_core.di import ServiceContainer
    from ai_agent_lib_core.observability import otel, start_telemetry

    config = ServiceConfig.for_testing(
        telemetry=TelemetryMode(mode),
        sections={Section.AUDIT: ProviderSelection("jsonl", {"tracing": tracing})},
    )
    started: list[str] = []
    monkeypatch.setattr(
        otel, "configure_telemetry", lambda service, **_: lambda: started.append("flushed")
    )
    adapter = ServiceContainer._default_telemetry(config)

    start_telemetry("accounts-agent", config)()
    assert telemetry_enabled(config) is on
    assert isinstance(adapter, OpenTelemetryTelemetry if on else NullTelemetry)
    assert started == (["flushed"] if on else [])

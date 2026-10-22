"""Agent tracing takes its collaborators explicitly; the container path is a convenience."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai_agent_lib_core import RequestContext, ServiceConfig
from ai_agent_lib_core.config import MappingConfigSource
from ai_agent_lib_core.config.langfuse import LANGFUSE_VARIABLES, LangfuseSettings
from ai_agent_lib_core.config.phoenix import PHOENIX_VARIABLES, PhoenixSettings
from ai_agent_lib_core.config.tracing import resolve_tracing_settings
from ai_agent_lib_core.contracts import ConfigurationError, Principal, ServerEntry, ToolEntry
from ai_agent_lib_core.integrations.langgraph.langfuse import LangfuseTracing, langfuse_tracing
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing, phoenix_tracing
from ai_agent_lib_core.integrations.langgraph.tracing import agent_tracing, tracing_lifetime
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink
from ai_agent_lib_core.testing import FakeRegistry

_LANGFUSE = {
    "EAP_LANGFUSE_ENABLED": "true",
    "LANGFUSE_BASE_URL": "http://localhost:3000",
    "LANGFUSE_PROJECT_ID": "trial",
    "LANGFUSE_PUBLIC_KEY": "public-test-key",
    "LANGFUSE_SECRET_KEY": "private-test-key",
}
_PHOENIX = {"EAP_PHOENIX_ENABLED": "true"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*LANGFUSE_VARIABLES.values(), *PHOENIX_VARIABLES):
        monkeypatch.delenv(name, raising=False)


def settings() -> LangfuseSettings:
    return LangfuseSettings("http://localhost:3000", "shared", "public", "secret", True)


def context() -> RequestContext:
    return RequestContext(
        principal=Principal(subject="ada", tenant="demo"),
        application="support",
        thread_id="one",
        request_id="request-1",
    )


class ClosingExporter(InMemorySpanExporter):
    """Record whether the provider flushed and shut this exporter down."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def shutdown(self) -> None:
        self.closed = True
        super().shutdown()


class Graph:
    """Enough of a compiled graph: a copy with callbacks and a checkpoint history."""

    def __init__(self) -> None:
        self.configs: list[Mapping[str, Any]] = []

    def with_config(self, **kwargs: Any) -> Graph:
        return self

    async def aget_state_history(
        self, config: Mapping[str, Any], **kwargs: Any
    ) -> AsyncIterator[Any]:
        self.configs.append(config)
        for index in range(2):
            yield SimpleNamespace(
                config={"configurable": {"checkpoint_id": f"c{index}"}}, parent_config=None
            )


class Services:
    """A hand-written stand-in for the container the deprecated path derives from."""

    def __init__(self, registry: FakeRegistry) -> None:
        self.registry = registry
        self.invocations: list[str] = []

    def invocation(self, request: RequestContext) -> dict[str, Any]:
        self.invocations.append(request.request_id)
        return {"config": {"configurable": {"thread_id": request.thread_id}}}


def registry_with_greet() -> FakeRegistry:
    return FakeRegistry(
        servers=[
            ServerEntry(
                id="data", owner="team", url="http://data", tools=(ToolEntry("greet", "1"),)
            )
        ]
    )


async def test_observe_needs_no_container_and_names_tool_servers_from_the_registry() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(settings(), application="support", exporter=exporter)
    graph = Graph()
    seen: list[tuple[Any, str, Any]] = []

    async def respond(observed: Any, request: RequestContext, given: Any) -> dict[str, Any]:
        seen.append((observed, request.request_id, given))
        run_id = uuid4()
        tracing.tools.on_tool_start({"name": "greet"}, "Ada", run_id=run_id)
        tracing.tools.on_tool_end("Hello Ada", run_id=run_id)
        return {"answer": "Hello Ada", "artifacts": [{"id": "a1"}]}

    try:
        output = await tracing.observe(
            graph,
            context(),
            "Greet Ada",
            respond=respond,
            registry=registry_with_greet(),
            invocation=lambda request: {"config": {"configurable": {"thread_id": "scoped"}}},
        )
        assert seen == [(graph, "request-1", "Greet Ada")]
        assert output["answer"] == "Hello Ada"
        assert output["trace"]["provider"] == "langfuse"
        assert graph.configs == [{"configurable": {"thread_id": "scoped"}}]
        assert tracing.provider.force_flush()
        spans = {span.name: span for span in exporter.get_finished_spans()}
        assert set(spans) == {"support.turn", "greet"}
        tool = spans["greet"].attributes or {}
        assert tool["langfuse.observation.metadata.mcp_server"] == "data"
        root = spans["support.turn"].attributes or {}
        assert json.loads(str(root["langfuse.observation.metadata.checkpoints"])) == [
            {"checkpoint_id": "c0", "parent_id": None},
            {"checkpoint_id": "c1", "parent_id": None},
        ]
        assert json.loads(str(root["langfuse.observation.metadata.artifact_ids"])) == ["a1"]
    finally:
        tracing.close()


async def test_run_derives_registry_and_invocation_from_the_services_object() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(settings(), application="support", exporter=exporter)
    services = Services(registry_with_greet())
    graph = Graph()

    async def respond(
        observed: Any, given_services: Any, request: RequestContext, given: Any
    ) -> dict[str, Any]:
        assert given_services is services
        run_id = uuid4()
        tracing.tools.on_tool_start({"name": "greet"}, "Ada", run_id=run_id)
        tracing.tools.on_tool_end("Hello Ada", run_id=run_id)
        return {"result": given}

    try:
        output = await tracing.run(graph, services, context(), "Greet Ada", respond=respond)
        assert output["result"] == "Greet Ada"
        assert services.invocations == ["request-1"]
        assert graph.configs == [{"configurable": {"thread_id": "one"}}]
        assert tracing.provider.force_flush()
        tool = next(s for s in exporter.get_finished_spans() if s.name == "greet")
        assert (tool.attributes or {})["langfuse.observation.metadata.mcp_server"] == "data"
    finally:
        tracing.close()


async def test_run_without_a_container_explains_what_to_pass_instead() -> None:
    tracing = LangfuseTracing(settings(), application="support", exporter=InMemorySpanExporter())

    async def respond(*args: Any) -> dict[str, Any]:
        raise AssertionError("the turn must not start")

    try:
        with pytest.raises(ConfigurationError, match="registry and invocation") as caught:
            await tracing.run(Graph(), object(), context(), "hi", respond=respond)
        assert caught.value.expected
        assert "observe(" in (caught.value.fix or "")
    finally:
        tracing.close()


async def test_tracing_lifetime_closes_the_adapter_once_and_passes_none_through() -> None:
    exporter = ClosingExporter()
    tracing = PhoenixTracing(PhoenixSettings(), application="support", exporter=exporter)
    async with tracing_lifetime(tracing) as owned:
        assert owned is tracing
        closed_inside = exporter.closed
    # Closing happens on exit, not while requests may still be running.
    assert (closed_inside, exporter.closed) == (False, True)
    absent: PhoenixTracing | None = None
    async with tracing_lifetime(absent) as nothing:
        assert nothing is None


async def test_agent_tracing_selects_only_the_enabled_backend(tmp_path: Path) -> None:
    sink = JsonlModelTraceSink(tmp_path / "model.jsonl")
    config = ServiceConfig.for_testing()
    nothing = resolve_tracing_settings(None, environment=MappingConfigSource({}))
    async with agent_tracing("support", config, resolution=nothing) as off:
        assert off is None
    phoenix_resolution = resolve_tracing_settings(None, environment=MappingConfigSource(_PHOENIX))
    async with agent_tracing(
        "support", config, resolution=phoenix_resolution, local_sink=sink
    ) as phoenix:
        assert isinstance(phoenix, PhoenixTracing)
        assert phoenix.local_sink is sink
    langfuse_resolution = resolve_tracing_settings(None, environment=MappingConfigSource(_LANGFUSE))
    async with agent_tracing("support", config, resolution=langfuse_resolution) as langfuse:
        assert isinstance(langfuse, LangfuseTracing)
        assert langfuse.local_sink is None
    both = resolve_tracing_settings(None, environment=MappingConfigSource(_LANGFUSE))
    with pytest.raises(ConfigurationError, match="Enable one"):
        async with agent_tracing(
            "support",
            config,
            resolution=replace(both, phoenix=phoenix_resolution.phoenix),
        ):
            pytest.fail("two destinations must not start a service")


async def test_convenience_entry_points_take_resolved_settings_and_an_injected_sink(
    tmp_path: Path,
) -> None:
    configured = ServiceConfig.for_testing(model_trace_path=tmp_path / "configured.jsonl")
    injected = JsonlModelTraceSink(tmp_path / "injected.jsonl")
    phoenix_settings = PhoenixSettings()
    async with phoenix_tracing(
        "support", configured, settings=phoenix_settings, local_sink=injected
    ) as phoenix:
        assert isinstance(phoenix, PhoenixTracing)
        assert phoenix.settings is phoenix_settings
        assert phoenix.local_sink is injected
    async with langfuse_tracing("support", configured, settings=settings()) as langfuse:
        assert isinstance(langfuse, LangfuseTracing)
        assert isinstance(langfuse.local_sink, JsonlModelTraceSink)
    async with agent_tracing(
        "support",
        ServiceConfig.for_testing(),
        resolution=resolve_tracing_settings(None, environment=MappingConfigSource(_LANGFUSE)),
    ) as selected:
        assert isinstance(selected, LangfuseTracing)
        assert selected.local_sink is None
    async with agent_tracing("support", ServiceConfig.for_testing(), dotenv_path=None) as off:
        assert off is None

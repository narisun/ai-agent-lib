"""Phoenix uses real graph checkpoints and OpenInference, without needing a server."""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ai_agent_lib_core import RequestContext, ServiceConfig, ServiceContainer
from ai_agent_lib_core.config import MappingConfigSource, load_service_config
from ai_agent_lib_core.config.phoenix import (
    PHOENIX_VARIABLES,
    PhoenixSettings,
    load_phoenix_settings,
)
from ai_agent_lib_core.contracts import ConfigurationError, Principal
from ai_agent_lib_core.integrations.langgraph._agent_tracing import _content
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing
from ai_agent_lib_core.integrations.langgraph.tracing import agent_tracing
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink
from ai_agent_lib_core.testing import calls_tool, fake_providers


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        *PHOENIX_VARIABLES,
        "EAP_LANGFUSE_ENABLED",
        "EAP_LANGFUSE_ENV_FILE",
        "RMPREP_LANGFUSE_ENABLED",
        "RMPREP_LANGFUSE_ENV_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_precedence_explicit_disable_and_secret_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = tmp_path / ".env"
    service.write_text("EAP_PHOENIX_ENV_FILE=traces.env\nPHOENIX_PROJECT_NAME=service\n")
    (tmp_path / "traces.env").write_text(
        "EAP_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=separate\nPHOENIX_API_KEY=private-key\n"
    )
    loaded = load_phoenix_settings(service)
    assert loaded
    assert loaded.project_name == "separate"
    assert not loaded.capture_content
    assert "private-key" not in repr(loaded)
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "shell")
    loaded = load_phoenix_settings(service)
    assert loaded is not None
    assert loaded.project_name == "shell"
    monkeypatch.setenv("EAP_PHOENIX_ENABLED", "true")
    assert load_service_config(service).profile.value == "local"
    monkeypatch.setenv("EAP_PHOENIX_CAPTURE_CONTENT", "yes")
    with pytest.raises(ConfigurationError, match="true or false"):
        load_phoenix_settings(service)
    monkeypatch.setenv("EAP_PHOENIX_ENABLED", "false")
    monkeypatch.setenv("EAP_PHOENIX_ENV_FILE", "missing.env")
    assert load_phoenix_settings(service) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.test",
        "https://user:pass@remote.test",
        "https://example.com/v1/traces",
        "https://example.com?token=x",
        "http://localhost:70000",
        "\x00https://example.com",
    ],
)
def test_settings_reject_unsafe_or_non_origin_destinations(url: str) -> None:
    with pytest.raises(ConfigurationError):
        PhoenixSettings(base_url=url)


def test_planned_child_environment_uses_the_same_file_layers_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = tmp_path / ".env"
    service.write_text("EAP_PHOENIX_ENV_FILE=traces.env\nPHOENIX_PROJECT_NAME=service\n")
    (tmp_path / "traces.env").write_text(
        "EAP_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=separate\n"
    )
    monkeypatch.setenv("EAP_PHOENIX_ENABLED", "false")
    environment = MappingConfigSource({"PHOENIX_PROJECT_NAME": "child"})
    settings = load_phoenix_settings(service, environment=environment)
    assert settings is not None
    assert settings.project_name == "child"
    assert load_phoenix_settings(service) is None
    assert (
        load_phoenix_settings(
            service,
            environment=MappingConfigSource(
                {"EAP_PHOENIX_ENABLED": "false", "EAP_PHOENIX_ENV_FILE": "missing.env"}
            ),
        )
        is None
    )
    with pytest.raises(ValueError, match="either source or environment"):
        load_phoenix_settings(source=environment, environment=environment)


async def test_backend_selection_rejects_two_enabled_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EAP_PHOENIX_ENABLED", "true")
    monkeypatch.setenv("EAP_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:3000")
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "trial")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    with pytest.raises(ConfigurationError, match="Enable one"):
        async with agent_tracing("test", ServiceConfig.for_testing(), dotenv_path=None):
            pytest.fail("Conflicting destinations must not run a request")
    assert load_phoenix_settings(source=MappingConfigSource({})) is None


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_graph(services: ServiceContainer) -> Any:
    def greet(name: str) -> str:
        """Greet a named person."""
        return f"Hello {name}"

    tools = services.tools([greet], read_only=["greet"])
    model = services.model().bind_tools(tools)

    async def agent(state: State) -> dict[str, list[AnyMessage]]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(**services.compile_kwargs())


async def respond(
    graph: Any, services: ServiceContainer, context: RequestContext, question: str
) -> dict[str, Any]:
    state = await graph.ainvoke(
        {"messages": [HumanMessage(question)]}, **services.invocation(context)
    )
    return {"answer": state["messages"][-1].text}


@pytest.mark.parametrize("content", [True, False])
async def test_turns_tools_input_output_usage_checkpoints_and_local_sink(
    tmp_path: Path, content: bool
) -> None:
    exporter = InMemorySpanExporter()
    local = tmp_path / "model.jsonl"
    tracing = PhoenixTracing(
        PhoenixSettings(capture_content=content),
        application="support",
        exporter=exporter,
        local_sink=JsonlModelTraceSink(local),
    )
    reply = AIMessage(
        "Hello Ada",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "input_token_details": {"cache_read": 40, "cache_creation": 20},
        },
    )
    try:
        async with ServiceContainer(
            ServiceConfig.for_testing(),
            fake_providers([calls_tool("greet", name="Ada"), reply, "Your name is Ada"]),
            model_trace=tracing,
        ) as services:
            graph = build_graph(services)
            outputs = []
            for index, question in enumerate(("Greet Ada", "What name did I give?")):
                context = RequestContext(
                    principal=Principal(subject="ada", tenant="demo"),
                    application="support",
                    thread_id="one",
                    request_id=f"request-{index}",
                )
                outputs.append(
                    await tracing.run(graph, services, context, question, respond=respond)
                )
        assert [o["answer"] for o in outputs] == ["Hello Ada", "Your name is Ada"]
        assert all(o["trace"]["provider"] == "phoenix" for o in outputs)
        assert tracing.provider.force_flush()
        spans = exporter.get_finished_spans()
        roots = [s for s in spans if s.name == "support.turn"]
        models = [s for s in spans if s.name == "model"]
        tools = [s for s in spans if s.name == "greet"]
        assert len(roots) == 2
        assert len(models) == 3
        assert len(tools) == 1
        assert len({s.context.trace_id for s in roots}) == 2
        assert len({(s.attributes or {})["session.id"] for s in roots}) == 1
        assert all(
            s.resource.attributes["openinference.project.name"] == "agent-lab" for s in spans
        )
        for root in roots:
            checkpoints = json.loads(str((root.attributes or {})["agentlib.checkpoints"]))
            assert len(checkpoints) >= 3
            assert all(c["checkpoint_id"] for c in checkpoints)
        attributes = models[1].attributes or {}
        assert attributes["llm.token_count.prompt"] == 100
        assert attributes["llm.token_count.completion"] == 10
        assert attributes["llm.token_count.total"] == 110
        assert attributes["llm.token_count.prompt_details.cache_read"] == 40
        assert attributes["llm.token_count.prompt_details.cache_write"] == 20
        if content:
            assert "Hello Ada" in str((models[-1].attributes or {})["input.value"])
            assert (models[-1].attributes or {})[
                "llm.output_messages.0.message.role"
            ] == "assistant"
            assert "Hello Ada" in str((tools[0].attributes or {})["output.value"])
        else:
            assert all(
                "input.value" not in (s.attributes or {})
                and "output.value" not in (s.attributes or {})
                for s in spans
            )
        assert len(local.read_text().splitlines()) == 6
    finally:
        tracing.close()


async def test_cancelled_attempt_ends_spans_without_exception_content() -> None:
    exporter = InMemorySpanExporter()
    tracing = PhoenixTracing(PhoenixSettings(), application="test", exporter=exporter)
    try:
        async with ServiceContainer(ServiceConfig.for_testing(), fake_providers()) as services:
            context = RequestContext(
                principal=Principal(subject="ada", tenant="demo"),
                application="test",
                thread_id="one",
                request_id="a",
            )

            async def cancelled(*args: Any) -> dict[str, Any]:
                await tracing.write(
                    {
                        "event": "model.request",
                        "request_id": "a",
                        "call_id": "x",
                        "attempt": 1,
                        "model_id": "fake",
                        "model_provider": "fake",
                    }
                )
                raise asyncio.CancelledError("private error")

            with pytest.raises(asyncio.CancelledError):
                await tracing.run(build_graph(services), services, context, "hi", respond=cancelled)
        tracing.provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert all(s.status.status_code == StatusCode.ERROR for s in spans)
        assert "private error" not in str([s.attributes for s in spans])
    finally:
        tracing.close()


def test_long_messages_preserve_usage_correlation_and_bounded_redacted_content() -> None:
    exporter = InMemorySpanExporter()
    tracing = PhoenixTracing(PhoenixSettings(), application="test", exporter=exporter)
    messages = [
        {
            "type": "ai",
            "content": f"Turn {index}",
            "tool_calls": [
                {"id": str(number), "name": "lookup", "args": {"api_key": "private-key"}}
                for number in range(15)
            ],
        }
        for index in range(50)
    ]
    try:
        with tracing.tracer.start_as_current_span(
            "model", attributes={"agentlib.request_id": "request", "session.id": "session"}
        ) as span:
            _content(tracing.schema, span, "input", {"messages": messages})
            _content(tracing.schema, span, "output", messages[-1])
            span.set_attribute("llm.token_count.prompt", 1000)
        with tracing.tracer.start_as_current_span("oversized") as span:
            _content(tracing.schema, span, "input", {"messages": ["x" * 300_000]})
        tracing.provider.force_flush()
        normal, oversized = exporter.get_finished_spans()
        attributes = normal.attributes or {}
        assert attributes["agentlib.request_id"] == "request"
        assert attributes["session.id"] == "session"
        assert attributes["llm.token_count.prompt"] == 1000
        assert attributes["agentlib.input_messages_truncated"] is True
        assert "Turn 49" in str(attributes["input.value"])
        assert "private-key" not in str(attributes)
        assert len(attributes) < 256
        value = json.loads(str((oversized.attributes or {})["input.value"]))
        assert value["truncated"] is True
        assert len(value["preview"]) == 262_144
    finally:
        tracing.close()


@pytest.mark.parametrize("streaming", [False, True])
async def test_failed_turn_keeps_trace_and_checkpoint_links_in_http_and_journal(
    tmp_path: Path, streaming: bool
) -> None:
    import httpx

    from ai_agent_lib_core import ValidationFailed
    from ai_agent_lib_core.integrations.http import (
        ServiceLifecycle,
        SqliteOutcomeJournal,
        agent_app,
    )
    from ai_agent_lib_core.testing import FakeIdentityVerifier, Fakes

    exporter = InMemorySpanExporter()
    tracing = PhoenixTracing(
        PhoenixSettings(capture_content=True), application="support", exporter=exporter
    )
    principal = Principal(subject="developer", tenant="local")
    kit = Fakes(identity=FakeIdentityVerifier({"token": principal}))
    try:
        # Phoenix exports independently of the optional operational OTLP collector.
        config = ServiceConfig.for_testing()
        assert config.telemetry.value == "off"
        async with kit.container(config) as services:
            graph = build_graph(services)

            async def broken(
                graph: Any, services: ServiceContainer, context: RequestContext, given: Any
            ) -> dict[str, Any]:
                await respond(graph, services, context, "hello")
                raise ValidationFailed("private invalid draft")

            async def run(context: RequestContext, given: Any) -> dict[str, Any]:
                if given == "untraced":
                    raise ValidationFailed("untraced private draft")
                return await tracing.run(graph, services, context, given, respond=broken)

            async def ready() -> None:
                pass

            lifecycle = ServiceLifecycle(ready)
            await lifecycle.start()
            app = agent_app(
                services,
                run,
                application="support",
                lifecycle=lifecycle,
                live_updates=True,
                outcomes=SqliteOutcomeJournal(tmp_path / "outcomes.sqlite"),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                headers = {
                    "authorization": "Bearer token",
                    "x-request-id": "failed",
                    "accept": "text/event-stream" if streaming else "application/json",
                }
                response = await client.post(
                    "/invoke", headers=headers, json={"thread_id": "thread", "input": "traced"}
                )
                wire = (
                    json.loads(response.text.split("data: ")[-1].strip())
                    if streaming
                    else response.json()
                )
                assert wire["error"] == "invalid"
                assert wire["trace"]["provider"] == "phoenix"
                assert "private invalid draft" not in response.text
                assert (
                    await client.get("/requests/failed?thread_id=thread", headers=headers)
                ).json() == wire
                headers["x-request-id"] = "next"
                next_response = await client.post(
                    "/invoke", headers=headers, json={"thread_id": "thread", "input": "untraced"}
                )
                assert '"trace"' not in next_response.text
        tracing.provider.force_flush()
        roots = [span for span in exporter.get_finished_spans() if span.parent is None]
        assert len(roots) == 1
        root = roots[0]
        assert root.status.status_code == StatusCode.ERROR
        assert format(root.context.trace_id, "032x") == wire["trace"]["trace_id"]
        assert json.loads(str((root.attributes or {})["agentlib.checkpoints"]))
        assert "private invalid draft" not in str(root.attributes)
    finally:
        tracing.close()

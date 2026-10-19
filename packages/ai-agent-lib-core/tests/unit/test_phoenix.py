"""Phoenix uses real graph checkpoints and OpenInference, without needing a server."""

import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ai_agent_lib_core import RequestContext, ServiceConfig, ServiceContainer
from ai_agent_lib_core.config import (
    LEGACY_PREFIX,
    PREFIX,
    MappingConfigSource,
    load_service_config,
)
from ai_agent_lib_core.config import deprecations as deprecations_module
from ai_agent_lib_core.config.bindings import legacy_spelling
from ai_agent_lib_core.config.phoenix import (
    PHOENIX_VARIABLES,
    PhoenixSettings,
    load_phoenix_settings,
    phoenix_env_example,
    resolve_phoenix_settings,
)
from ai_agent_lib_core.contracts import ConfigurationError, Principal
from ai_agent_lib_core.integrations.langgraph._agent_tracing import _content
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing
from ai_agent_lib_core.integrations.langgraph.tracing import agent_tracing
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink
from ai_agent_lib_core.testing import calls_tool, fake_providers


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in PHOENIX_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        if name.startswith(PREFIX):
            monkeypatch.delenv(legacy_spelling(name), raising=False)
    # A deprecation is logged once per process; each test starts from a fresh channel.
    deprecations_module._LOGGED.clear()
    yield
    deprecations_module._LOGGED.clear()


def test_settings_precedence_explicit_disable_and_secret_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = tmp_path / ".env"
    service.write_text("AC2_PHOENIX_ENV_FILE=traces.env\nPHOENIX_PROJECT_NAME=service\n")
    (tmp_path / "traces.env").write_text(
        "AC2_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=separate\nPHOENIX_API_KEY=private-key\n"
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
    monkeypatch.setenv("AC2_PHOENIX_ENABLED", "true")
    assert load_service_config(service).profile.value == "local"
    monkeypatch.setenv("AC2_PHOENIX_CAPTURE_CONTENT", "yes")
    with pytest.raises(ConfigurationError, match="true or false"):
        load_phoenix_settings(service)
    monkeypatch.setenv("AC2_PHOENIX_ENABLED", "false")
    monkeypatch.setenv("AC2_PHOENIX_ENV_FILE", "missing.env")
    assert load_phoenix_settings(service) is None


def test_env_example_comments_every_variable_with_its_default() -> None:
    block = phoenix_env_example()
    lines = [line for line in block.splitlines() if line.strip()]
    assert all(line.startswith("#") for line in lines)
    assert "docs/phoenix-trial.md" in lines[0]
    assert "docs/tracing.md" in phoenix_env_example(guide="docs/tracing.md").splitlines()[1]
    shown = dict(line.removeprefix("# ").split("=", 1) for line in lines[1:])
    assert tuple(shown) == PHOENIX_VARIABLES
    defaults = PhoenixSettings()
    assert shown["AC2_PHOENIX_ENABLED"] == "false"
    assert shown["AC2_PHOENIX_CAPTURE_CONTENT"] == "false"
    assert shown["PHOENIX_COLLECTOR_ENDPOINT"] == defaults.base_url
    assert shown["PHOENIX_PROJECT_NAME"] == defaults.project_name
    assert shown["PHOENIX_API_KEY"] != defaults.api_key


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
    service.write_text("AC2_PHOENIX_ENV_FILE=traces.env\nPHOENIX_PROJECT_NAME=service\n")
    (tmp_path / "traces.env").write_text(
        "AC2_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=separate\n"
    )
    monkeypatch.setenv("AC2_PHOENIX_ENABLED", "false")
    environment = MappingConfigSource({"PHOENIX_PROJECT_NAME": "child"})
    settings = load_phoenix_settings(service, environment=environment)
    assert settings is not None
    assert settings.project_name == "child"
    assert load_phoenix_settings(service) is None
    assert (
        load_phoenix_settings(
            service,
            environment=MappingConfigSource(
                {"AC2_PHOENIX_ENABLED": "false", "AC2_PHOENIX_ENV_FILE": "missing.env"}
            ),
        )
        is None
    )
    with pytest.raises(ValueError, match="either source or environment"):
        load_phoenix_settings(source=environment, environment=environment)


# ------------------------------------------------- the EAP_ spellings, for one minor release
#
# Every test in this section is a dedicated legacy-alias test: it spells Phoenix
# variables with the old ``EAP_`` prefix on purpose. The rest of the file speaks ``AC2_``.


def _config_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "ai_agent_lib_core.config"]


def _resolve(
    values: dict[str, str], *, service: Path | None = None, working_dir: Path | None = None
) -> Any:
    return resolve_phoenix_settings(
        service, environment=MappingConfigSource(values), working_dir=working_dir
    )


def test_the_canonical_names_are_ac2_and_each_has_an_eap_spelling() -> None:
    owned = [name for name in PHOENIX_VARIABLES if name.startswith(PREFIX)]
    assert owned == ["AC2_PHOENIX_ENABLED", "AC2_PHOENIX_CAPTURE_CONTENT", "AC2_PHOENIX_ENV_FILE"]
    assert [legacy_spelling(name) for name in owned] == [
        "EAP_PHOENIX_ENABLED",
        "EAP_PHOENIX_CAPTURE_CONTENT",
        "EAP_PHOENIX_ENV_FILE",
    ]
    assert all(legacy_spelling(name).startswith(LEGACY_PREFIX) for name in owned)


def test_a_canonical_name_enables_tracing_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ai_agent_lib_core.config"):
        resolved = _resolve({"AC2_PHOENIX_ENABLED": "true", "AC2_PHOENIX_CAPTURE_CONTENT": "true"})
    assert resolved.settings is not None
    assert resolved.settings.capture_content
    assert resolved.origins["AC2_PHOENIX_ENABLED"] == "process environment: AC2_PHOENIX_ENABLED"
    assert _config_messages(caplog) == []


def test_a_legacy_name_still_enables_tracing_and_the_warning_names_its_replacement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ai_agent_lib_core.config"):
        resolved = _resolve({"EAP_PHOENIX_ENABLED": "true", "EAP_PHOENIX_CAPTURE_CONTENT": "true"})
    assert resolved.settings is not None
    assert resolved.settings.capture_content
    # Values and origins are keyed by the canonical name; the origin says which spelling won.
    assert resolved.shown["AC2_PHOENIX_ENABLED"] == "true"
    assert resolved.origins["AC2_PHOENIX_ENABLED"] == "process environment: EAP_PHOENIX_ENABLED"
    assert sorted(_config_messages(caplog)) == [
        "EAP_PHOENIX_CAPTURE_CONTENT is deprecated; use AC2_PHOENIX_CAPTURE_CONTENT instead",
        "EAP_PHOENIX_ENABLED is deprecated; use AC2_PHOENIX_ENABLED instead",
    ]


def test_a_legacy_name_is_logged_once_per_process_however_often_it_is_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ai_agent_lib_core.config"):
        for _ in range(3):
            assert _resolve({"EAP_PHOENIX_ENABLED": "true"}).settings is not None
    assert _config_messages(caplog) == [
        "EAP_PHOENIX_ENABLED is deprecated; use AC2_PHOENIX_ENABLED instead"
    ]


def test_a_canonical_resolution_carries_no_warnings() -> None:
    resolved = _resolve({"AC2_PHOENIX_ENABLED": "true", "AC2_PHOENIX_CAPTURE_CONTENT": "true"})
    assert resolved.warnings == ()
    assert _resolve({}).warnings == ()


def test_a_legacy_resolution_carries_each_warning_every_time_it_is_resolved() -> None:
    expected = (
        "EAP_PHOENIX_ENABLED is deprecated; use AC2_PHOENIX_ENABLED instead",
        "EAP_PHOENIX_CAPTURE_CONTENT is deprecated; use AC2_PHOENIX_CAPTURE_CONTENT instead",
    )
    legacy = {"EAP_PHOENIX_ENABLED": "true", "EAP_PHOENIX_CAPTURE_CONTENT": "true"}
    # The log says each name once per process; every resolution still carries its own texts.
    for _ in range(2):
        assert sorted(_resolve(legacy).warnings) == sorted(expected)


@pytest.mark.parametrize("holder", ["process", "service"])
def test_a_legacy_separate_file_setting_is_carried_once_though_it_is_read_twice(
    tmp_path: Path, holder: str
) -> None:
    # The setting is read once to find the file's layer and once with the rest.
    folder = tmp_path / "agent"
    folder.mkdir()
    service = folder / ".env"
    service.write_text("EAP_PHOENIX_ENV_FILE=traces.env\n" if holder == "service" else "")
    (folder / "traces.env").write_text("AC2_PHOENIX_ENABLED=true\n")
    environment = {"EAP_PHOENIX_ENV_FILE": "traces.env"} if holder == "process" else {}
    resolved = _resolve(environment, service=service, working_dir=folder)
    assert resolved.warnings == (
        "EAP_PHOENIX_ENV_FILE is deprecated; use AC2_PHOENIX_ENV_FILE instead",
    )


def test_a_legacy_name_disables_tracing_too() -> None:
    assert (
        _resolve({"EAP_PHOENIX_ENABLED": "false", "EAP_PHOENIX_ENV_FILE": "x.env"}).settings is None
    )


def test_both_spellings_with_equal_values_coexist_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    both = {
        "AC2_PHOENIX_ENABLED": "true",
        "EAP_PHOENIX_ENABLED": "true",
        "AC2_PHOENIX_CAPTURE_CONTENT": "true",
        "EAP_PHOENIX_CAPTURE_CONTENT": "true",
    }
    with caplog.at_level(logging.WARNING, logger="ai_agent_lib_core.config"):
        resolved = _resolve(both)
    assert resolved.settings is not None
    assert resolved.settings.capture_content
    assert resolved.origins["AC2_PHOENIX_ENABLED"] == "process environment: AC2_PHOENIX_ENABLED"
    assert _config_messages(caplog) == []


@pytest.mark.parametrize(
    ("name", "canonical_value", "legacy_value"),
    [
        ("ENABLED", "true", "false"),
        ("CAPTURE_CONTENT", "true", "false"),
        ("ENV_FILE", "one.env", "two.env"),
    ],
)
def test_both_spellings_with_different_values_stop_startup_naming_both(
    name: str, canonical_value: str, legacy_value: str
) -> None:
    values = {
        "AC2_PHOENIX_ENABLED": "true",
        f"AC2_PHOENIX_{name}": canonical_value,
        f"EAP_PHOENIX_{name}": legacy_value,
    }
    with pytest.raises(ConfigurationError) as error:
        _resolve(values)
    message = str(error.value)
    assert f"EAP_PHOENIX_{name}" in message
    assert f"AC2_PHOENIX_{name}" in message
    assert canonical_value not in message.replace(f"AC2_PHOENIX_{name}", "")
    assert legacy_value not in message.replace(f"EAP_PHOENIX_{name}", "")


def test_the_conflict_rule_holds_across_layers(tmp_path: Path) -> None:
    service = tmp_path / ".env"
    service.write_text("EAP_PHOENIX_CAPTURE_CONTENT=false\n")
    environment = {"AC2_PHOENIX_ENABLED": "true", "AC2_PHOENIX_CAPTURE_CONTENT": "true"}
    with pytest.raises(ConfigurationError, match=r"EAP_PHOENIX_CAPTURE_CONTENT.*AC2_PHOENIX_"):
        _resolve(environment, service=service)
    # The same value in both layers is no conflict, and the canonical spelling is not warned about.
    service.write_text("EAP_PHOENIX_CAPTURE_CONTENT=true\n")
    resolved = _resolve(environment, service=service)
    assert resolved.settings is not None
    assert resolved.settings.capture_content


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("holder", ["process", "service"])
def test_the_separate_file_is_found_through_either_spelling_in_either_layer(
    tmp_path: Path, canonical: bool, holder: str
) -> None:
    variable = ("AC2" if canonical else "EAP") + "_PHOENIX_ENV_FILE"
    folder = tmp_path / "agent"
    folder.mkdir()
    service = folder / ".env"
    service.write_text(f"{variable}=traces.env\n" if holder == "service" else "")
    (folder / "traces.env").write_text("AC2_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=separate\n")
    environment = {variable: "traces.env"} if holder == "process" else {}
    resolved = _resolve(environment, service=service, working_dir=folder)
    assert resolved.settings is not None
    assert resolved.settings.project_name == "separate"
    # The separate file is reported as the origin of the settings it supplied.
    assert str((folder / "traces.env").resolve()) in resolved.origins["AC2_PHOENIX_ENABLED"]


def test_a_legacy_disable_in_the_process_skips_the_separate_file_like_the_canonical_one(
    tmp_path: Path,
) -> None:
    service = tmp_path / ".env"
    service.write_text("AC2_PHOENIX_ENV_FILE=missing.env\n")
    assert _resolve({"EAP_PHOENIX_ENABLED": "false"}, service=service).settings is None
    with pytest.raises(ConfigurationError, match="configuration file not found"):
        _resolve({"EAP_PHOENIX_ENABLED": "true"}, service=service)


def test_a_legacy_name_in_the_service_file_enables_tracing_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = tmp_path / ".env"
    service.write_text("EAP_PHOENIX_ENABLED=true\n")
    with caplog.at_level(logging.WARNING, logger="ai_agent_lib_core.config"):
        settings = load_phoenix_settings(service)
    assert settings is not None
    assert _config_messages(caplog) == [
        "EAP_PHOENIX_ENABLED is deprecated; use AC2_PHOENIX_ENABLED instead"
    ]


def test_a_legacy_invalid_value_is_reported_under_the_canonical_name() -> None:
    with pytest.raises(ConfigurationError, match="AC2_PHOENIX_ENABLED must be true or false"):
        _resolve({"EAP_PHOENIX_ENABLED": "yes"})


def test_the_resolver_accepts_a_canonical_phoenix_name_and_the_loader_honours_it(
    tmp_path: Path,
) -> None:
    service = tmp_path / ".env"
    service.write_text("AC2_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=from-file\n")
    # The service resolver accepts the name (it would stop startup otherwise) ...
    assert load_service_config(service).profile.value == "local"
    # ... and the loader that owns it must not ignore it.
    settings = load_phoenix_settings(service)
    assert settings is not None
    assert settings.project_name == "from-file"


async def test_agent_tracing_yields_nothing_until_phoenix_is_enabled(tmp_path: Path) -> None:
    async with agent_tracing("test", ServiceConfig.for_testing(), dotenv_path=None) as tracing:
        assert tracing is None
    service = tmp_path / ".env"
    service.write_text("PHOENIX_PROJECT_NAME=from-file\n")
    async with agent_tracing("test", ServiceConfig.for_testing(), dotenv_path=service) as tracing:
        assert tracing is None


async def test_agent_tracing_starts_phoenix_from_the_service_file_or_given_settings(
    tmp_path: Path,
) -> None:
    service = tmp_path / ".env"
    service.write_text("AC2_PHOENIX_ENABLED=true\nPHOENIX_PROJECT_NAME=from-file\n")
    config = ServiceConfig.for_testing()
    async with agent_tracing("test", config, dotenv_path=service) as tracing:
        assert isinstance(tracing, PhoenixTracing)
        assert tracing.settings.project_name == "from-file"
    given = PhoenixSettings(project_name="given")
    async with agent_tracing("test", config, dotenv_path=service, settings=given) as tracing:
        assert isinstance(tracing, PhoenixTracing)
        assert tracing.settings == given


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


# The behaviors below live in the shared ``AgentTracing`` base class; Phoenix is the
# supported adapter that exercises them.


class ExistingCallbacks(BaseCallbackHandler):
    run_inline = True

    def __init__(self) -> None:
        self.tools: list[str] = []

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        self.tools.append(serialized["name"])


async def respond_with_own_shape(
    graph: Any, services: ServiceContainer, context: RequestContext, question: str
) -> dict[str, Any]:
    state = await graph.ainvoke(
        {"messages": [HumanMessage(question)]}, **services.invocation(context)
    )
    # A non-memo agent with its own output shape; tracing must not rewrite it.
    return {"result": state["messages"][-1].text, "artifacts": None}


@pytest.mark.parametrize("content", [False, True])
async def test_independent_agent_records_tools_usage_context_and_preserves_callbacks(
    content: bool,
) -> None:
    exporter = InMemorySpanExporter()
    tracing = PhoenixTracing(
        PhoenixSettings(capture_content=content), application="support", exporter=exporter
    )
    measured = AIMessage(
        "Hello Ada",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "input_token_details": {"cache_read": 40, "cache_creation": 20},
        },
    )
    callbacks = ExistingCallbacks()
    try:
        async with ServiceContainer(
            ServiceConfig.for_testing(),
            fake_providers([calls_tool("greet", name="Ada"), measured, "Your name is Ada"]),
            model_trace=tracing,
        ) as services:
            graph = build_graph(services).with_config(callbacks=[callbacks], tags=["existing"])
            outputs = []
            for question in ("Greet Ada", "What is my name?"):
                context = RequestContext(
                    principal=Principal(subject="ada", tenant="demo"),
                    application="support",
                    thread_id="one",
                    request_id=question,
                )
                outputs.append(
                    await tracing.run(
                        graph, services, context, question, respond=respond_with_own_shape
                    )
                )
        assert [o["result"] for o in outputs] == ["Hello Ada", "Your name is Ada"]
        # Framework callbacks still see both the governed wrapper and native tool.
        assert callbacks.tools == ["greet", "greet"]
        assert tracing.provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len([s for s in spans if s.name == "greet"]) == 1
        roots = [s for s in spans if s.name == "support.turn"]
        assert len(roots) == 2
        assert len({s.context.trace_id for s in roots if s.context}) == 2
        assert len({s.attributes["session.id"] for s in roots if s.attributes}) == 1
        assert all(s.resource.attributes["service.name"] == "support" for s in spans)
        models = [s for s in spans if s.name == "model"]
        assert len(models) == 3
        assert models[1].attributes
        usage = models[1].attributes
        assert usage["llm.token_count.prompt"] == 100
        assert usage["llm.token_count.completion"] == 10
        assert usage["llm.token_count.total"] == 110
        assert usage["llm.token_count.prompt_details.cache_read"] == 40
        assert usage["llm.token_count.prompt_details.cache_write"] == 20
        for root in roots:
            assert root.attributes
            assert len(json.loads(str(root.attributes["agentlib.checkpoints"]))) >= 3
        if content:
            assert models[-1].attributes
            saved = json.loads(str(models[-1].attributes["input.value"]))
            assert any(m["content"] == "Hello Ada" for m in saved["messages"])
        else:
            assert all("input.value" not in (s.attributes or {}) for s in spans)
    finally:
        tracing.close()


async def test_concurrent_agents_and_cancelled_attempts_do_not_leak_context() -> None:
    exports = [InMemorySpanExporter(), InMemorySpanExporter()]
    tracers = [
        PhoenixTracing(PhoenixSettings(capture_content=True), application=app, exporter=export)
        for app, export in zip(("alpha", "beta"), exports, strict=True)
    ]

    async def run(tracing: PhoenixTracing) -> None:
        async with ServiceContainer(ServiceConfig.for_testing(), fake_providers()) as services:
            context = RequestContext(
                principal=Principal(subject="ada", tenant="demo"),
                application=tracing.application,
                thread_id="same",
                request_id="same",
            )
            graph = build_graph(services)

            async def interrupted(*args: Any) -> dict[str, Any]:
                await tracing.write(
                    {
                        "event": "model.request",
                        "request_id": "same",
                        "call_id": "x",
                        "attempt": 1,
                        "model_id": "fake",
                        "model_provider": "fake",
                    }
                )
                await asyncio.sleep(0)
                raise asyncio.CancelledError

            with pytest.raises(asyncio.CancelledError):
                await tracing.run(graph, services, context, "cancel", respond=interrupted)
            await tracing.write({"event": "model.request", "request_id": "same"})
            tracing.provider.force_flush()

    try:
        await asyncio.gather(*(run(t) for t in tracers))
        sessions = []
        for tracer, export in zip(tracers, exports, strict=True):
            spans = export.get_finished_spans()
            assert {s.name for s in spans} == {f"{tracer.application}.turn", "model"}
            assert all(s.status.status_code == StatusCode.ERROR for s in spans)
            root = next(s for s in spans if s.name.endswith(".turn"))
            assert root.attributes
            sessions.append(root.attributes["session.id"])
        assert len(set(sessions)) == 2
    finally:
        for tracer in tracers:
            tracer.close()


def test_disabled_integration_needs_no_optional_otel_sdk() -> None:
    script = """
import sys, importlib.abc, asyncio
class NoSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("opentelemetry.sdk", "opentelemetry.exporter")):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoSdk())
from ai_agent_lib_core import ServiceConfig
from ai_agent_lib_core.config.phoenix import PhoenixSettings
from ai_agent_lib_core.contracts import ConfigurationError, Principal
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing, phoenix_tracing
async def main():
    async with phoenix_tracing("plain", ServiceConfig.for_testing(), dotenv_path=None) as tracing:
        assert tracing is None
asyncio.run(main())
try:
    PhoenixTracing(PhoenixSettings(), application="x")
except ConfigurationError as error:
    assert "python -m pip" in str(error)
else:
    raise AssertionError("missing SDK must be explained")
"""
    result = subprocess.run(  # noqa: S603 - fixed local test, no network or model calls
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["stalled", "failed", "unlimited", "cancelled"])
async def test_checkpoint_diagnostics_are_bounded_and_preserve_cancellation(mode: str) -> None:
    entered, closed = asyncio.Event(), asyncio.Event()
    exporter = InMemorySpanExporter()
    tracing = PhoenixTracing(
        PhoenixSettings(capture_content=True),
        application="test",
        exporter=exporter,
        checkpoint_timeout_seconds=0.03 if mode != "cancelled" else 10,
    )
    calls = 0

    class Graph:
        def with_config(self, **kwargs: Any) -> Any:
            return self

        async def aget_state_history(self, config: Any, **kwargs: Any) -> Any:
            assert kwargs == {"filter": {"agentlib_request_id": "a"}, "limit": 65}
            entered.set()
            try:
                if mode in {"stalled", "cancelled"}:
                    await asyncio.Event().wait()
                if mode == "failed":
                    raise RuntimeError("private diagnostic detail")
                for index in range(100):
                    yield SimpleNamespace(
                        config={"configurable": {"checkpoint_id": str(index)}}, parent_config=None
                    )
            finally:
                closed.set()

    async def accepted(*args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"answer": "completed", "artifacts": []}

    try:
        async with ServiceContainer(ServiceConfig.for_testing(), fake_providers()) as services:
            context = RequestContext(
                principal=Principal(subject="ada", tenant="test"),
                application="test",
                thread_id="one",
                request_id="a",
            )
            task = asyncio.create_task(
                tracing.run(Graph(), services, context, "hi", respond=accepted)
            )
            if mode == "cancelled":
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await asyncio.wait_for(task, 1)
                assert result["answer"] == "completed"
                assert result["trace"]["provider"] == "phoenix"
            assert calls == 1
            assert closed.is_set()
        assert tracing.provider.force_flush()
        (root,) = exporter.get_finished_spans()
        attributes = root.attributes or {}
        prefix = "agentlib."
        if mode == "unlimited":
            assert len(json.loads(str(attributes[prefix + "checkpoints"]))) == 64
            assert attributes[prefix + "checkpoints_truncated"] is True
        elif mode == "cancelled":
            assert root.status.status_code == StatusCode.ERROR
            assert attributes["error.type"] == "CancelledError"
        else:
            assert attributes[prefix + "checkpoints_unavailable"] is True
            assert root.status.status_code != StatusCode.ERROR
            assert (attributes.get(prefix + "checkpoints_timed_out") is True) == (mode == "stalled")
        assert "private diagnostic detail" not in str(attributes)
    finally:
        tracing.close()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_checkpoint_deadline_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        PhoenixTracing(PhoenixSettings(), application="test", checkpoint_timeout_seconds=timeout)

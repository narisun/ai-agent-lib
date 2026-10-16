"""Shared tracing works with independent agents and does not own business behavior."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
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
from ai_agent_lib_core.config import load_service_config
from ai_agent_lib_core.config.langfuse import (
    LANGFUSE_VARIABLES,
    LangfuseSettings,
    load_langfuse_settings,
)
from ai_agent_lib_core.contracts import ConfigurationError, Principal
from ai_agent_lib_core.integrations.langgraph.langfuse import LangfuseTracing
from ai_agent_lib_core.testing import calls_tool, fake_providers


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LANGFUSE_VARIABLES.values():
        monkeypatch.delenv(name, raising=False)


def settings(content: bool = True) -> LangfuseSettings:
    return LangfuseSettings("http://localhost:3000", "shared", "public", "secret", content)


def test_generic_settings_precedence_relative_file_and_disabled_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("EAP_LANGFUSE_ENV_FILE=traces.env\nLANGFUSE_PROJECT_ID=service\n")
    extra = tmp_path / "traces.env"
    extra.write_text(
        "EAP_LANGFUSE_ENABLED=true\nLANGFUSE_PROJECT_ID=shared\n"
        "LANGFUSE_BASE_URL=http://localhost:3000\nLANGFUSE_PUBLIC_KEY=public\n"
        "LANGFUSE_SECRET_KEY=secret\n"
    )
    monkeypatch.chdir(tmp_path.parent)
    resolved = load_langfuse_settings(dotenv)
    assert resolved is not None
    assert resolved.project_id == "shared"
    assert load_service_config(dotenv).profile.value == "local"
    assert not resolved.capture_content
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "process")
    resolved = load_langfuse_settings(dotenv)
    assert resolved is not None
    assert resolved.project_id == "process"
    monkeypatch.setenv("EAP_LANGFUSE_ENABLED", "true")
    load_service_config(dotenv)
    monkeypatch.setenv("EAP_LANGFUSE_ENABELD", "true")
    with pytest.raises(ConfigurationError, match="EAP_LANGFUSE_ENABLED"):
        load_service_config(dotenv)
    monkeypatch.delenv("EAP_LANGFUSE_ENABELD")
    monkeypatch.setenv("EAP_LANGFUSE_CAPTURE_CONTENT", "yes")
    with pytest.raises(ConfigurationError, match="true or false"):
        load_langfuse_settings(dotenv)
    monkeypatch.setenv("EAP_LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("EAP_LANGFUSE_ENV_FILE", str(tmp_path / "missing"))
    assert load_langfuse_settings(dotenv) is None


def test_rmprep_aliases_obey_layer_precedence_and_canonical_name_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("EAP_LANGFUSE_ENABLED=true\n")
    monkeypatch.setenv("RMPREP_LANGFUSE_ENABLED", "false")
    assert load_langfuse_settings(dotenv, legacy_prefix="RMPREP") is None
    monkeypatch.setenv("EAP_LANGFUSE_ENABLED", "true")
    with pytest.raises(ConfigurationError, match="HTTPS origin"):
        load_langfuse_settings(dotenv, legacy_prefix="RMPREP")


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class ExistingCallbacks(BaseCallbackHandler):
    run_inline = True

    def __init__(self) -> None:
        self.tools: list[str] = []

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        self.tools.append(serialized["name"])


def graph_for(services: ServiceContainer) -> Any:
    def greet(name: str) -> str:
        """Greet the caller."""
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
    # A non-memo agent with its own output shape; tracing must not rewrite it.
    return {"result": state["messages"][-1].text, "artifacts": None}


@pytest.mark.parametrize("content", [False, True])
async def test_independent_agent_records_tools_usage_context_and_preserves_callbacks(
    content: bool,
) -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(settings(content), application="support", exporter=exporter)
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
            graph = graph_for(services).with_config(callbacks=[callbacks], tags=["existing"])
            outputs = []
            for question in ("Greet Ada", "What is my name?"):
                context = RequestContext(
                    principal=Principal(subject="ada", tenant="demo"),
                    application="support",
                    thread_id="one",
                    request_id=question,
                )
                outputs.append(
                    await tracing.run(graph, services, context, question, respond=respond)
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
        assert len({s.attributes["langfuse.session.id"] for s in roots if s.attributes}) == 1
        assert all(s.resource.attributes["service.name"] == "support" for s in spans)
        models = [s for s in spans if s.name == "model"]
        assert len(models) == 3
        assert models[1].attributes
        assert json.loads(str(models[1].attributes["langfuse.observation.usage_details"])) == {
            "input": 40,
            "output": 10,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 20,
        }
        for root in roots:
            assert root.attributes
            assert (
                len(json.loads(str(root.attributes["langfuse.observation.metadata.checkpoints"])))
                >= 3
            )
        if content:
            assert models[-1].attributes
            saved = json.loads(str(models[-1].attributes["langfuse.observation.input"]))
            assert any(m["content"] == "Hello Ada" for m in saved["messages"])
        else:
            assert all("langfuse.observation.input" not in (s.attributes or {}) for s in spans)
    finally:
        tracing.close()


async def test_concurrent_agents_and_cancelled_attempts_do_not_leak_context() -> None:
    exports = [InMemorySpanExporter(), InMemorySpanExporter()]
    tracers = [
        LangfuseTracing(settings(), application=app, exporter=export)
        for app, export in zip(("alpha", "beta"), exports, strict=True)
    ]

    async def run(tracing: LangfuseTracing) -> None:
        async with ServiceContainer(ServiceConfig.for_testing(), fake_providers()) as services:
            context = RequestContext(
                principal=Principal(subject="ada", tenant="demo"),
                application=tracing.application,
                thread_id="same",
                request_id="same",
            )
            graph = graph_for(services)

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
            sessions.append(root.attributes["langfuse.session.id"])
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
from ai_agent_lib_core.config.langfuse import LangfuseSettings
from ai_agent_lib_core.contracts import ConfigurationError, Principal
from ai_agent_lib_core.integrations.langgraph.langfuse import LangfuseTracing, langfuse_tracing
async def main():
    async with langfuse_tracing("plain", ServiceConfig.for_testing(), dotenv_path=None) as tracing:
        assert tracing is None
asyncio.run(main())
try:
    LangfuseTracing(LangfuseSettings("http://localhost:3000", "p", "pk", "sk"), application="x")
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
    tracing = LangfuseTracing(
        settings(),
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
                assert result["trace"]["provider"] == "langfuse"
            assert calls == 1
            assert closed.is_set()
        assert tracing.provider.force_flush()
        (root,) = exporter.get_finished_spans()
        attributes = root.attributes or {}
        prefix = "langfuse.observation.metadata."
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
        LangfuseTracing(settings(), application="test", checkpoint_timeout_seconds=timeout)

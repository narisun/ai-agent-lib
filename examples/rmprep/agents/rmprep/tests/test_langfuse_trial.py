"""Exercise the real graph, governed MCP tools and checkpoint correlation with OTLP spans."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.config import MappingConfigSource
from ai_agent_lib_core.config.phoenix import PhoenixSettings
from ai_agent_lib_core.contracts import ConfigurationError
from ai_agent_lib_core.integrations.langgraph import phoenix
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink
from ai_agent_lib_core.testing import calls_tool, load_test_config, scripted_providers
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rmprep import service
from rmprep.context import ContextBuilder
from rmprep.examples.trace_react import FOLLOW_UP, MEMO, QUESTIONS, WORKSPACE
from rmprep.graph import APPLICATION, build_graph, response
from rmprep.langfuse_settings import LangfuseSettings, load_langfuse_settings
from rmprep.langfuse_trial import LangfuseTrial
from rmprep_data import SERVER_ID, build_server


def settings(*, content: bool = False) -> LangfuseSettings:
    return LangfuseSettings("http://127.0.0.1:3000", "trial", "public-test", "secret-test", content)


def test_explicit_opt_in_and_validated_destination(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert load_langfuse_settings(env, source=MappingConfigSource({})) is None
    with pytest.raises(ConfigurationError):
        load_langfuse_settings(env, source=MappingConfigSource({"RMPREP_LANGFUSE_ENABLED": "true"}))
    for url in (
        "http://remote.test",
        "https://user:pass@remote.test",
        "https://remote.test?q=key",
        "https://remote.test:70000",
        "\x00https://remote.test",
        "http://[invalid]",
    ):
        with pytest.raises(ConfigurationError):
            replace(settings(), base_url=url)
    assert "secret-test" not in repr(settings())
    assert (
        replace(settings(), base_url="http://[::1]:3000")
        .trace_url("a" * 32)
        .startswith("http://[::1]:3000/project/")
    )


def test_separate_settings_file_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / ".env"
    agent.write_text("RMPREP_LANGFUSE_ENABLED=false\nLANGFUSE_PROJECT_ID=agent\n")
    extra = tmp_path / "agent.env"
    extra.write_text(
        "RMPREP_LANGFUSE_ENABLED=true\nLANGFUSE_PROJECT_ID=separate\n"
        "LANGFUSE_BASE_URL=http://localhost:3000\n"
        "LANGFUSE_PUBLIC_KEY=public-test\nLANGFUSE_SECRET_KEY=secret-test\n"
    )
    for name in (
        "RMPREP_LANGFUSE_ENABLED",
        "RMPREP_LANGFUSE_CAPTURE_CONTENT",
        "LANGFUSE_PROJECT_ID",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RMPREP_LANGFUSE_ENV_FILE", str(extra))
    loaded = load_langfuse_settings(agent)
    assert loaded is not None
    assert loaded.project_id == "separate"
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "process")
    loaded = load_langfuse_settings(agent)
    assert loaded is not None
    assert loaded.project_id == "process"
    monkeypatch.setenv("RMPREP_LANGFUSE_ENABLED", "false")
    assert load_langfuse_settings(agent) is None


async def test_service_closes_exporter_when_container_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = Mock()
    # agent_tracing now starts Phoenix only, so the service-level close is exercised there.
    monkeypatch.setenv("EAP_PHOENIX_ENABLED", "true")
    monkeypatch.setenv("EAP_PHOENIX_ENV_FILE", "")
    monkeypatch.setattr(phoenix, "PhoenixTracing", Mock(return_value=trial))
    monkeypatch.setattr(
        service, "ServiceContainer", Mock(side_effect=RuntimeError("invalid adapter"))
    )
    config = load_test_config(WORKSPACE / "agents/rmprep/.env.example", state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="invalid adapter"):
        await service._serve(config, "127.0.0.1", 8000, ContextBuilder())
    trial.close.assert_called_once_with()


@pytest.mark.parametrize("backend", ["langfuse", "phoenix"])
@pytest.mark.parametrize("content", [False, True])
async def test_memo_followups_have_distinct_traces_same_session_and_real_checkpoints(
    tmp_path: Path, content: bool, backend: str
) -> None:
    exporter = InMemorySpanExporter()
    trial = (
        LangfuseTrial(
            settings(content=content),
            exporter=exporter,
            local_sink=JsonlModelTraceSink(tmp_path / "local.jsonl"),
        )
        if backend == "langfuse"
        else PhoenixTracing(
            PhoenixSettings(capture_content=content),
            application=APPLICATION,
            exporter=exporter,
            local_sink=JsonlModelTraceSink(tmp_path / "local.jsonl"),
        )
    )
    prefix = "langfuse.observation.metadata." if backend == "langfuse" else "agentlib."
    session_key = "langfuse.session.id" if backend == "langfuse" else "session.id"
    input_key = "langfuse.observation.input" if backend == "langfuse" else "input.value"
    agent_config = load_test_config(
        WORKSPACE / "agents/rmprep/.env.example", state_dir=tmp_path / "agent"
    )
    server_config = load_test_config(
        WORKSPACE / "mcp-servers/rmprep-data/.env.example", state_dir=tmp_path / "server"
    )
    measured = AIMessage(
        content=MEMO,
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "input_token_details": {"cache_read": 60, "cache_creation": 20},
        },
    )
    replies = scripted_providers(
        calls_tool("rmprep-data.get_memo_evidence", uen="JONE-0022", meeting_date="2026-08-03"),
        measured,
        calls_tool(
            "rmprep-data.get_client_records",
            uen="JONE-0022",
            section="covenants",
            end_date="2026-08-03",
        ),
        FOLLOW_UP,
        "Discuss recorded covenant failures and confirm missing reporting with the client.",
    )
    outputs: list[dict[str, Any]] = []
    request_ids = []
    try:
        async with ServiceContainer(server_config) as server_services:
            connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
            async with ServiceContainer(
                agent_config, replies, mcp_connector=connector, model_trace=trial
            ) as services:
                graph = build_graph(services, await services.mcp_tools(SERVER_ID))
                for question in (*QUESTIONS, "Summarize the next steps in the memo."):
                    context = await services.authenticate(
                        None, application=APPLICATION, thread_id="meeting"
                    )
                    request_ids.append(context.request_id)
                    outputs.append(
                        await trial.run(graph, services, context, question, respond=response)
                    )
        assert trial.provider.force_flush()
        spans = exporter.get_finished_spans()
        roots = [s for s in spans if s.name == "rmprep.turn"]
        assert len(roots) == 3
        assert len({s.context.trace_id for s in roots if s.context}) == 3
        assert len({s.attributes[session_key] for s in roots if s.attributes}) == 1
        for root, output, request_id in zip(roots, outputs, request_ids, strict=True):
            assert root.context
            assert root.attributes
            assert output["trace"]["trace_id"] == format(root.context.trace_id, "032x")
            assert root.attributes["agentlib.request_id"] == request_id
            checkpoints = json.loads(str(root.attributes[prefix + "checkpoints"]))
            assert checkpoints
            assert all(c["checkpoint_id"] for c in checkpoints)
            assert all(
                s.attributes["agentlib.request_id"] == request_id
                for s in spans
                if s.context and s.context.trace_id == root.context.trace_id and s.attributes
            )
            assert all(
                s.parent.span_id == root.context.span_id
                for s in spans
                if s.parent and s.context and s.context.trace_id == root.context.trace_id
            )
        tools = [
            s
            for s in spans
            if s.attributes
            and s.attributes.get(
                "langfuse.observation.type", s.attributes.get("openinference.span.kind")
            )
            in {"tool", "TOOL"}
        ]
        assert [s.name for s in tools] == [
            "rmprep-data.get_memo_evidence",
            "rmprep-data.get_client_records",
        ]
        generations = [s for s in spans if s.name == "model"]
        assert len(generations) == 5  # one span per provider attempt, not inner/outer duplicates
        assert generations[1].attributes
        if backend == "langfuse":
            usage = json.loads(str(generations[1].attributes["langfuse.observation.usage_details"]))
            assert usage == {
                "input": 20,
                "output": 10,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 20,
            }
        else:
            assert generations[1].attributes["llm.token_count.prompt"] == 100
            assert generations[1].attributes["llm.token_count.completion"] == 10
            assert generations[1].attributes["llm.token_count.prompt_details.cache_read"] == 60
            assert all(
                (tool.attributes or {})["agentlib.mcp_server"] == SERVER_ID for tool in tools
            )
        text = str([dict(s.attributes or {}) for s in spans])
        assert ("credit_metrics[uen=JONE-0022]" in text) == content
        if content:
            assert generations[2].attributes
            saved_input = json.loads(str(generations[2].attributes[input_key]))
            assert any(
                isinstance(m["content"], str) and m["content"].startswith(MEMO.strip())
                for m in saved_input["messages"]
            )
        else:
            assert all("langfuse.observation.input" not in (s.attributes or {}) for s in spans)
        assert (tmp_path / "local.jsonl").read_text().count('"event": "model.request"') == 5
    finally:
        trial.close()


async def test_export_failure_and_concurrent_threads_preserve_answers(tmp_path: Path) -> None:
    class BrokenExporter(InMemorySpanExporter):
        def export(self, spans: Any) -> Any:
            raise RuntimeError("backend unavailable")

    trial = LangfuseTrial(settings(), exporter=BrokenExporter())
    config = load_test_config(WORKSPACE / "agents/rmprep/.env.example", state_dir=tmp_path)
    try:
        async with ServiceContainer(
            config, scripted_providers("Answer A", "Answer B"), model_trace=trial
        ) as services:
            graph = build_graph(services)
            contexts = [
                await services.authenticate(None, application=APPLICATION, thread_id=thread)
                for thread in ("a", "b")
            ]
            output = await asyncio.gather(
                *(trial.run(graph, services, ctx, "Hello") for ctx in contexts)
            )
        assert {item["answer"] for item in output} == {"Answer A", "Answer B"}
        assert len({item["trace"]["trace_id"] for item in output}) == 2
        trial.provider.force_flush()
    finally:
        trial.close()

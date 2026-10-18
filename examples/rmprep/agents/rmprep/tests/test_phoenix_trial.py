"""Exercise the real graph, governed MCP tools and checkpoint correlation with OTLP spans."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.config.phoenix import PhoenixSettings
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
from rmprep_data import SERVER_ID, build_server


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


@pytest.mark.parametrize("content", [False, True])
async def test_memo_followups_have_distinct_traces_same_session_and_real_checkpoints(
    tmp_path: Path, content: bool
) -> None:
    exporter = InMemorySpanExporter()
    trial = PhoenixTracing(
        PhoenixSettings(capture_content=content),
        application=APPLICATION,
        exporter=exporter,
        local_sink=JsonlModelTraceSink(tmp_path / "local.jsonl"),
    )
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
        assert len({s.attributes["session.id"] for s in roots if s.attributes}) == 1
        for root, output, request_id in zip(roots, outputs, request_ids, strict=True):
            assert root.context
            assert root.attributes
            assert output["trace"]["trace_id"] == format(root.context.trace_id, "032x")
            assert root.attributes["agentlib.request_id"] == request_id
            checkpoints = json.loads(str(root.attributes["agentlib.checkpoints"]))
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
            if s.attributes and s.attributes.get("openinference.span.kind") == "TOOL"
        ]
        assert [s.name for s in tools] == [
            "rmprep-data.get_memo_evidence",
            "rmprep-data.get_client_records",
        ]
        generations = [s for s in spans if s.name == "model"]
        assert len(generations) == 5  # one span per provider attempt, not inner/outer duplicates
        assert generations[1].attributes
        assert generations[1].attributes["llm.token_count.prompt"] == 100
        assert generations[1].attributes["llm.token_count.completion"] == 10
        assert generations[1].attributes["llm.token_count.prompt_details.cache_read"] == 60
        assert all((tool.attributes or {})["agentlib.mcp_server"] == SERVER_ID for tool in tools)
        text = str([dict(s.attributes or {}) for s in spans])
        assert ("credit_metrics[uen=JONE-0022]" in text) == content
        if content:
            assert generations[2].attributes
            saved_input = json.loads(str(generations[2].attributes["input.value"]))
            assert any(
                isinstance(m["content"], str) and m["content"].startswith(MEMO.strip())
                for m in saved_input["messages"]
            )
        else:
            assert all("input.value" not in (s.attributes or {}) for s in spans)
        assert (tmp_path / "local.jsonl").read_text().count('"event": "model.request"') == 5
    finally:
        trial.close()


async def test_export_failure_and_concurrent_threads_preserve_answers(tmp_path: Path) -> None:
    class BrokenExporter(InMemorySpanExporter):
        def export(self, spans: Any) -> Any:
            raise RuntimeError("backend unavailable")

    trial = PhoenixTracing(PhoenixSettings(), application=APPLICATION, exporter=BrokenExporter())
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
                *(trial.run(graph, services, ctx, "Hello", respond=response) for ctx in contexts)
            )
        assert {item["answer"] for item in output} == {"Answer A", "Answer B"}
        assert len({item["trace"]["trace_id"] for item in output}) == 2
        trial.provider.force_flush()
    finally:
        trial.close()

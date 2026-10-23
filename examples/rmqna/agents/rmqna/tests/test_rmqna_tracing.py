"""The two-question walkthroughs pass tool results and saved history to the model, and the SQL the
model writes shows on the tool's span in Phoenix, and only when content capture is on."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.config.phoenix import PhoenixSettings
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import (
    audit_records,
    calls_tool,
    load_test_config,
    scripted_providers,
)
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rmqna import service
from rmqna.examples import trace_react
from rmqna.examples.trace_anthropic import QUESTIONS, run_two_turns
from rmqna.graph import APPLICATION, build_graph
from rmqna_data import SERVER_ID, build_server

SERVICE = Path(__file__).resolve().parents[1]
SERVER_ENV = SERVICE.parents[1] / "mcp-servers" / "rmqna-data" / ".env.example"


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_two_questions_share_history_and_the_checkpoint_survives_restart(
    tmp_path: Path,
) -> None:
    config = replace(
        load_test_config(SERVICE / ".env.example", state_dir=tmp_path),
        model_trace_path=tmp_path / "model-trace.jsonl",
    )
    history = await run_two_turns(
        config,
        "tracing-test",
        providers=scripted_providers(
            "The facilities, utilization and covenants tables.", "The covenants table."
        ),
    )
    assert [len(turn["messages"]) for turn in history] == [2, 4]
    assert history[0]["request_id"] != history[1]["request_id"]
    assert {turn["thread_id"] for turn in history} == {"tracing-test"}
    assert history[1]["messages"][:2] == history[0]["messages"]
    events = read_events(tmp_path / "model-trace.jsonl")
    assert [event["event"] for event in events] == ["model.request", "model.response"] * 2
    # The second request carries the whole first exchange, then the new question.
    second = events[2]["messages"]
    assert [len(event["messages"]) for event in events[::2]] == [2, 4]
    assert second[1:3] == history[0]["messages"]
    assert second[-1]["content"] == QUESTIONS[1]

    # Read the actual SQLite checkpoint after the walkthrough's container closes.
    async with ServiceContainer(config, scripted_providers()) as services:
        graph = build_graph(services)
        context = await services.authenticate(
            None, application=APPLICATION, thread_id="tracing-test"
        )
        with bind_request_context(context):
            snapshot = await graph.aget_state(services.invocation(context)["config"])
        assert [
            message.model_dump(mode="json") for message in snapshot.values["messages"]
        ] == history[1]["messages"]
        fresh = await services.authenticate(None, application=APPLICATION, thread_id="new-thread")
        with bind_request_context(fresh):
            empty = await graph.aget_state(services.invocation(fresh)["config"])
        assert not empty.values.get("messages")


async def test_the_tool_loop_walkthrough_runs_the_sql_it_scripts_and_saves_both_turns(
    tmp_path: Path,
) -> None:
    history = await trace_react.run_demo(tmp_path)

    # Turn 1: question, tool call, tool result, answer. Turn 2 adds the same four to the history.
    assert [len(turn["messages"]) for turn in history] == [4, 8]
    assert history[1]["messages"][:4] == history[0]["messages"]
    events = read_events(tmp_path / "model-trace.jsonl")
    assert [event["event"] for event in events] == ["model.request", "model.response"] * 4
    # The SQL the model wrote is the tool call's argument, in the trace and in the checkpoint.
    first_call = events[1]["response"]["tool_calls"][0]
    assert first_call["name"] == "rmqna-data_execute_sql"
    assert first_call["args"] == {"sql": trace_react.FIRST_SQL}
    saved_call = history[0]["messages"][1]["tool_calls"][0]
    assert saved_call["args"] == {"sql": trace_react.FIRST_SQL}
    # What the model saw next was the server's real rows, framed as data.
    result = events[2]["messages"][-1]
    assert result["tool_call_id"] == first_call["id"]
    assert result["content"].startswith('<untrusted_tool_result tool="rmqna-data.execute_sql">')
    assert "Boston Pizza International Inc." in result["content"]
    # Every request began with the same instructions, whatever the turn or the tool result.
    prompts = {json.dumps(event["messages"][0]) for event in events[::2]}
    assert len(prompts) == 1

    # The server's own log shows the two statements it ran for the same caller.
    runs = [
        record for record in audit_records(tmp_path / "server") if record["event"] == "tool.call"
    ]
    assert len(runs) == 2
    assert {record["subject"] for record in runs} == {"dev-user"}


CANADIAN = "SELECT uen, name FROM accounts WHERE country = 'Canada' ORDER BY name LIMIT 3"


@pytest.mark.parametrize("content", [False, True])
async def test_the_sql_is_on_the_tool_span_only_when_content_capture_is_on(
    tmp_path: Path, content: bool
) -> None:
    exporter = InMemorySpanExporter()
    trial = PhoenixTracing(
        PhoenixSettings(capture_content=content),
        application=APPLICATION,
        exporter=exporter,
        local_sink=JsonlModelTraceSink(tmp_path / "local.jsonl"),
    )
    agent_config = load_test_config(SERVICE / ".env.example", state_dir=tmp_path / "agent")
    server_config = load_test_config(SERVER_ENV, state_dir=tmp_path / "server")
    replies = scripted_providers(calls_tool("rmqna-data.execute_sql", sql=CANADIAN), "Three.")
    try:
        # The real server, in process: the span is made on the agent's side of a real MCP call.
        async with ServiceContainer(server_config) as server_services:
            connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
            async with ServiceContainer(
                agent_config, replies, mcp_connector=connector, model_trace=trial
            ) as services:
                graph = build_graph(services, await services.mcp_tools(SERVER_ID))
                context = await services.authenticate(
                    None, application=APPLICATION, thread_id="debug-canada"
                )
                output = await trial.run(
                    graph,
                    services,
                    context,
                    "Which Canadian clients?",
                    respond=service.SERVICE.respond,
                )
        assert trial.provider.force_flush()
        spans = exporter.get_finished_spans()
    finally:
        trial.close()
    assert output["answer"] == "Three."

    # One tool span, named for the tool and tagged with the server it was called on.
    tools = [
        s for s in spans if s.attributes and s.attributes.get("openinference.span.kind") == "TOOL"
    ]
    assert [s.name for s in tools] == ["rmqna-data.execute_sql"]
    attributes = dict(tools[0].attributes or {})
    assert attributes["agentcraft2.mcp_server"] == SERVER_ID
    # The model spans are the two provider calls: the one that wrote the SQL, and the answer.
    assert [s.name for s in spans if s.name == "model"] == ["model", "model"]

    everything = str([dict(s.attributes or {}) for s in spans])
    if content:
        # The statement the model wrote is the span's input, and what the server returned its output.
        assert json.loads(str(attributes["input.value"])) == {"sql": CANADIAN}
        assert "Boston Pizza International Inc." in str(attributes["output.value"])
        # It is also the argument of the tool call the model span records.
        model_spans = [dict(s.attributes or {}) for s in spans if s.name == "model"]
        arguments = model_spans[0][
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments"
        ]
        assert json.loads(str(arguments)) == {"sql": CANADIAN}
    else:
        # Metadata only: nothing the model wrote or the server returned, on any span.
        assert all("input.value" not in (s.attributes or {}) for s in spans)
        assert all("output.value" not in (s.attributes or {}) for s in spans)
        assert "accounts" not in everything
        assert "Boston Pizza" not in everything

    # The local model trace records the same calls whatever the capture setting says: it is the
    # other way to read the SQL, and it holds content, so it stays in the service's private folder.
    events = read_events(tmp_path / "local.jsonl")
    sent = [
        call
        for event in events
        if event["event"] == "model.response"
        for call in event["response"]["tool_calls"]
    ]
    assert [call["args"] for call in sent] == [{"sql": CANADIAN}]

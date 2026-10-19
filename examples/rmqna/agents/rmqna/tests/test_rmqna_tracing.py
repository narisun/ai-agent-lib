"""The two-question walkthroughs pass tool results and saved history to the model."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import audit_records, load_test_config, scripted_providers

from rmqna.examples import trace_react
from rmqna.examples.trace_anthropic import QUESTIONS, run_two_turns
from rmqna.graph import APPLICATION, build_graph

SERVICE = Path(__file__).resolve().parents[1]


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

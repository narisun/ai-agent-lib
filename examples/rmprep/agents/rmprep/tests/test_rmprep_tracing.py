"""The business walkthrough executes MCP tools and persists both specialist turns."""

import json
from pathlib import Path

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import audit_records, load_test_config, scripted_providers

from rmprep.examples.trace_react import MEMO, QUESTIONS, WORKSPACE, run_demo
from rmprep.graph import APPLICATION, answer, build_graph


async def test_memo_and_follow_up_share_tool_evidence_and_survive_restart(tmp_path: Path) -> None:
    history = await run_demo(tmp_path)
    assert [turn["route"] for turn in history] == ["memoagent", "qnaagent"]
    assert [len(turn["messages"]) for turn in history] == [4, 8]
    assert {turn["thread_id"] for turn in history} == {"jone-0022-meeting"}
    assert history[0]["request_id"] != history[1]["request_id"]
    assert history[1]["messages"][:4] == history[0]["messages"]
    events = [
        json.loads(line)
        for line in (tmp_path / "model-trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["model.request", "model.response"] * 4
    requests = events[::2]
    assert [len(event["messages"]) for event in requests] == [2, 4, 6, 8]
    # Q&A gets the original memo and a projection of its original covenant evidence.
    assert requests[2]["messages"][1:3] == history[0]["messages"][:2]
    assert requests[2]["messages"][4] == history[0]["messages"][3]
    assert '"covenants": {' in requests[2]["messages"][3]["content"]
    assert '"financials": {' not in requests[2]["messages"][3]["content"]
    assert '"financials": {' in history[0]["messages"][2]["content"]
    assert requests[2]["messages"][-1]["content"] == QUESTIONS[1]
    assert requests[2]["messages"][-2]["content"].startswith(MEMO)
    memo = history[0]["memos"][0]
    assert memo["uen"] == "JONE-0022" and memo["meeting_date"] == "2026-08-03"
    assert history[1]["context_diagnostics"]["memo_ids"] == [memo["memo_id"]]
    assert history[1]["context_diagnostics"]["estimated_input_tokens"] > 0
    assert '"estimated_input_tokens"' not in str(requests[2]["messages"][0]["content"])
    for request_index, source in [(1, "credit_metrics[uen=JONE-0022]"), (3, "FAC000000130")]:
        tool_result = requests[request_index]["messages"][-1]
        preceding_call = events[2 * request_index - 1]["response"]["tool_calls"][0]
        assert tool_result["tool_call_id"] == preceding_call["id"]
        assert source in tool_result["content"]
        assert tool_result["content"].startswith("<untrusted_tool_result")

    queries = [
        record for record in audit_records(tmp_path / "server") if record["event"] == "data.query"
    ]
    assert len(queries) == 22 and all(record["outcome"] == "success" for record in queries)
    config = load_test_config(
        WORKSPACE / "agents/rmprep/.env.example", state_dir=tmp_path / "agent"
    )
    async with ServiceContainer(
        config, scripted_providers("The saved memo identifies recorded covenant failures.")
    ) as services:
        graph = build_graph(services)
        context = await services.authenticate(
            None, application=APPLICATION, thread_id="jone-0022-meeting"
        )
        with bind_request_context(context):
            saved = await graph.aget_state(services.invocation(context)["config"])
        assert [message.model_dump(mode="json") for message in saved.values["messages"]] == history[
            1
        ]["messages"]
        assert saved.values["memos"] == history[1]["memos"]
        assert saved.values["active_memo_id"] == memo["memo_id"]
        reply = await answer(graph, services, context, f"Explain {memo['memo_id']}.")
        assert reply == "The saved memo identifies recorded covenant failures."
        other = await services.authenticate(
            None, application=APPLICATION, thread_id="another-thread"
        )
        reply = await answer(graph, services, other, f"Explain {memo['memo_id']}.")
        assert "No matching memo is saved in this thread" in reply

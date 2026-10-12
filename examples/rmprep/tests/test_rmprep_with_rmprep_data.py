"""rmprep and rmprep-data together, in one process with no network.

Both services run with their own configuration. The model is scripted: it
decides to call a tool of the server, and the test checks what came back for
the caller the agent acted for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.testing import (
    audit_records,
    calls_tool,
    last_shown_to_model,
    load_test_config,
    scripted_providers,
)
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector

from rmprep import APPLICATION as AGENT
from rmprep import answer, build_graph
from rmprep.graph import steps
from rmprep_data import SERVER_ID, build_server

ROOT = Path(__file__).resolve().parents[1]
AGENT_ENV = ROOT / "agents" / "rmprep" / ".env.example"
SERVER_ENV = ROOT / "mcp-servers" / "rmprep-data" / ".env.example"


QUESTION = "What does rmprep-data have?"
CALLS_THE_TOOL = calls_tool("rmprep-data.get_client_records", uen="JONE-0022", section="profile")


async def ask_as(roles: list[str], tmp_path: Path, question: str, *replies: Any) -> str:
    """Ask the agent as a local caller with ``roles``; return what the model was shown."""
    server_config = load_test_config(SERVER_ENV, state_dir=tmp_path / "server")
    # The caller is whoever uses the agent: on a developer's machine, the
    # development identity, here with the roles the test gives it.
    agent_config = load_test_config(AGENT_ENV, state_dir=tmp_path / "agent", roles=roles)
    async with ServiceContainer(server_config) as server_services:
        connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
        async with ServiceContainer(
            agent_config, scripted_providers(*replies), mcp_connector=connector
        ) as services:
            context = await services.authenticate(None, application=AGENT, thread_id="th-1")
            # Only this server's tools, so the test needs no other server running.
            graph = build_graph(services, await services.mcp_tools(SERVER_ID))
            await answer(graph, services, context, question)
            return last_shown_to_model(services)


async def test_the_agent_answers_from_the_server_as_an_analyst(tmp_path: Path) -> None:
    shown = await ask_as(
        ["analyst"], tmp_path, QUESTION, CALLS_THE_TOOL, "Here it is: accounts[uen=JONE-0022]."
    )
    # The model was shown the table as data.
    assert shown.startswith('<untrusted_tool_result tool="rmprep-data.get_client_records">')

    # Each side keeps its own audit log, for the same person and the same request.
    agent_call = audit_records(tmp_path / "agent")[1]
    assert agent_call["attributes"]["mcp_server"] == SERVER_ID
    assert agent_call["attributes"]["policy_reason_code"] == "rmprep-calls-rmprep-data"
    for record in audit_records(tmp_path / "server"):
        assert record["subject"] == agent_call["subject"] == "dev-user"
        assert record["request_id"] == agent_call["request_id"]


async def test_a_caller_the_server_does_not_allow_gets_a_refusal_the_model_can_explain(
    tmp_path: Path,
) -> None:
    shown = await ask_as(["intern"], tmp_path, QUESTION, CALLS_THE_TOOL, "I may not.")
    assert "The call was denied" in shown


async def test_stream_reports_the_memo_tool_loop_without_repeating_old_messages(
    tmp_path: Path,
) -> None:
    server_config = load_test_config(SERVER_ENV, state_dir=tmp_path / "server")
    agent_config = load_test_config(AGENT_ENV, state_dir=tmp_path / "agent")
    async with ServiceContainer(server_config) as server_services:
        connector = InProcessMcpConnector({SERVER_ID: build_server(server_services)})
        async with ServiceContainer(
            agent_config,
            scripted_providers(
                calls_tool(
                    "rmprep-data.get_memo_evidence", uen="JONE-0022", meeting_date="2026-08-03"
                ),
                "Sample memo: accounts[uen=JONE-0022].",
                "Explanation",
            ),
            mcp_connector=connector,
        ) as services:
            graph = build_graph(services, await services.mcp_tools(SERVER_ID))
            context = await services.authenticate(None, application=AGENT, thread_id="meeting-1")
            first = [
                step
                async for step in steps(
                    graph, services, context, "memo: Prepare a memo for JONE-0022 as of 2026-08-03."
                )
            ]
            second = [step async for step in steps(graph, services, context, "Explain the memo.")]
    assert [step["node"] for step in first] == [
        "router",
        "memoagent/model",
        "memoagent/tools",
        "memoagent/model",
    ]
    assert [step["node"] for step in second] == ["router", "qnaagent/model"]
    assert len(second[1]["messages"]) == 1
    assert second[1]["messages"][0]["content"] == "Explanation"

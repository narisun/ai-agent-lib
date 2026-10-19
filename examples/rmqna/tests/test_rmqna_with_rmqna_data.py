"""rmqna and rmqna-data together, in one process with no network.

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

from rmqna import APPLICATION as AGENT
from rmqna import answer, build_graph
from rmqna_data import SERVER_ID, build_server

ROOT = Path(__file__).resolve().parents[1]
AGENT_ENV = ROOT / "agents" / "rmqna" / ".env.example"
SERVER_ENV = ROOT / "mcp-servers" / "rmqna-data" / ".env.example"


# A model calls an MCP tool by its registered name; calls_tool spells it the way
# a model API needs it.


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


CANADIAN = "SELECT uen, name FROM accounts WHERE country = 'Canada' ORDER BY name LIMIT 3"


async def test_the_agent_answers_from_the_server_with_the_rows_the_sql_selected(
    tmp_path: Path,
) -> None:
    shown = await ask_as(
        ["analyst"],
        tmp_path,
        "Which Canadian clients are there?",
        calls_tool("rmqna-data.execute_sql", sql=CANADIAN),
        "Three of them.",
    )
    # The model was shown the table as data: the columns and the rows the statement selected.
    assert shown.startswith('<untrusted_tool_result tool="rmqna-data.execute_sql">')
    assert "Boston Pizza International Inc." in shown
    assert "Canada Goose Holdings Inc." in shown

    # Each side keeps its own audit log, for the same person and the same request.
    agent_call = audit_records(tmp_path / "agent")[1]
    assert agent_call["attributes"]["mcp_server"] == SERVER_ID
    assert agent_call["attributes"]["policy_reason_code"] == "rmqna-calls-rmqna-data"
    for record in audit_records(tmp_path / "server"):
        assert record["subject"] == agent_call["subject"] == "dev-user"
        assert record["request_id"] == agent_call["request_id"]


async def test_a_manager_is_shown_the_same_rows(tmp_path: Path) -> None:
    # No per-role mask applies to the model's own SQL (docs/sql-governance.md).
    shown = await ask_as(
        ["manager"],
        tmp_path,
        "Which Canadian clients are there?",
        calls_tool("rmqna-data.execute_sql", sql=CANADIAN),
        "Three of them.",
    )
    assert "Boston Pizza International Inc." in shown


async def test_a_refused_statement_reaches_the_model_as_data_it_can_repair_from(
    tmp_path: Path,
) -> None:
    shown = await ask_as(
        ["analyst"],
        tmp_path,
        "Remove every client.",
        calls_tool("rmqna-data.execute_sql", sql="DELETE FROM accounts"),
        "I can only read data.",
    )
    assert "not_a_select" in shown
    assert "Boston Pizza" not in shown


async def test_a_caller_the_server_does_not_allow_gets_a_refusal_the_model_can_explain(
    tmp_path: Path,
) -> None:
    shown = await ask_as(
        ["intern"],
        tmp_path,
        "Which Canadian clients are there?",
        calls_tool("rmqna-data.execute_sql", sql=CANADIAN),
        "I may not.",
    )
    assert "The call was denied" in shown
    assert "Boston Pizza" not in shown

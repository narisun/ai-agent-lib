"""rmqna and rmqna-data together, in one process with no network.

Both services run with their own configuration, and the server is the real one: the real engine
over the workspace's data, the real validator, the real rules and registry. Only the model is
scripted. It decides which tool to call and with what SQL; the test checks what the server
answered, how the model was shown it, and what each side recorded for the caller the agent acted for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.testing import (
    audit_records,
    calls_tool,
    load_test_config,
    scripted_model,
    scripted_providers,
)
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector
from langchain_core.messages import BaseMessage

from rmqna import APPLICATION as AGENT
from rmqna import answer, build_graph
from rmqna.prompts import schema_document, system_prompt
from rmqna_data import SERVER_ID, build_server

ROOT = Path(__file__).resolve().parents[1]
AGENT_ENV = ROOT / "agents" / "rmqna" / ".env.example"
SERVER_ENV = ROOT / "mcp-servers" / "rmqna-data" / ".env.example"

EXECUTE = "rmqna-data.execute_sql"
LOOKUP = "rmqna-data.search_column_values"
SCHEMA = "rmqna-data.get_schema"
FRAME = '<untrusted_tool_result tool="{}">'


@dataclass(frozen=True)
class Run:
    """One question to the agent: what it answered, and everything the model was sent."""

    reply: str
    prompts: list[list[BaseMessage]]
    agent_dir: Path
    server_dir: Path

    @property
    def shown(self) -> str:
        """The last thing the model was shown: after a tool call, that tool's framed result."""
        return str(self.prompts[-1][-1].content)

    def result(self, call: int) -> Any:
        """The data of the tool result the model saw in its ``call``-th request (1 is the first).

        A result reaches the model inside the untrusted-data frame; the frame is checked here and
        the JSON inside it is returned.
        """
        text = str(self.prompts[call][-1].content)
        assert text.startswith("<untrusted_tool_result tool=")
        body = text.split("\n", 1)[1].rsplit("\n</untrusted_tool_result>", 1)[0]
        return json.loads(body)


async def run_as(roles: list[str], tmp_path: Path, question: str, *replies: Any) -> Run:
    """Ask the agent as a local caller with ``roles``; return what happened."""
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
            reply = await answer(graph, services, context, question)
            prompts = [list(call) for call in scripted_model(services).models[0].calls]
    return Run(reply, prompts, tmp_path / "agent", tmp_path / "server")


async def ask_as(roles: list[str], tmp_path: Path, question: str, *replies: Any) -> str:
    """Ask the agent as a local caller with ``roles``; return what the model was shown last."""
    return (await run_as(roles, tmp_path, question, *replies)).shown


CANADIAN = "SELECT uen, name FROM accounts WHERE country = 'Canada' ORDER BY name LIMIT 3"


async def test_the_agent_answers_from_the_server_as_an_analyst(tmp_path: Path) -> None:
    run = await run_as(
        ["analyst"],
        tmp_path,
        "Which Canadian clients are there?",
        calls_tool(EXECUTE, sql=CANADIAN),
        "Three of them, as of 2026-06-30.",
    )
    assert run.reply == "Three of them, as of 2026-06-30."

    # The model was shown the table as data: the columns and the rows the statement selected,
    # inside the frame that marks a tool result as something to read, not to obey.
    assert run.shown.startswith(FRAME.format(EXECUTE))
    table = run.result(1)
    assert table["columns"] == ["uen", "name"]
    assert table["row_count"] == 3 and table["truncated"] is False
    assert [row[1] for row in table["rows"]] == [
        "Boston Pizza International Inc.",
        "Canada Goose Holdings Inc.",
        "Freshii Inc.",
    ]

    # Every request carried the same static prompt: instructions, the schema document the server
    # also serves, and the examples. The agent never sends anything else as its instructions.
    assert schema_document() in system_prompt()
    for prompt in run.prompts:
        instructions = prompt[0]
        assert instructions.additional_kwargs["agentlib_static_prefix"] is True
        assert isinstance(instructions.content, list)
        assert isinstance(instructions.content[0], dict)
        assert instructions.content[0]["text"] == system_prompt()

    # Each side keeps its own audit log, for the same person and the same request.
    agent_call = audit_records(run.agent_dir)[1]
    assert agent_call["attributes"]["mcp_server"] == SERVER_ID
    assert agent_call["attributes"]["policy_reason_code"] == "rmqna-calls-rmqna-data"
    for record in audit_records(run.server_dir):
        assert record["subject"] == agent_call["subject"] == "dev-user"
        assert record["request_id"] == agent_call["request_id"]


async def test_a_manager_is_shown_the_same_rows(tmp_path: Path) -> None:
    # No per-role mask applies to the model's own SQL (docs/sql-governance.md).
    shown = await ask_as(
        ["manager"],
        tmp_path,
        "Which Canadian clients are there?",
        calls_tool(EXECUTE, sql=CANADIAN),
        "Three of them.",
    )
    assert "Boston Pizza International Inc." in shown


async def test_a_refused_statement_reaches_the_model_as_data(tmp_path: Path) -> None:
    run = await run_as(
        ["analyst"],
        tmp_path,
        "Remove every client.",
        calls_tool(EXECUTE, sql="DROP TABLE accounts"),
        calls_tool(EXECUTE, sql="SELECT COUNT(*) AS clients FROM accounts"),
        "I can only read data.",
    )
    # A refusal is an ordinary tool result: the model reads {error_kind, message} inside the same
    # frame as rows, and nothing is raised on the way.
    assert str(run.prompts[1][-1].content).startswith(FRAME.format(EXECUTE))
    refusal = run.result(1)
    assert refusal["error_kind"] == "not_a_select"
    assert "DROP" in refusal["message"]
    assert set(refusal) == {"error_kind", "message"}
    # And the statement did nothing: the table is all there when the model looks.
    assert run.result(2)["rows"] == [[46]]
    assert run.reply == "I can only read data."


async def test_a_refusal_is_repaired_from_its_message_into_an_answer(tmp_path: Path) -> None:
    guess = "SELECT a.name, f.committed_amount FROM facilities f JOIN accounts a ON a.uen = f.uen"
    repaired = (
        "SELECT a.name, f.commitment FROM facilities f JOIN accounts a ON a.uen = f.uen "
        "ORDER BY f.commitment DESC LIMIT 1"
    )
    run = await run_as(
        ["analyst"],
        tmp_path,
        "Who has the largest commitment?",
        calls_tool(EXECUTE, sql=guess),
        calls_tool(EXECUTE, sql=repaired),
        "Faribault Woolen Mill Co., $62.0M.",
    )
    failure = run.result(1)
    # DuckDB's own words reach the model, with the candidate it should use instead.
    assert failure["error_kind"] == "execution_error"
    assert "committed_amount" in failure["message"]
    assert 'Candidate bindings: : "commitment"' in failure["message"]
    # The repair is a second statement, and its rows are what the answer rests on.
    assert run.result(2)["rows"] == [["Faribault Woolen Mill Co.", 61981785.76]]
    assert [len(prompt) for prompt in run.prompts] == [2, 4, 6]
    assert run.reply == "Faribault Woolen Mill Co., $62.0M."


async def test_a_name_is_resolved_before_it_is_filtered_on(tmp_path: Path) -> None:
    deals = (
        "SELECT o.stage, o.amount FROM opportunities o JOIN accounts a ON a.uen = o.uen "
        "WHERE a.name = 'Wawona Packing Co., LLC' AND o.stage = 'Proposal'"
    )
    run = await run_as(
        ["analyst"],
        tmp_path,
        "What deals does Wawona have in proposal?",
        calls_tool(LOOKUP, table="accounts", column="name", search="wawona"),
        calls_tool(EXECUTE, sql=deals),
        "One: $38.0M.",
    )
    # The lookup returns the stored spelling, and the statement filters on exactly that.
    assert run.result(1) == {"values": ["Wawona Packing Co., LLC"], "truncated": False}
    assert run.result(2)["rows"] == [["Proposal", 38007299.25]]

    # Both tools were allowed to the agent by the one rule that names the server.
    reasons = {
        record["attributes"]["policy_reason_code"]
        for record in audit_records(run.agent_dir)
        if record["event"] == "tool.call"
    }
    assert reasons == {"rmqna-calls-rmqna-data"}


async def test_an_ambiguous_name_comes_back_as_every_match(tmp_path: Path) -> None:
    run = await run_as(
        ["analyst"],
        tmp_path,
        "How many open tickets does Vermont have?",
        calls_tool(LOOKUP, table="accounts", column="name", search="vermont"),
        "Which do you mean: The Vermont Country Store, Inc. or Vermont Teddy Bear Company?",
    )
    # Two matches: the model is shown both, which is what makes the clarifying question possible.
    assert run.result(1) == {
        "values": ["The Vermont Country Store, Inc.", "Vermont Teddy Bear Company"],
        "truncated": False,
    }


async def test_the_schema_the_model_can_ask_for_is_the_one_it_was_given(tmp_path: Path) -> None:
    run = await run_as(
        ["analyst"], tmp_path, "What tables are there?", calls_tool(SCHEMA), "Thirty-one."
    )
    assert run.result(1) == {"schema": schema_document()}


async def test_a_caller_the_server_does_not_allow_gets_a_refusal_the_model_can_explain(
    tmp_path: Path,
) -> None:
    shown = await ask_as(
        ["intern"],
        tmp_path,
        "Which Canadian clients are there?",
        calls_tool(EXECUTE, sql=CANADIAN),
        "I may not.",
    )
    assert "The call was denied" in shown
    assert "Boston Pizza" not in shown

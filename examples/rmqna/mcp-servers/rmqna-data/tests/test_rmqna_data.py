"""The server with its own configuration, called in process with no network.

Everything is real except the transport: the CSV engine, the rules file, the registry and the audit
log. A test picks the caller's roles, calls a tool the way an agent does, and checks what that
caller was shown. The engine and the validator have their own suites; these tests are about the
tools as the agent meets them: their registration, who may call them, what a refusal looks like
on the wire, and what the logs do and do not hold.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.integrations.mcp import mcp_result_text, verify_registration
from ai_agent_lib_core.testing import TEST_AGENT, audit_records, load_test_config
from ai_agent_lib_core.testing.mcp import call_tool_as
from mcp.server.mcpserver import MCPServer

from rmqna_data import APPLICATION, SERVER_ID, build_server
from rmqna_data.settings import SCHEMA_PATH

SERVICE = Path(__file__).resolve().parents[1]
Running = tuple[ServiceContainer, MCPServer[Any]]

EXECUTE = "rmqna-data.execute_sql"
VALUES = "rmqna-data.search_column_values"
SCHEMA = "rmqna-data.get_schema"


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[Running]:
    """The server, started the way its command line starts it.

    A server only answers an agent the registry lists for it. `test_agent`
    adds one for the test, so these tests do not depend on which agents exist.
    """
    config = load_test_config(SERVICE / ".env.example", state_dir=tmp_path, test_agent=TEST_AGENT)
    async with ServiceContainer(config) as services:
        await services.validate()
        yield services, build_server(services)


async def sql_as(running: Running, sql: str, roles: list[str]) -> Any:
    services, server = running
    return await call_tool_as(services, server, EXECUTE, {"sql": sql}, roles=roles)


async def test_the_tools_are_the_ones_the_registry_lists(running: Running) -> None:
    services, server = running
    entry = services.registry.tools.get(SERVER_ID)
    assert entry is not None
    # A tool that is offered but not registered, or the other way round, fails
    # here. So does a tool whose arguments changed since it was pinned: run
    # 'agentlib registry pin rmqna-data' after changing a tool on purpose.
    await verify_registration(server, entry, require_pins=False)
    assert entry.audience == APPLICATION
    assert {tool.name for tool in entry.tools} == {EXECUTE, VALUES, SCHEMA}


async def test_every_tool_is_pinned_and_the_pins_are_current(running: Running) -> None:
    services, server = running
    entry = services.registry.tools.get(SERVER_ID)
    assert entry is not None

    await verify_registration(server, entry, require_pins=True)


async def test_the_sample_tools_are_gone(running: Running) -> None:
    _, server = running

    names = {tool.name for tool in await server.list_tools()}

    assert names == {EXECUTE, VALUES, SCHEMA}


async def test_an_analyst_and_a_manager_both_run_sql(running: Running) -> None:
    for roles in (["analyst"], ["manager"]):
        result = await sql_as(
            running, "SELECT uen, name FROM accounts WHERE country = 'Canada' ORDER BY uen", roles
        )
        table = result.structured_content

        assert not result.is_error
        assert table["columns"] == ["uen", "name"]
        assert table["row_count"] == len(table["rows"]) > 0
        assert table["truncated"] is False
        assert isinstance(table["elapsed_ms"], int)
        assert "error_kind" not in table


async def test_a_long_result_is_cut_at_the_cap_and_says_so(running: Running) -> None:
    result = await sql_as(running, "SELECT * FROM transactions", ["analyst"])
    table = result.structured_content

    assert (table["row_count"], len(table["rows"]), table["truncated"]) == (200, 200, True)


async def test_a_query_nobody_matches_is_an_empty_table_not_an_error(running: Running) -> None:
    result = await sql_as(running, "SELECT uen FROM accounts WHERE name = 'Nobody'", ["manager"])

    assert not result.is_error
    assert result.structured_content["rows"] == []
    assert result.structured_content["row_count"] == 0


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("DROP TABLE accounts", "not_a_select"),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("SELECT 1 UNION SELECT 2", "set_operation"),
        ("SELECT * FROM customers", "unknown_table"),
        ("SELECT * FROM read_csv('accounts.csv')", "forbidden_function"),
        ("SELEC 1", "parse_error"),
        ("SELECT no_such_column FROM accounts", "execution_error"),
    ],
)
async def test_a_refusal_is_a_normal_result_the_agent_can_read(
    running: Running, sql: str, kind: str
) -> None:
    result = await sql_as(running, sql, ["analyst"])

    # Not a tool error: the model sees the payload and can repair its SQL.
    assert not result.is_error
    payload = result.structured_content
    assert set(payload) == {"error_kind", "message"}
    assert payload["error_kind"] == kind
    assert payload["message"]


async def test_the_engines_own_message_reaches_the_agent_verbatim(running: Running) -> None:
    result = await sql_as(running, "SELECT no_such_column FROM accounts", ["analyst"])

    assert 'Referenced column "no_such_column" not found' in result.structured_content["message"]


async def test_values_resolve_a_name_fragment(running: Running) -> None:
    services, server = running
    result = await call_tool_as(
        services,
        server,
        VALUES,
        {"table": "accounts", "column": "name", "search": "jones"},
        roles=["analyst"],
    )

    assert not result.is_error
    assert any(v.startswith("Jones Dairy Farm") for v in result.structured_content["values"])
    assert result.structured_content["truncated"] is False


async def test_values_for_an_unknown_column_are_a_readable_refusal(running: Running) -> None:
    services, server = running
    result = await call_tool_as(
        services,
        server,
        VALUES,
        {"table": "accounts", "column": "nme", "search": "x"},
        roles=["analyst"],
    )

    assert not result.is_error
    assert result.structured_content["error_kind"] == "unknown_table"
    assert "'nme'" in result.structured_content["message"]


async def test_get_schema_serves_the_committed_document(running: Running) -> None:
    services, server = running
    result = await call_tool_as(services, server, SCHEMA, {}, roles=["manager"])

    assert not result.is_error
    assert result.structured_content == {"schema": SCHEMA_PATH.read_text(encoding="utf-8")}
    assert result.structured_content["schema"].startswith("# Schema document v1")


async def test_any_other_role_is_denied_before_the_tool_runs(
    running: Running, tmp_path: Path
) -> None:
    result = await sql_as(running, "SELECT 1", ["intern"])

    assert result.is_error
    assert mcp_result_text(result) == "The call was denied (reason: no_matching_rule)."
    (record,) = audit_records(tmp_path)
    assert (record["event"], record["outcome"]) == ("tool.call", "denied")


async def test_the_denial_holds_for_each_tool(running: Running) -> None:
    services, server = running
    for tool, arguments in [
        (EXECUTE, {"sql": "SELECT 1"}),
        (VALUES, {"table": "accounts", "column": "name", "search": "x"}),
        (SCHEMA, {}),
    ]:
        result = await call_tool_as(services, server, tool, arguments, roles=["intern"])
        assert result.is_error, tool
        assert "no_matching_rule" in mcp_result_text(result), tool


async def test_the_audit_log_holds_the_decision_and_not_the_sql(
    running: Running, tmp_path: Path
) -> None:
    marker = "zq-audit-marker-7731"
    await sql_as(running, f"SELECT '{marker}' AS note", ["analyst"])

    # One decision for the call. There is no data.query decision: the SQL is the model's, not a
    # named query, so the tool-level rule is the whole policy story (docs/sql-governance.md).
    (call,) = audit_records(tmp_path)
    assert call["event"] == "tool.call"
    assert call["attributes"]["policy_reason_code"] == "rmqna-data-staff-call-the-tools"
    assert marker not in json.dumps(call)


async def test_the_server_logs_hold_counts_and_error_kinds_and_never_the_sql(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    services, server = running
    caplog.set_level(logging.INFO)
    sql_marker, value_marker = "zqsqlmarker5512", "zqvaluemarker9034"

    await sql_as(running, f"SELECT '{sql_marker}' AS note, uen FROM accounts", ["analyst"])
    await sql_as(running, f"SELECT '{sql_marker}' FROM nosuch_{sql_marker}", ["analyst"])
    await sql_as(running, f"SELECT {sql_marker} FROM accounts", ["analyst"])  # a DuckDB error
    await sql_as(running, f"DROP TABLE {sql_marker}", ["analyst"])
    await call_tool_as(
        services,
        server,
        VALUES,
        {"table": "accounts", "column": "name", "search": value_marker},
        roles=["analyst"],
    )

    assert sql_marker not in caplog.text
    assert value_marker not in caplog.text
    lines = [r.__dict__ for r in caplog.records if r.name == "rmqna_data.server"]
    assert [line["tool"] for line in lines] == [
        "execute_sql",
        "execute_sql",
        "execute_sql",
        "execute_sql",
        "search_column_values",
    ]
    assert [line.get("error_kind") for line in lines] == [
        None,
        "unknown_table",
        "execution_error",
        "not_a_select",
        None,
    ]
    first = lines[0]
    assert (first["row_count"], first["column_count"], first["truncated"]) == (46, 2, False)
    assert isinstance(first["duration_ms"], int)

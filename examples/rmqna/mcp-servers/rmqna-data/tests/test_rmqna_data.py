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
from mcp_types import CallToolResult

from rmqna_data import APPLICATION, SERVER_ID, build_server
from rmqna_data import validate as validate_module
from rmqna_data.engine import ExecuteResult, ValuesResult
from rmqna_data.server import add_tools
from rmqna_data.settings import SCHEMA_PATH
from rmqna_data.validate import ErrorKind, SqlRefused

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
    # 'agentcraft2 registry pin rmqna-data' after changing a tool on purpose.
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


async def test_the_tool_description_names_every_generator_the_validator_denies(
    running: Running,
) -> None:
    _, server = running

    tool = next(tool for tool in await server.list_tools() if tool.name == EXECUTE)

    # The description is what the model reads before it writes SQL. One the server denies and the
    # description omits is a refusal the model could have been spared.
    for name in validate_module._GENERATORS:
        assert name in (tool.description or ""), name


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


async def test_now_is_returned_as_a_utc_timestamp_over_the_wire(running: Running) -> None:
    result = await sql_as(running, "SELECT now() AS n", ["analyst"])

    assert not result.is_error
    assert result.structured_content["rows"][0][0].endswith("+00:00")


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
        ("SELECT * FROM accounts PIVOT (count(*) FOR country IN ('USA'))", "not_a_select"),
        ("SELECT sum(x) FROM (SELECT unnest(range(0, 3000000000)) AS x)", "forbidden_function"),
        ("SELECT list(t) FROM transactions t", "execution_error"),  # one row, over the byte ceiling
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


async def call_stub(server: MCPServer[Any], tool: str, arguments: dict[str, Any]) -> CallToolResult:
    result = await server.call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    return result


class StubEngine:
    """An engine that answers from a script: all the tools need is the protocol."""

    def __init__(self, error: SqlRefused | None = None) -> None:
        self.error = error
        self.seen: list[str] = []

    async def execute(self, sql: str) -> ExecuteResult:
        self.seen.append(sql)
        if self.error is not None:
            raise self.error
        return ExecuteResult(columns=["n"], rows=[[1]], row_count=1, truncated=False, elapsed_ms=3)

    async def values(self, table: str, column: str, search: str) -> ValuesResult:
        self.seen.append(f"{table}.{column}:{search}")
        if self.error is not None:
            raise self.error
        return ValuesResult(values=["A"], truncated=True)


async def test_the_tools_depend_on_the_engine_protocol_alone() -> None:
    engine = StubEngine()
    server: MCPServer[Any] = MCPServer("stub")
    add_tools(server, engine, "# a schema")

    ran = await call_stub(server, EXECUTE, {"sql": "SELECT 1"})
    found = await call_stub(server, VALUES, {"table": "t", "column": "c", "search": "s"})
    schema = await call_stub(server, SCHEMA, {})

    assert ran.structured_content == {
        "columns": ["n"],
        "rows": [[1]],
        "row_count": 1,
        "truncated": False,
        "elapsed_ms": 3,
    }
    assert found.structured_content == {"values": ["A"], "truncated": True}
    assert schema.structured_content == {"schema": "# a schema"}
    assert engine.seen == ["SELECT 1", "t.c:s"]


@pytest.mark.parametrize("kind", ["timeout", "execution_error", "parse_error"])
async def test_any_refusal_the_engine_raises_becomes_a_normal_result(kind: ErrorKind) -> None:
    engine = StubEngine(SqlRefused(kind, "say what to change"))
    server: MCPServer[Any] = MCPServer("stub")
    add_tools(server, engine, "# a schema")

    for tool, arguments in [
        (EXECUTE, {"sql": "SELECT 1"}),
        (VALUES, {"table": "t", "column": "c", "search": "s"}),
    ]:
        result = await call_stub(server, tool, arguments)
        assert not result.is_error
        assert result.structured_content == {"error_kind": kind, "message": "say what to change"}

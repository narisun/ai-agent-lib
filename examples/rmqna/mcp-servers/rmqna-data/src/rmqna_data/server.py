"""The MCP server: three tools over one contained SQL engine, and the library's governance.

The tools are thin. ``server.py`` declares their transport-only signatures and turns an engine
refusal into a normal tool result, so the agent can read it and repair its SQL; everything that
decides what SQL may run lives in ``validate.py`` and ``engine.py``. The library governs every call
exactly as it does for any tool: identity, the registry's pins, policy, guardrails and audit.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Protocol

from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.contracts import ConfigurationError
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from rmqna_data.engine import ExecuteResult, RmqnaEngine, ValuesResult
from rmqna_data.settings import DATA_DIR, SCHEMA_PATH
from rmqna_data.validate import SqlRefused

__all__ = ["APPLICATION", "SERVER_ID", "SqlEngine", "add_tools", "build_server"]

APPLICATION = "rmqna-data"
"""The server's application name: its token audience, and its name in policy and audit."""

SERVER_ID = "rmqna-data"
"""The server's ID in the tool registry. Agents name its tools ``rmqna-data/<tool>``."""

INSTRUCTIONS = (
    "Read-only SQL over the relationship-manager data. Call get_schema to see the tables and "
    "columns. Turn a name the user typed into the exact stored value with search_column_values "
    "before filtering on it. Run one SELECT at a time with execute_sql. A refused or failing "
    "statement comes back as {error_kind, message}, not as an error: read the message and repair "
    "the SQL. A result is cut at 200 rows (truncated says so) and a statement may run for 30 "
    "seconds."
)

_LOG = logging.getLogger(__name__)


class SqlEngine(Protocol):
    """The only engine the tools need: SQL or a lookup in, a result or a ``SqlRefused`` out.

    The tools depend on this, not on DuckDB; ``build_server`` is the one place that builds the real
    engine (rmprep's layering, where its evidence service sees only a ``QuerySource``).
    """

    async def execute(self, sql: str) -> ExecuteResult:
        """Validate and run one SELECT."""
        ...

    async def values(self, table: str, column: str, search: str) -> ValuesResult:
        """Find the stored values of a column that contain some text."""
        ...


def build_server(services: ServiceContainer) -> MCPServer[Any]:
    """Build the MCP server from governed parts; the one place the engine is constructed.

    Args:
        services: A started service container.

    Raises:
        ConfigurationError: If the data folder or the schema document is missing.
    """
    engine = RmqnaEngine(DATA_DIR)
    schema = _read_schema()
    server: MCPServer[Any] = MCPServer(
        APPLICATION,
        instructions=INSTRUCTIONS,
        # The middleware that governs every tool call and, when callers present tokens from an
        # identity provider, the token check at the door.
        **services.mcp_server_kwargs(SERVER_ID, application=APPLICATION),
    )
    add_tools(server, engine, schema)
    return server


def add_tools(server: MCPServer[Any], engine: SqlEngine, schema: str) -> None:
    """Register the three tools on ``server``, over any engine that speaks the protocol."""

    @server.tool(name="rmqna-data.execute_sql")
    async def execute_sql(
        sql: Annotated[str, Field(description="One DuckDB SELECT statement.")],
    ) -> dict[str, Any]:
        """Run one read-only SQL SELECT over the loaded tables (DuckDB dialect) and return rows.

        Write a single SELECT; it may begin with WITH. Name tables bare, as the schema document
        lists them (FROM accounts, never FROM crm.accounts). Set operations (UNION, EXCEPT,
        INTERSECT), PIVOT, row, list and string generators (range, generate_series,
        generate_subscripts, unnest, repeat, rpad, lpad, printf, format, concat_ws, list_resize,
        array_resize, equi_width_bins), table and file functions, and any statement that changes
        something are refused. At most 200 rows and 2 MB come back: truncated=true means the rows
        were cut, so filter or aggregate instead of listing everything. A refused or failing
        statement returns {error_kind, message}; the message says what to change, so repair the
        SQL and try again.
        """
        started = time.perf_counter()
        try:
            result = await engine.execute(sql)
        except SqlRefused as refused:
            _log_call("execute_sql", started, error_kind=refused.error_kind)
            return refused.to_payload()
        _log_call(
            "execute_sql",
            started,
            row_count=result.row_count,
            column_count=len(result.columns),
            truncated=result.truncated,
        )
        return result.to_payload()

    @server.tool(name="rmqna-data.search_column_values")
    async def search_column_values(
        table: Annotated[str, Field(description="A table name from the schema document.")],
        column: Annotated[str, Field(description="A column of that table.")],
        search: Annotated[str, Field(description="Text to look for inside the values.")],
    ) -> dict[str, Any]:
        """Find the stored values of one column that contain some text, ignoring case.

        Use it to turn a name the user typed ('jones dairy') into the exact value to filter on,
        instead of guessing a LIKE pattern. Returns up to 25 distinct values; truncated=true
        means more matched, so search for more of the text. Any column works: a number or a date
        is matched on its text form. An unknown table or column returns {error_kind, message}.
        """
        started = time.perf_counter()
        try:
            found = await engine.values(table, column, search)
        except SqlRefused as refused:
            _log_call("search_column_values", started, error_kind=refused.error_kind)
            return refused.to_payload()
        _log_call(
            "search_column_values", started, row_count=len(found.values), truncated=found.truncated
        )
        return found.to_payload()

    @server.tool(name="rmqna-data.get_schema")
    async def get_schema() -> dict[str, Any]:
        """Return the schema document: every table and column with its type, keys and joins.

        It also carries value lists, conventions and the definitions of the metrics. It is the
        same text the agent's prompt carries, so this is for checking, not for every question.
        """
        started = time.perf_counter()
        _log_call("get_schema", started)
        return {"schema": schema}


def _read_schema() -> str:
    try:
        return SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(
            "rmqna-data: the schema document could not be read",
            expected="the agent's committed prompts/schema_v1.md",
            actual=f"{type(error).__name__} for {SCHEMA_PATH}",
            fix="run the server from a complete workspace checkout",
        ) from error


def _log_call(tool: str, started: float, **fields: object) -> None:
    """Write the one line a call leaves: its tool, duration, counts and error kind.

    Never the SQL, a search text or a value: the statement is in the agent's trace, and a server
    log is kept where the data is not.
    """
    _LOG.info(
        "tool_result",
        extra={
            "tool": tool,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            **fields,
        },
    )

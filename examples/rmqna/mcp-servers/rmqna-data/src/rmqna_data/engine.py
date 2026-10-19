"""The contained SQL engine: DuckDB over the workspace's CSV files, behind the validator.

The load follows ``ai_agent_lib_core.adapters.data_duckdb`` step for step: each content CSV is read
with a plain ``read_csv`` into an in-memory DuckDB table named after its file (lower case, letters,
digits and underscores), and then the engine is forbidden from touching the file system or the
network, so nothing a query says can read anything but those tables. The schema document the agent
reasons over is rendered from the same load, so its column types are the types a query sees.

What this module adds to that discipline, because the SQL here is written by a model:

* every statement goes through :func:`rmqna_data.validate.validate_sql` first, and only the
  rewritten statement is run;
* the configuration is locked after loading, so nothing can turn external access back on;
* each call runs on its own cursor, in a worker thread, under a timeout that interrupts the
  query rather than abandoning it;
* a result is cut at the row cap; asking DuckDB for one row more than the cap is how the cut is
  known, and the extra row is never returned;
* an error is a value, not a crash: a refusal or a DuckDB error is raised as
  :class:`~rmqna_data.validate.SqlRefused`, which the server returns as a normal tool result.

The dictionary (``data_model.csv``) and rmprep's answer key (``rm_scenarios.csv``) are metadata, not
data: they are never loaded, wherever they happen to sit.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import duckdb
from ai_agent_lib_core.contracts import ConfigurationError, describe

from rmqna_data.validate import SqlRefused, close_matches, validate_sql

__all__ = [
    "DEFAULT_ROW_CAP",
    "DEFAULT_TIMEOUT_SECONDS",
    "NOT_QUERYABLE",
    "VALUES_CAP",
    "ExecuteResult",
    "RmqnaEngine",
    "ValuesResult",
]

DEFAULT_ROW_CAP: Final = 200
"""The most rows a result carries."""

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
"""How long one statement may run before it is interrupted."""

VALUES_CAP: Final = 25
"""The most values a lookup returns."""

NOT_QUERYABLE: Final = frozenset({"data_model", "rm_scenarios"})
"""Files that are metadata or an answer key, never tables."""

_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")  # the pattern data_duckdb.py applies


@dataclass(frozen=True, slots=True)
class ExecuteResult:
    """What a statement returned, ready to be sent as JSON.

    Attributes:
        columns: The column names, as DuckDB named them.
        rows: At most the row cap of rows, each a list of JSON-safe values.
        row_count: The number of rows in ``rows``.
        truncated: Whether the statement produced more rows than the cap, so ``rows`` is a prefix.
        elapsed_ms: How long the engine took to run the statement and fetch its rows.
    """

    columns: list[str]
    rows: list[list[object]]
    row_count: int
    truncated: bool
    elapsed_ms: int

    def to_payload(self) -> dict[str, Any]:
        """Return the tool's result payload."""
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class ValuesResult:
    """The distinct values of a column that match a search.

    Attributes:
        values: Up to :data:`VALUES_CAP` matching values, as text, in order.
        truncated: Whether there were more matches than were returned.
    """

    values: list[str]
    truncated: bool

    def to_payload(self) -> dict[str, Any]:
        """Return the tool's result payload."""
        return {"values": self.values, "truncated": self.truncated}


class RmqnaEngine:
    """Runs validated, read-only SQL over the CSV files of one folder.

    The tables are loaded when the engine is built, so a folder that cannot be loaded stops the
    server at startup rather than at the first question.

    Args:
        data_dir: The folder of CSV files. Each content file becomes a table.
        row_cap: The most rows a result carries.
        timeout_seconds: How long one statement may run.

    Raises:
        ConfigurationError: If the folder is missing, or a file is not a valid table.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if row_cap < 1:
            raise ValueError("row_cap must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._row_cap = row_cap
        self._timeout = timeout_seconds
        self._connection, self._column_types = _load(data_dir)

    @property
    def tables(self) -> frozenset[str]:
        """The loaded tables: the only names a statement may read."""
        return frozenset(self._column_types)

    @property
    def column_types(self) -> Mapping[str, Mapping[str, str]]:
        """Each table's columns, in file order, with the DuckDB type each was loaded as."""
        return self._column_types

    async def execute(self, sql: str) -> ExecuteResult:
        """Validate ``sql`` and run it.

        Raises:
            SqlRefused: With ``error_kind`` ``not_a_select``, ``multiple_statements``,
                ``set_operation``, ``unknown_table``, ``forbidden_function`` or ``parse_error``
                if the validator refuses the statement; ``execution_error`` with DuckDB's own
                message if the engine fails on it; ``timeout`` if it ran too long.
        """
        rewritten = validate_sql(sql, self.tables, self._row_cap)
        started = time.perf_counter()
        columns, rows = await self._run(rewritten, None, self._row_cap + 1)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        kept = rows[: self._row_cap]
        return ExecuteResult(
            columns=columns,
            rows=[[_json_safe(cell) for cell in row] for row in kept],
            row_count=len(kept),
            truncated=len(rows) > self._row_cap,
            elapsed_ms=elapsed_ms,
        )

    async def values(self, table: str, column: str, search: str) -> ValuesResult:
        """Return the distinct values of ``table.column`` that contain ``search``.

        The table and the column are looked up among the loaded ones, never written into SQL as
        given; the search is a bound parameter, matched case-insensitively as plain text (``%`` and
        ``_`` in it are not wildcards). A column that is not text is matched on its text form.

        Raises:
            SqlRefused: ``unknown_table`` if the table or the column is not loaded (the message
                says which); ``execution_error`` or ``timeout`` if the engine fails.
        """
        table_name = self._known_table(table)
        column_name = self._known_column(table_name, column)
        quoted = _quote(column_name)
        statement = (
            f"SELECT DISTINCT CAST({quoted} AS VARCHAR) AS v FROM {_quote(table_name)} "
            f"WHERE CAST({quoted} AS VARCHAR) ILIKE ? ESCAPE '\\' ORDER BY v LIMIT {VALUES_CAP + 1}"
        )
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", search.strip()) + "%"
        _, rows = await self._run(statement, [pattern], VALUES_CAP + 1)
        return ValuesResult(
            values=[str(row[0]) for row in rows[:VALUES_CAP]],
            truncated=len(rows) > VALUES_CAP,
        )

    def close(self) -> None:
        """Close the engine. Safe to call more than once."""
        with contextlib.suppress(duckdb.Error):
            self._connection.close()

    def _known_table(self, table: str) -> str:
        name = table.strip().lower()
        if name not in self._column_types:
            close = close_matches(name, self._column_types)
            hint = f" Did you mean {', '.join(repr(c) for c in close)}?" if close else ""
            raise SqlRefused(
                "unknown_table",
                f"Unknown table '{table}'.{hint} Use a table listed in the schema document.",
            )
        return name

    def _known_column(self, table: str, column: str) -> str:
        columns = {name.lower(): name for name in self._column_types[table]}
        found = columns.get(column.strip().lower())
        if found is None:
            close = close_matches(column.strip().lower(), columns)
            hint = f" Did you mean {', '.join(repr(columns[c]) for c in close)}?" if close else ""
            raise SqlRefused(
                "unknown_table",
                f"Table '{table}' has no column '{column}'.{hint} "
                "Use a column listed for it in the schema document.",
            )
        return found

    async def _run(
        self, sql: str, parameters: Sequence[object] | None, count: int
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        # A cursor is its own connection to the same database, so calls can overlap.
        cursor = self._connection.cursor()
        work = asyncio.to_thread(_execute_on, cursor, sql, parameters, count)
        try:
            return await asyncio.wait_for(work, self._timeout)
        except TimeoutError:
            _interrupt(cursor)
            raise SqlRefused(
                "timeout",
                f"The query ran longer than {self._timeout:g} seconds and was stopped. "
                "Filter earlier, join on keys, or aggregate before joining.",
            ) from None
        except asyncio.CancelledError:
            _interrupt(cursor)
            raise


def _execute_on(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: Sequence[object] | None,
    count: int,
) -> tuple[list[str], list[tuple[object, ...]]]:
    try:
        statement = _the_one_select(cursor, sql)
        cursor.execute(statement, list(parameters) if parameters is not None else None)
        columns = [str(column[0]) for column in cursor.description or ()]
        return columns, cursor.fetchmany(count)
    except duckdb.Error as error:
        # DuckDB's own text: it names the candidates for a misspelt column or function, which is
        # what the model needs to repair the statement. It goes to the caller, never to a log.
        raise SqlRefused("execution_error", str(error)) from error
    except (OverflowError, ValueError, UnicodeError) as error:
        raise SqlRefused(
            "execution_error", f"A value in the result could not be read: {error}"
        ) from error
    finally:
        _close(cursor)


def _the_one_select(cursor: duckdb.DuckDBPyConnection, sql: str) -> duckdb.Statement:
    """Return ``sql`` as the one SELECT DuckDB itself reads it to be, or refuse.

    The validator reasons with sqlglot's parser; the engine runs DuckDB's. This asks DuckDB to
    parse the text (nothing is run) and insists on exactly one statement of kind SELECT. If the
    two parsers ever disagreed about a text, the disagreement stops here, and what is executed is
    the statement DuckDB parsed, not the text.
    """
    statements = cursor.extract_statements(sql)
    if len(statements) != 1:
        raise SqlRefused(
            "multiple_statements",
            f"Send one statement per call; the engine reads this input as {len(statements)}.",
        )
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SqlRefused(
            "not_a_select",
            f"The engine reads this as a {statements[0].type.name} statement, not a SELECT: "
            "this tool runs one read-only SELECT.",
        )
    return statements[0]


def _close(cursor: duckdb.DuckDBPyConnection) -> None:
    # The cursor may already be closed if the statement was interrupted at the same moment.
    with contextlib.suppress(Exception):
        cursor.close()


def _interrupt(cursor: duckdb.DuckDBPyConnection) -> None:
    with contextlib.suppress(Exception):
        cursor.interrupt()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _load(
    data_dir: Path,
) -> tuple[duckdb.DuckDBPyConnection, dict[str, dict[str, str]]]:
    """Load every content CSV of ``data_dir`` into DuckDB, then lock the engine down."""
    if not data_dir.is_dir():
        raise ConfigurationError(
            "rmqna-data: the data folder does not exist",
            expected="a folder of .csv files, one per table",
            actual=f"nothing at {data_dir}",
            fix="run the server from a workspace whose data/ folder holds the CSV files",
        )
    connection = duckdb.connect(":memory:")
    try:
        for path in sorted(data_dir.glob("*.csv")):
            table = path.stem.lower()
            if table in NOT_QUERYABLE:
                continue
            if not _TABLE_NAME.match(table):
                raise ConfigurationError(
                    f"rmqna-data: {path.name} cannot be a table name; "
                    "use lower-case letters, digits and underscores"
                )
            try:
                connection.read_csv(str(path)).create(table)
            except duckdb.Error as exc:
                raise ConfigurationError(
                    f"rmqna-data: {path.name} could not be loaded",
                    expected="a CSV file with a header row",
                    actual=describe(exc),
                ) from exc
        column_types: dict[str, dict[str, str]] = {}
        for table, column, kind in connection.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
        ).fetchall():
            column_types.setdefault(str(table), {})[str(column)] = str(kind)
        # From here on the engine can read only what was loaded above, and nothing can say
        # otherwise: the configuration is locked, so a statement cannot re-enable file access.
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
    except BaseException:
        connection.close()
        raise
    return connection, column_types


def _json_safe(value: object) -> object:
    """Return ``value`` as something JSON can carry, losing nothing a reader needs."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, datetime | date | clock_time):
        return value.isoformat()
    if isinstance(value, timedelta | UUID):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)

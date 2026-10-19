"""The contained SQL engine: DuckDB over the workspace's CSV files, behind the validator.

The load follows ``ai_agent_lib_core.adapters.data_duckdb`` step for step: each content CSV is read
with a plain ``read_csv`` into an in-memory DuckDB table named after its file (lower case, letters,
digits and underscores), and then the engine is forbidden from reaching the file system or the
network and from spilling to disk, so nothing a query says can read anything but those tables or
leave a file behind. The schema document the agent
reasons over is rendered from the same load, so its column types are the types a query sees.

What this module adds to that discipline, because the SQL here is written by a model:

* every statement goes through :func:`rmqna_data.validate.validate_sql` first, and only the
  rewritten statement is run;
* the configuration is locked after loading, so nothing can turn external access back on; DuckDB
  is given a memory limit (which it does not always honour) and no temp directory, so a statement
  that does not fit in memory fails instead of spilling to disk;
* each call runs on its own cursor, in a worker thread, under a timeout that interrupts the
  query. Interrupting does not reach everything: DuckDB's binder and optimizer, and a single huge
  scalar, run to completion. :attr:`RmqnaEngine.running` counts the workers still going, including
  any whose caller has already been told "timeout";
* a result is cut at the row cap; asking DuckDB for one row more than the cap is how the cut is
  known, and the extra row is never returned. A result is also refused if it would serialize to
  more than a byte ceiling, which a single value holding a whole table could otherwise reach in one
  row. The conversion runs in the worker, inside the timeout;
* an error is a value, not a crash: a refusal or a DuckDB error is raised as
  :class:`~rmqna_data.validate.SqlRefused`, which the server returns as a normal tool result.

The dictionary (``data_model.csv``) and rmprep's answer key (``rm_scenarios.csv``) are metadata, not
data: they are never loaded, wherever they happen to sit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    "DEFAULT_MAX_RESULT_BYTES",
    "DEFAULT_MEMORY_LIMIT",
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

DEFAULT_MAX_RESULT_BYTES: Final = 2 * 1024 * 1024
"""The most a result may weigh once serialized. The row cap alone does not bound it: one row can
hold a whole table as a list."""

DEFAULT_MEMORY_LIMIT: Final = "1GB"
"""DuckDB's memory limit. A backstop only: DuckDB does not account for every allocation (measured:
``repeat('a', 3e9)`` and ``unnest(range(0, 3e9))`` pass 5 GB under it), so the validator refuses
those and the ADR says what remains."""

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
        max_result_bytes: The most a result may weigh once serialized.
        memory_limit: DuckDB's memory limit, for example ``"1GB"``.

    Raises:
        ConfigurationError: If the folder is missing, or a file is not a valid table.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        row_cap: int = DEFAULT_ROW_CAP,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
    ) -> None:
        if row_cap < 1:
            raise ValueError("row_cap must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_result_bytes < 1:
            raise ValueError("max_result_bytes must be at least 1")
        self._row_cap = row_cap
        self._timeout = timeout_seconds
        self._max_result_bytes = max_result_bytes
        self._executing: set[duckdb.DuckDBPyConnection] = set()
        self._executing_lock = threading.Lock()
        self._connection, self._column_types = _load(data_dir, memory_limit)

    @property
    def tables(self) -> frozenset[str]:
        """The loaded tables: the only names a statement may read."""
        return frozenset(self._column_types)

    @property
    def column_types(self) -> Mapping[str, Mapping[str, str]]:
        """Each table's columns, in file order, with the DuckDB type each was loaded as."""
        return self._column_types

    @property
    def running(self) -> int:
        """How many statements are still executing in a worker thread.

        A call that timed out or was cancelled has been interrupted, but DuckDB does not stop
        everywhere it is interrupted (its binder and optimizer, and one huge scalar, run to the
        end), so the worker of an abandoned call can outlive the call. This counts them.
        """
        with self._executing_lock:
            return len(self._executing)

    async def execute(self, sql: str) -> ExecuteResult:
        """Validate ``sql`` and run it.

        Raises:
            SqlRefused: With ``error_kind`` ``not_a_select``, ``multiple_statements``,
                ``set_operation``, ``unknown_table``, ``forbidden_function`` or ``parse_error``
                if the validator refuses the statement; ``execution_error`` with DuckDB's own
                message if the engine fails on it or the result is too large to return;
                ``timeout`` if it ran too long.
        """
        rewritten = validate_sql(sql, self.tables, self._row_cap)
        started = time.perf_counter()
        fetched = await self._run(rewritten, None, self._row_cap)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return ExecuteResult(
            columns=fetched.columns,
            rows=fetched.rows,
            row_count=len(fetched.rows),
            truncated=fetched.fetched > self._row_cap,
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
        fetched = await self._run(statement, [pattern], VALUES_CAP)
        return ValuesResult(
            values=[str(row[0]) for row in fetched.rows],
            truncated=fetched.fetched > VALUES_CAP,
        )

    def close(self) -> None:
        """Interrupt anything still executing, then close the engine. Safe to call more than once."""
        with self._executing_lock:
            cursors = list(self._executing)
        for cursor in cursors:
            with contextlib.suppress(Exception):
                cursor.interrupt()
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

    async def _run(self, sql: str, parameters: Sequence[object] | None, keep: int) -> _Fetched:
        """Run ``sql`` on its own cursor in a worker thread, under the timeout."""
        # A cursor is its own connection to the same database, so calls can overlap.
        cursor = self._connection.cursor()

        def work() -> _Fetched:
            with self._tracking(cursor):
                return _execute_on(cursor, sql, parameters, keep, self._max_result_bytes)

        try:
            return await asyncio.wait_for(asyncio.to_thread(work), self._timeout)
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

    @contextmanager
    def _tracking(self, cursor: duckdb.DuckDBPyConnection) -> Iterator[None]:
        with self._executing_lock:
            self._executing.add(cursor)
        try:
            yield
        finally:
            with self._executing_lock:
                self._executing.discard(cursor)


@dataclass(frozen=True, slots=True)
class _Fetched:
    """What a worker brings back: JSON-safe rows, at most the ones to keep."""

    columns: list[str]
    rows: list[list[object]]
    fetched: int
    """How many rows DuckDB returned: one more than ``rows`` when the result was cut."""


class _TooLarge(Exception):
    """The result would serialize to more than the byte ceiling."""


class _Budget:
    """A running estimate of a result's serialized size, spent as the result is converted."""

    def __init__(self, ceiling: int) -> None:
        self.ceiling = ceiling
        self.left = ceiling

    def spend(self, size: int) -> None:
        self.left -= size
        if self.left < 0:
            raise _TooLarge


def _execute_on(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: Sequence[object] | None,
    keep: int,
    max_bytes: int,
) -> _Fetched:
    """Run one statement and convert its first ``keep`` rows; runs in a worker thread."""
    try:
        statement = _the_one_select(cursor, sql)
        cursor.execute(statement, list(parameters) if parameters is not None else None)
        columns = [str(column[0]) for column in cursor.description or ()]
        fetched = cursor.fetchmany(keep + 1)
        budget = _Budget(max_bytes)
        budget.spend(sum(len(column) + 3 for column in columns))
        rows = [[_json_safe(cell, budget) for cell in row] for row in fetched[:keep]]
        return _Fetched(columns, rows, len(fetched))
    except duckdb.Error as error:
        # DuckDB's own text: it names the candidates for a misspelt column or function, which is
        # what the model needs to repair the statement. It goes to the caller, never to a log.
        raise SqlRefused("execution_error", str(error)) from error
    except _TooLarge:
        raise SqlRefused(
            "execution_error",
            f"The result is larger than {max_bytes / 2**20:g} MB once serialized. Narrow the "
            "selection: fewer columns, a filter, an aggregate or a smaller LIMIT. Collecting a "
            "whole table into one value (list, array_agg, string_agg, a struct per row) is too "
            "large too.",
        ) from None
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


_MEMORY_LIMIT = re.compile(r"^[0-9]+(\.[0-9]+)? ?[A-Za-z]+$")


def _load(
    data_dir: Path, memory_limit: str
) -> tuple[duckdb.DuckDBPyConnection, dict[str, dict[str, str]]]:
    """Load every content CSV of ``data_dir`` into DuckDB, then lock the engine down."""
    if not _MEMORY_LIMIT.match(memory_limit):
        raise ValueError(f"memory_limit {memory_limit!r} is not a size such as '1GB'")
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
        connection.execute(f"SET memory_limit = '{memory_limit}'")
        # No spilling to disk. DuckDB writes what does not fit in memory to a .tmp folder in the
        # working directory, bounded only by the free space (a window function over a cross join
        # wrote 3.6 GB in 8 seconds). With no temp directory such a statement fails at the memory
        # limit with a clean Out of Memory Error, and the engine writes no file at all. This is set
        # before external access is turned off, which forbids changing the temp directory.
        connection.execute("SET temp_directory = ''")
        # A time zone with an offset (now(), a TIMESTAMPTZ) is shown in the session's zone, which
        # DuckDB takes from the machine. UTC makes an answer the same wherever the server runs.
        connection.execute("SET GLOBAL TimeZone = 'UTC'")
        # From here on the engine can read only what was loaded above, and nothing can say
        # otherwise: the configuration is locked, so a statement cannot re-enable file access.
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
    except BaseException:
        connection.close()
        raise
    return connection, column_types


def _json_safe(value: object, budget: _Budget) -> object:
    """Return ``value`` as something JSON can carry, spending its serialized size from ``budget``.

    The size is what the value weighs as compact JSON with every non-ASCII and control character
    escaped (a control character is six bytes, ``\\u0001``): the most it can weigh on the wire,
    whichever encoder sends it. Each value also pays a byte for the comma after it.
    """
    if value is None:
        budget.spend(5)
        return None
    if isinstance(value, bool):
        budget.spend(6)
        return value
    if isinstance(value, int):
        budget.spend(len(str(value)) + 1)
        return value
    if isinstance(value, str):
        budget.spend(_string_size(value, budget) + 1)
        return value
    if isinstance(value, float):
        return _number(value, budget)
    if isinstance(value, Decimal):
        return _number(float(value), budget) if value.is_finite() else _number(math.nan, budget)
    if isinstance(value, datetime | date | clock_time):
        text = value.isoformat()
        budget.spend(_string_size(text, budget) + 1)
        return text
    if isinstance(value, timedelta | UUID):
        text = str(value)
        budget.spend(_string_size(text, budget) + 1)
        return text
    if isinstance(value, bytes | bytearray):
        budget.spend(2 * len(value) + 3)
        return bytes(value).hex()
    if isinstance(value, list | tuple):
        budget.spend(2)
        return [_json_safe(item, budget) for item in value]
    if isinstance(value, dict):
        budget.spend(2)
        converted: dict[str, object] = {}
        for key, item in value.items():
            budget.spend(_string_size(str(key), budget) + 1)
            converted[str(key)] = _json_safe(item, budget)
        return converted
    text = str(value)
    budget.spend(_string_size(text, budget) + 1)
    return text


def _number(value: float, budget: _Budget) -> object:
    """Return a float as JSON carries it; NaN and the infinities, which it cannot, as text."""
    if math.isfinite(value):
        budget.spend(len(repr(value)) + 1)
        return value
    text = "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    budget.spend(_string_size(text, budget) + 1)
    return text


def _string_size(text: str, budget: _Budget) -> int:
    """Return the bytes ``text`` takes as a JSON string, quotes and escapes included."""
    if len(text) + 2 > budget.left:
        raise _TooLarge  # it cannot fit, so it is not worth escaping to find out by how much
    return len(json.dumps(text))

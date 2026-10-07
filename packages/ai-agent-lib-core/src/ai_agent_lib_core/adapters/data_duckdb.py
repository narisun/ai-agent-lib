"""A data source over CSV files, for local development.

Each ``*.csv`` file in the data directory becomes a table named after the
file. The named queries are the same SQL files the production database runs,
written in its dialect and transpiled for DuckDB, so a developer exercises the
real queries without a database to connect to.

The tables are loaded once, when the adapter starts. After loading, the engine
is forbidden from touching the file system or the network, so a query can only
read what was loaded.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from ai_agent_lib_core.adapters.sql import CompiledQuery, QueryCatalog, compile_query, finish_result
from ai_agent_lib_core.contracts import (
    AgentLibError,
    Clock,
    ConfigurationError,
    Obligations,
    OptionsModel,
    ParameterType,
    QueryDescription,
    QueryResult,
    SourceMetadata,
    TransientError,
)

if TYPE_CHECKING:
    import duckdb

__all__ = ["DuckDbCsvDataSource", "DuckDbCsvOptions"]

_ENGINE_DIALECT = "duckdb"
_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# One value of each declared type, used to check a query without running it.
_SAMPLE_VALUES: Mapping[ParameterType, object] = {
    ParameterType.STRING: "",
    ParameterType.INTEGER: 0,
    ParameterType.NUMBER: Decimal(0),
    ParameterType.BOOLEAN: False,
    ParameterType.DATE: date(2000, 1, 1),
    ParameterType.TIMESTAMP: datetime(2000, 1, 1),  # noqa: DTZ001 - a typed placeholder
}


class DuckDbCsvOptions(OptionsModel):
    """Options of the ``duckdb_csv`` data source.

    Attributes:
        data_dir: Directory of CSV files. Each file becomes a table.
        queries_dir: Directory of named query files.
        dialect: The dialect the queries are written in.
        timeout_seconds: How long one query may run.
    """

    data_dir: Path
    queries_dir: Path
    dialect: str = "redshift"
    timeout_seconds: float = Field(default=30.0, gt=0)


class DuckDbCsvDataSource:
    """Runs named queries over CSV files with an in-process DuckDB engine.

    Args:
        name: The name of this data source.
        options: Where the files are.
        clock: Stamps each result with the time it was fetched.
    """

    def __init__(self, name: str, options: DuckDbCsvOptions, clock: Clock) -> None:
        self._name = name
        self._options = options
        self._clock = clock
        self._catalog: QueryCatalog | None = None
        self._connection: duckdb.DuckDBPyConnection | None = None

    async def start(self) -> None:
        """Load the queries and the CSV files, then check every query.

        Raises:
            ConfigurationError: If the engine is not installed, a directory is
                missing, or a file or a query is invalid.
        """
        if self._connection is not None:
            raise RuntimeError("the data source is already started")
        if importlib.util.find_spec("duckdb") is None:
            raise ConfigurationError(
                "the duckdb_csv data source needs DuckDB; install it with "
                "'pip install \"ai-agent-lib-core[duckdb]\"'"
            )
        catalog = QueryCatalog.load(self._options.queries_dir, dialect=self._options.dialect)
        connection = await asyncio.to_thread(self._load, catalog)
        self._catalog, self._connection = catalog, connection

    def _load(self, catalog: QueryCatalog) -> duckdb.DuckDBPyConnection:
        import duckdb

        data_dir = self._options.data_dir
        if not data_dir.is_dir():
            raise ConfigurationError(f"data source {self._name!r}: data directory not found")
        connection = duckdb.connect(":memory:")
        try:
            for path in sorted(data_dir.glob("*.csv")):
                table = path.stem.lower()
                if not _TABLE_NAME.match(table):
                    raise ConfigurationError(
                        f"data source {self._name!r}: {path.name} cannot be a table name; "
                        "use lower-case letters, digits and underscores"
                    )
                try:
                    connection.read_csv(str(path)).create(table)
                except duckdb.Error as exc:
                    raise ConfigurationError(
                        f"data source {self._name!r}: {path.name} could not be loaded"
                    ) from exc
            # From here on the engine can read only what was loaded above.
            connection.execute("SET enable_external_access = false")
            for name, description in catalog.describe().items():
                compiled = compile_query(catalog.get(name), None, target_dialect=_ENGINE_DIALECT)
                samples = {p.name: _SAMPLE_VALUES[p.type] for p in description.parameters}
                try:
                    connection.execute(f"EXPLAIN {compiled.sql}", samples)
                except duckdb.Error as exc:
                    raise ConfigurationError(
                        f"data source {self._name!r}: query {name!r} does not fit the loaded "
                        f"tables ({type(exc).__name__})"
                    ) from exc
        except BaseException:
            connection.close()
            raise
        return connection

    def describe(self) -> Mapping[str, QueryDescription]:
        """Return the queries this source offers, by name."""
        return self._started()[0].describe()

    async def query(
        self,
        name: str,
        parameters: Mapping[str, object] | None = None,
        *,
        obligations: Obligations | None = None,
    ) -> QueryResult:
        """Run the query called ``name``.

        Raises:
            ValidationFailed: If the query is unknown or a parameter is invalid.
            PolicyDenied: If an obligation cannot be enforced.
            TransientError: If the query ran longer than the timeout.
            AgentLibError: If the engine rejected the query.
        """
        catalog, connection = self._started()
        bound = catalog.bind(name, parameters)
        compiled = compile_query(catalog.get(name), obligations, target_dialect=_ENGINE_DIALECT)
        # A cursor is its own connection to the same database, so calls can overlap.
        cursor = connection.cursor()
        work = asyncio.to_thread(
            self._run, cursor, name, compiled, {**bound, **compiled.parameters}
        )
        try:
            columns, rows = await asyncio.wait_for(work, self._options.timeout_seconds)
        except TimeoutError:
            self._interrupt(cursor)
            raise TransientError(
                f"query {name!r} on data source {self._name!r} ran longer than "
                f"{self._options.timeout_seconds:g} seconds"
            ) from None
        except asyncio.CancelledError:
            self._interrupt(cursor)
            raise
        source = SourceMetadata(
            source=self._name,
            retrieved_at=self._clock.now(),
            uri=f"datasource://{self._name}/{name}",
        )
        return finish_result(columns, rows, compiled, obligations, source)

    def _run(
        self,
        cursor: duckdb.DuckDBPyConnection,
        name: str,
        compiled: CompiledQuery,
        parameters: Mapping[str, object],
    ) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
        import duckdb

        try:
            cursor.execute(compiled.sql, dict(parameters))
            columns = tuple(str(column[0]).lower() for column in cursor.description or ())
            return columns, cursor.fetchmany(compiled.row_cap + 1)
        except duckdb.Error as exc:
            # The engine's message can repeat parameter values, so it stays in the cause.
            raise AgentLibError(
                f"query {name!r} failed on data source {self._name!r} ({type(exc).__name__})"
            ) from exc
        finally:
            cursor.close()

    @staticmethod
    def _interrupt(cursor: duckdb.DuckDBPyConnection) -> None:
        # The cursor may already be closed if the query ended at the same moment.
        with contextlib.suppress(Exception):
            cursor.interrupt()

    async def validate(self) -> None:
        """Check that the source is started and offers at least one query.

        Raises:
            ConfigurationError: If it is not ready.
        """
        if self._connection is None or self._catalog is None:
            raise ConfigurationError(f"data source {self._name!r} is not started")
        if not self._catalog.describe():
            raise ConfigurationError(f"data source {self._name!r} has no queries")

    async def aclose(self) -> None:
        """Close the engine. Safe to call more than once."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _started(self) -> tuple[QueryCatalog, duckdb.DuckDBPyConnection]:
        if self._catalog is None or self._connection is None:
            raise RuntimeError(f"data source {self._name!r} is not started")
        return self._catalog, self._connection

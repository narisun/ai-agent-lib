"""A data source that runs named queries on Amazon Redshift through the Data API.

The Data API is an HTTPS service, so the server needs no database driver, no
connection pool and no network path to the cluster. The server's own IAM role
is the database identity: one role per server, with access to exactly the
tables its queries read.

The queries are the same SQL files a developer runs over CSV files with the
``duckdb_csv`` data source.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Self

from botocore import exceptions as aws
from pydantic import Field, model_validator
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.generator import Generator

from ai_agent_lib_aws.redshift_target import RedshiftTarget, target_problem
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import (
    AgentLibError,
    Clock,
    ConfigurationError,
    Obligations,
    OptionsModel,
    QueryDescription,
    QueryResult,
    SourceMetadata,
    TransientError,
)
from ai_agent_lib_core.kit import (
    CompiledQuery,
    GovernedStatement,
    QueryCatalog,
    build_statement,
    finish_result,
)

__all__ = ["RedshiftDataOptions", "RedshiftDataSource"]

_WHAT = "the redshift_data data source"
_DIALECT = "redshift"
_CHECK = "connection_check"

_FINISHED, _FAILED, _ABORTED = "FINISHED", "FAILED", "ABORTED"
_POLL_FIRST, _POLL_GROWTH, _POLL_LONGEST = 0.1, 1.5, 2.0

_DECIMAL_TYPES = frozenset({"numeric", "decimal"})
_DATE_TYPES = frozenset({"date"})
_TIMESTAMP_TYPES = frozenset({"timestamp", "timestamptz"})
_FIELD_KEYS = ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue")

Sleep = Callable[[float], Awaitable[None]]


class RedshiftDataOptions(OptionsModel):
    """Options of the ``redshift_data`` data source.

    Name either a serverless workgroup or a provisioned cluster. With a
    workgroup the server's IAM role is the database user and nothing else is
    needed. A cluster also needs ``db_user`` or ``secret_arn``.

    Attributes:
        queries_dir: Directory of named query files.
        database: The database the queries run in.
        workgroup: The name of a Redshift Serverless workgroup.
        cluster_id: The identifier of a provisioned cluster.
        db_user: The database user to run as, on a provisioned cluster.
        secret_arn: A Secrets Manager secret that holds database credentials.
        timeout_seconds: How long one query may take, waiting included.
    """

    queries_dir: Path
    database: str = Field(min_length=1)
    workgroup: str | None = Field(default=None, min_length=1)
    cluster_id: str | None = Field(default=None, min_length=1)
    db_user: str | None = Field(default=None, min_length=1)
    secret_arn: str | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _one_target_and_one_identity(self) -> Self:
        problem = target_problem(
            workgroup=self.workgroup,
            cluster_id=self.cluster_id,
            db_user=self.db_user,
            secret_arn=self.secret_arn,
        )
        if problem is not None:
            raise ValueError(problem)
        return self

    def target(self) -> RedshiftTarget:
        """Return the database these options name, as a Data API call names it."""
        return RedshiftTarget(
            database=self.database,
            workgroup=self.workgroup,
            cluster_id=self.cluster_id,
            db_user=self.db_user,
            secret_arn=self.secret_arn,
        )


def _placeholder(generator: Generator, expression: exp.Placeholder) -> str:
    del generator
    return f":{expression.name}"


def _data_api_generator() -> type[Generator]:
    """Return a writer of Redshift SQL that writes each parameter as ``:name``.

    That is the form the Data API binds. The dialect's own writer uses the
    form of a database driver, which the Data API would send as plain text.
    """
    base = Dialect.get_or_raise(_DIALECT).generator_class
    transforms = {**base.TRANSFORMS, exp.Placeholder: _placeholder}
    return type("DataApiGenerator", (base,), {"TRANSFORMS": transforms})


_GENERATOR = _data_api_generator()


@dataclass(frozen=True, slots=True)
class _Request:
    """One statement as the Data API takes it."""

    sql: str
    parameters: tuple[Mapping[str, str], ...]


def _typed(value: object) -> tuple[str, str]:
    """Return the SQL type to cast a bound value to, and the value as text.

    The Data API sends every parameter as text, so each one is cast to the
    type of its value. Without the cast the database would have to guess.
    """
    if isinstance(value, bool):
        return "BOOLEAN", "true" if value else "false"
    if isinstance(value, int):
        return "BIGINT", str(value)
    if isinstance(value, float):
        return "DOUBLE PRECISION", repr(value)
    if isinstance(value, Decimal):
        return "DECIMAL(38, 10)", format(value, "f")
    if isinstance(value, datetime):
        kind = "TIMESTAMP" if value.tzinfo is None else "TIMESTAMPTZ"
        return kind, value.isoformat(sep=" ")
    if isinstance(value, date):
        return "DATE", value.isoformat()
    if isinstance(value, str):
        return "VARCHAR", value
    raise TypeError(f"a {type(value).__name__} cannot be bound to a query")


def _request(statement: GovernedStatement, values: Mapping[str, object]) -> _Request:
    """Write ``statement`` for the Data API, with every parameter typed.

    Parameters are renamed ``p1``, ``p2`` and so on, so a name can never
    collide and never depends on what the service accepts in a name.
    """
    names = {name: f"p{number}" for number, name in enumerate(sorted(values), start=1)}
    parameters: dict[str, str] = {}

    def bind(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Placeholder):
            return node
        value = values[node.name]
        if value is None:
            return exp.null()
        kind, text = _typed(value)
        if not text:
            # The service refuses an empty parameter value; an empty string is a constant.
            return exp.Literal.string("")
        parameters[names[node.name]] = text
        return exp.cast(
            exp.Placeholder(this=names[node.name]), exp.DataType.build(kind, dialect=_DIALECT)
        )

    sql = _GENERATOR(dialect=_DIALECT).generate(statement.expression.transform(bind))
    return _Request(
        sql=sql,
        parameters=tuple({"name": name, "value": parameters[name]} for name in sorted(parameters)),
    )


def _convert(field: Mapping[str, Any], type_name: str) -> object:
    """Return one field of a result as a Python value."""
    if field.get("isNull"):
        return None
    raw = next((field[key] for key in _FIELD_KEYS if key in field), None)
    if not isinstance(raw, str):
        return raw
    try:
        if type_name in _DECIMAL_TYPES:
            return Decimal(raw)
        if type_name in _DATE_TYPES:
            return date.fromisoformat(raw)
        if type_name in _TIMESTAMP_TYPES:
            return datetime.fromisoformat(raw)
    except (ValueError, InvalidOperation):
        return raw
    return raw


def _error_code(error: BaseException) -> str:
    if isinstance(error, aws.ClientError):
        return str(error.response.get("Error", {}).get("Code") or type(error).__name__)
    return type(error).__name__


class RedshiftDataSource:
    """Runs named queries on Redshift through the Data API.

    A query is submitted, polled until it ends and then read page by page, all
    inside one deadline. Row filters and the row cap are part of the statement,
    so they run inside the database. Reading stops one row past the cap.

    Args:
        name: The name of this data source.
        options: Where the database is and where the queries are.
        sessions: Builds the client and makes its calls.
        clock: Stamps each result with the time it was fetched.
        client: A Redshift Data API client to use instead of building one.
        sleep: Waits between two status checks. A test replaces it.
    """

    def __init__(
        self,
        name: str,
        options: RedshiftDataOptions,
        sessions: AwsSessionFactory,
        clock: Clock,
        *,
        client: Any = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._name = name
        self._options = options
        self._target = options.target()
        self._sessions = sessions
        self._clock = clock
        self._client = client if client is not None else sessions.client("redshift-data")
        self._sleep = sleep
        self._catalog: QueryCatalog | None = None

    def __repr__(self) -> str:
        return f"RedshiftDataSource(name={self._name!r}, target={self._target.place!r})"

    async def start(self) -> None:
        """Load and check the query files. Nothing is sent to the database.

        Raises:
            ConfigurationError: If the directory is missing or a query is invalid.
        """
        if self._catalog is not None:
            raise RuntimeError("the data source is already started")
        self._catalog = await asyncio.to_thread(
            QueryCatalog.load, self._options.queries_dir, dialect=_DIALECT
        )

    def describe(self) -> Mapping[str, QueryDescription]:
        """Return the queries this source offers, by name."""
        return self._started().describe()

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
            TransientError: If the query ran longer than the timeout, was
                stopped, or AWS throttled the call.
            ConfigurationError: If AWS refused the server's access.
            AgentLibError: If the database rejected the query.
        """
        catalog = self._started()
        bound = catalog.bind(name, parameters)
        statement = build_statement(catalog.get(name), obligations)
        request = _request(statement, {**bound, **statement.parameters})
        columns, rows = await self._run(name, request, limit=statement.row_cap + 1)
        source = SourceMetadata(
            source=self._name,
            retrieved_at=self._clock.now(),
            uri=f"datasource://{self._name}/{name}",
        )
        compiled = CompiledQuery(
            sql=request.sql, parameters=statement.parameters, row_cap=statement.row_cap
        )
        return finish_result(columns, rows, compiled, obligations, source)

    async def validate(self) -> None:
        """Check that the source is started and that the database answers.

        One ``SELECT 1`` proves the workgroup or cluster, the database and the
        server's permission to run a statement.

        Raises:
            ConfigurationError: If it is not ready.
        """
        if self._catalog is None:
            raise ConfigurationError(f"data source {self._name!r} is not started")
        if not self._catalog.describe():
            raise ConfigurationError(f"data source {self._name!r} has no queries")
        try:
            await self._run(_CHECK, _Request(sql="SELECT 1 AS ready", parameters=()), limit=1)
        except ConfigurationError:
            raise
        except AgentLibError as exc:
            raise ConfigurationError(str(exc)) from None

    async def aclose(self) -> None:
        """Forget the loaded queries. Safe to call more than once."""
        self._catalog = None

    def _started(self) -> QueryCatalog:
        if self._catalog is None:
            raise RuntimeError(f"data source {self._name!r} is not started")
        return self._catalog

    async def _run(
        self, label: str, request: _Request, *, limit: int
    ) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
        submitted: list[str] = []
        try:
            async with asyncio.timeout(self._options.timeout_seconds):
                statement_id = await self._submit(label, request)
                submitted.append(statement_id)
                await self._wait(label, statement_id)
                return await self._fetch(label, statement_id, limit)
        except TimeoutError:
            await self._cancel(submitted)
            raise TransientError(
                f"query {label!r} on data source {self._name!r} took longer than "
                f"{self._options.timeout_seconds:g} seconds"
            ) from None
        except asyncio.CancelledError:
            await self._cancel(submitted)
            raise

    async def _submit(self, label: str, request: _Request) -> str:
        arguments: dict[str, Any] = {
            **self._target.arguments(),
            "Sql": request.sql,
            "StatementName": f"{self._name}.{label}"[:500],
        }
        if request.parameters:
            arguments["Parameters"] = [dict(parameter) for parameter in request.parameters]
        reply = await self._call(label, self._client.execute_statement, **arguments)
        statement_id = reply.get("Id")
        if not isinstance(statement_id, str) or not statement_id:
            raise AgentLibError(
                f"query {label!r} on data source {self._name!r} was not accepted by the database"
            )
        return statement_id

    async def _wait(self, label: str, statement_id: str) -> None:
        delay = _POLL_FIRST
        while True:
            reply = await self._call(label, self._client.describe_statement, Id=statement_id)
            status = reply.get("Status")
            if status == _FINISHED:
                return
            if status == _FAILED:
                # The database's message can repeat parameter values, so it is left out.
                raise AgentLibError(
                    f"query {label!r} failed on data source {self._name!r} "
                    f"(statement {statement_id})"
                )
            if status == _ABORTED:
                raise TransientError(
                    f"query {label!r} on data source {self._name!r} was stopped before it "
                    f"finished (statement {statement_id})"
                )
            await self._sleep(delay)
            delay = min(delay * _POLL_GROWTH, _POLL_LONGEST)

    async def _fetch(
        self, label: str, statement_id: str, limit: int
    ) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
        columns: tuple[str, ...] = ()
        types: tuple[str, ...] = ()
        rows: list[tuple[object, ...]] = []
        arguments: dict[str, Any] = {"Id": statement_id}
        while True:
            reply = await self._call(label, self._client.get_statement_result, **arguments)
            if not columns:
                described: Sequence[Mapping[str, Any]] = reply.get("ColumnMetadata") or ()
                columns = tuple(
                    str(column.get("label") or column.get("name") or "").lower()
                    for column in described
                )
                types = tuple(str(column.get("typeName") or "").lower() for column in described)
            for record in reply.get("Records") or ():
                rows.append(
                    tuple(_convert(field, kind) for field, kind in zip(record, types, strict=True))
                )
                if len(rows) >= limit:
                    return columns, rows
            token = reply.get("NextToken")
            if not token:
                return columns, rows
            arguments["NextToken"] = token

    async def _cancel(self, submitted: Sequence[str]) -> None:
        # Best effort: the statement may have ended, or the call may fail as well.
        for statement_id in submitted:
            with contextlib.suppress(Exception):
                await self._sessions.invoke(_WHAT, self._client.cancel_statement, Id=statement_id)

    async def _call(self, label: str, operation: Callable[..., Any], **arguments: Any) -> Any:
        try:
            return await self._sessions.invoke(_WHAT, operation, **arguments)
        except (aws.BotoCoreError, aws.ClientError) as exc:
            # The service's message can repeat the statement, so it stays in the cause.
            raise AgentLibError(
                f"query {label!r} failed on data source {self._name!r} ({_error_code(exc)})"
            ) from exc

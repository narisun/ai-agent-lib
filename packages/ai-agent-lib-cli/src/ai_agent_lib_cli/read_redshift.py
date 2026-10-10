"""Reading the Redshift catalogue: the tables of a schema and their columns.

Only the catalogue is read, never a row. So that a server over those tables
can run and be tested on a developer's machine, this module also writes
stand-in CSV files: the right columns, and two made-up rows of the right types.

The AWS package does the reading and is needed only for that.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.proposals import ColumnInfo, TableInfo, is_plain_name, stand_in
from ai_agent_lib_core.contracts import AgentLibError, ParameterType

__all__ = [
    "CatalogColumnLike",
    "CatalogReader",
    "CatalogTableLike",
    "RedshiftRequest",
    "fetch_catalog",
    "stand_in_csv",
    "tables_from_catalog",
]

_ROWS = 2
_TEXTS = frozenset({"varchar", "char", "bpchar", "nvarchar", "nchar", "text", "character", "name"})
_INTEGERS = frozenset({"int2", "int4", "int8", "smallint", "integer", "int", "bigint"})
_NUMBERS = frozenset(
    {"numeric", "decimal", "float4", "float8", "real", "float", "double precision"}
)
_TIMESTAMPS = frozenset({"timestamp", "timestamptz"})
_INSTALL = (
    "reading the Redshift catalogue needs the AWS package; install ai-agent-lib-aws where "
    "agentlib runs, in a workspace with: python -m pip install ai-agent-lib-aws"
)


CatalogReader = Callable[["RedshiftRequest"], Coroutine[Any, Any, Sequence["CatalogTableLike"]]]
"""Reads the tables a request names from the catalogue."""


class CatalogColumnLike(Protocol):
    """A column as the catalogue describes it."""

    @property
    def name(self) -> str:
        """The column's name."""

    @property
    def type_name(self) -> str:
        """The database's name for the column's type."""


class CatalogTableLike(Protocol):
    """A table as the catalogue describes it."""

    @property
    def name(self) -> str:
        """The table's name."""

    @property
    def columns(self) -> Sequence[CatalogColumnLike]:
        """The table's columns, in order."""


@dataclass(frozen=True, slots=True)
class RedshiftRequest:
    """What to read from the catalogue, and how to reach it.

    Attributes:
        schema: The schema whose tables are read.
        database: The database.
        workgroup: The name of a Redshift Serverless workgroup.
        cluster_id: The identifier of a provisioned cluster.
        db_user: The database user to read as, on a provisioned cluster.
        secret_arn: A Secrets Manager secret that holds database credentials.
        profile: The AWS profile to sign in with. By default the SDK's own choice.
        region: The AWS region. By default the profile's.
        tables: Read only these tables. By default all of the schema.
    """

    schema: str
    database: str
    workgroup: str | None = None
    cluster_id: str | None = None
    db_user: str | None = None
    secret_arn: str | None = None
    profile: str | None = None
    region: str | None = None
    tables: tuple[str, ...] = ()

    def deployed(self) -> dict[str, Any]:
        """Return the settings of the data source a deployed server reads these tables with."""
        optional = {
            "workgroup": self.workgroup,
            "cluster_id": self.cluster_id,
            "db_user": self.db_user,
            "secret_arn": self.secret_arn,
        }
        named = {key: value for key, value in optional.items() if value is not None}
        return {
            "kind": "redshift_data",
            "database": self.database,
            **named,
            "queries_dir": "queries",
        }


def _parameter_type(type_name: str) -> ParameterType | None:
    kind = type_name.lower().split("(")[0].strip()
    if kind in _TEXTS or kind.startswith("character"):
        return ParameterType.STRING
    if kind in _INTEGERS:
        return ParameterType.INTEGER
    if kind in _NUMBERS:
        return ParameterType.NUMBER
    if kind in {"bool", "boolean"}:
        return ParameterType.BOOLEAN
    if kind == "date":
        return ParameterType.DATE
    if kind in _TIMESTAMPS or kind.startswith("timestamp"):
        return ParameterType.TIMESTAMP
    return None


def tables_from_catalog(
    found: Sequence[CatalogTableLike],
) -> tuple[tuple[TableInfo, ...], tuple[str, ...]]:
    """Return what the proposals need to know about each table, and what was left out.

    Every column gets the value of its first stand-in row as its example, so a
    proposed lookup finds a row in the stand-in data.
    """
    tables: list[TableInfo] = []
    notes: list[str] = []
    for table in found:
        name = table.name.lower()
        usable = [column for column in table.columns if is_plain_name(column.name.lower())]
        left_out = [column.name for column in table.columns if column not in usable]
        if not is_plain_name(name):
            notes.append(f"left out the table {table.name}: a query cannot use its name as it is")
            continue
        if left_out:
            notes.append(
                f"{table.name}: left out {len(left_out)} column(s) whose names a query cannot "
                f"use as they are: {', '.join(left_out)}"
            )
        columns = []
        for column in usable:
            kind = _parameter_type(column.type_name)
            plain = column.name.lower()
            columns.append(ColumnInfo(plain, kind, examples=(stand_in(kind, plain, 1),)))
        tables.append(TableInfo(name, tuple(columns)))
    return tuple(tables), tuple(notes)


def stand_in_csv(table: TableInfo) -> str:
    """Return a CSV file with the table's columns and two made-up rows."""
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(column.name for column in table.columns)
    for number in range(1, _ROWS + 1):
        row = []
        for column in table.columns:
            value = stand_in(column.type, column.name, number)
            if isinstance(value, bool):
                row.append("true" if value else "false")
            else:
                # A timestamp is written the way the local engine reads one.
                row.append(str(value).replace("T00:00:00Z", " 00:00:00"))
        writer.writerow(row)
    return text.getvalue()


async def fetch_catalog(request: RedshiftRequest) -> Sequence[CatalogTableLike]:
    """Read the tables of the schema from the catalogue.

    Raises:
        CliError: If the AWS package is not installed, the request is not
            usable, or AWS refused or could not be reached.
    """
    try:
        from ai_agent_lib_aws.catalog_redshift import read_redshift_catalog
        from ai_agent_lib_aws.redshift_target import RedshiftTarget
        from ai_agent_lib_aws.session import AwsSessionFactory
    except ImportError:
        raise CliError(_INSTALL) from None
    try:
        target = RedshiftTarget(
            database=request.database,
            workgroup=request.workgroup,
            cluster_id=request.cluster_id,
            db_user=request.db_user,
            secret_arn=request.secret_arn,
        )
        sessions = AwsSessionFactory(profile=request.profile, region=request.region)
        found: Sequence[CatalogTableLike] = await read_redshift_catalog(
            sessions, target, schema=request.schema, tables=request.tables
        )
    except AgentLibError as problem:
        raise CliError(str(problem)) from None
    return found

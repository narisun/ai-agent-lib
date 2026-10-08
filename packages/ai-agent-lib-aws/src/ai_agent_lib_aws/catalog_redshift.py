"""Reading the Redshift catalogue: which tables a schema has, and their columns.

This is for tools that prepare a server, such as ``agentlib new mcp
--from-redshift``. It asks the Data API to list and describe tables. It runs
no statement and reads no row.

It needs ``redshift-data:ListTables`` and ``redshift-data:DescribeTable``, and
the same access to the database as the ``redshift_data`` data source.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai_agent_lib_aws.redshift_target import RedshiftTarget
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import ConfigurationError

__all__ = ["CatalogColumn", "CatalogTable", "read_redshift_catalog"]

_WHAT = "the Redshift catalogue"
_SERVICE = "redshift-data"
_PAGE = 100


@dataclass(frozen=True, slots=True)
class CatalogColumn:
    """One column of a table.

    Attributes:
        name: The column's name.
        type_name: The database's name for its type, such as ``varchar`` or ``int8``.
    """

    name: str
    type_name: str


@dataclass(frozen=True, slots=True)
class CatalogTable:
    """One table or view of a schema.

    Attributes:
        schema: The schema it is in.
        name: Its name.
        columns: Its columns, in order.
    """

    schema: str
    name: str
    columns: tuple[CatalogColumn, ...]


async def _pages(
    sessions: AwsSessionFactory, operation: Any, key: str, **arguments: Any
) -> list[Any]:
    """Return every item of a paged reply."""
    items: list[Any] = []
    token: str | None = None
    while True:
        page = dict(arguments, NextToken=token) if token else arguments
        reply = await sessions.invoke(_WHAT, operation, **page)
        items += reply.get(key) or []
        token = reply.get("NextToken")
        if not token:
            return items


async def read_redshift_catalog(
    sessions: AwsSessionFactory,
    target: RedshiftTarget,
    *,
    schema: str,
    tables: Sequence[str] = (),
    max_tables: int = 50,
) -> tuple[CatalogTable, ...]:
    """Return the tables and views of ``schema``, with their columns.

    Args:
        sessions: Builds the AWS client.
        target: The database to read.
        schema: The schema to list.
        tables: Read only the tables with these names. By default all of them.
        max_tables: The most tables to describe.

    Raises:
        ConfigurationError: If access is refused, a named table is not in the
            schema, or the schema holds more tables than ``max_tables``.
        CredentialsExpiredError: If the sign-in has expired.
        TransientError: If AWS throttled the call or could not be reached.
    """
    client = sessions.client(_SERVICE)
    listed = await _pages(
        sessions,
        client.list_tables,
        "Tables",
        SchemaPattern=schema,
        MaxResults=_PAGE,
        **target.arguments(),
    )
    names = sorted(
        str(table["name"])
        for table in listed
        if table.get("schema") == schema and not str(table.get("type", "")).startswith("SYSTEM")
    )
    if tables:
        missing = sorted(set(tables) - set(names))
        if missing:
            raise ConfigurationError(f"{_WHAT}: not in the schema {schema!r}: {', '.join(missing)}")
        names = [name for name in names if name in set(tables)]
    if len(names) > max_tables:
        raise ConfigurationError(
            f"{_WHAT}: the schema {schema!r} has {len(names)} tables; name the ones to read"
        )
    found = []
    for name in names:
        columns = await _pages(
            sessions,
            client.describe_table,
            "ColumnList",
            Schema=schema,
            Table=name,
            MaxResults=_PAGE,
            **target.arguments(),
        )
        found.append(
            CatalogTable(
                schema=schema,
                name=name,
                columns=tuple(
                    CatalogColumn(str(column["name"]), str(column.get("typeName", "")))
                    for column in columns
                ),
            )
        )
    return tuple(found)

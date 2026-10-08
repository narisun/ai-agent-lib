"""Proposing named queries from the tables a reader found.

A reader says what tables there are and what is in their columns. This module
turns that into a few queries a model can use: a lookup by the column that
identifies a row, and a filter for each column that sorts rows into a small
number of groups. Columns whose names suggest personal data are marked as
masked for analysts.

The proposals are a starting point the developer edits. They are deliberately
few and plain.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ai_agent_lib_cli.dataplan import (
    MAX_DESCRIPTION,
    MAX_QUERY_NAME,
    Example,
    PlannedParameter,
    PlannedQuery,
)
from ai_agent_lib_cli.names import PLAIN_NAME
from ai_agent_lib_core.contracts import ParameterType

__all__ = [
    "ColumnInfo",
    "TableInfo",
    "is_plain_name",
    "is_sensitive",
    "propose_queries",
    "stand_in",
]

_KEY_ROWS, _GROUP_ROWS, _ALL_ROWS = 10, 100, 100
_MAX_GROUPS = 50
_MAX_EXAMPLE = 30

# Column names that suggest personal or secret data. A match masks the column
# for analysts; it is a guess from the name, and the developer has the last word.
_SENSITIVE = re.compile(
    r"(^|_)("
    r"e_?mail|phone|mobile|fax|ssn|social_security|national_id|tax_id|tin|passport|"
    r"dob|birth|birthday|date_of_birth|address|street|postcode|post_code|zip|zip_code|"
    r"salary|income|wage|iban|account_number|acct_number|card_number|pan|cvv|"
    r"password|passwd|secret|token|api_key"
    r")(_|$)"
)
_KEY_NAME = re.compile(r"(^|_)(id|key|code|number|no|ref|uuid)$")
_FILTER_TYPES = frozenset({ParameterType.STRING, ParameterType.INTEGER, ParameterType.BOOLEAN})
_KEY_TYPES = frozenset({ParameterType.STRING, ParameterType.INTEGER})


# Words of the query language. A table or a column with one of these names has to be
# quoted everywhere it is used, so the proposals leave it out.
_RESERVED = frozenset(
    {
        "aes128",
        "aes256",
        "all",
        "allowoverwrite",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "authorization",
        "az64",
        "backup",
        "between",
        "binary",
        "blanksasnull",
        "both",
        "bytedict",
        "bzip2",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "connect",
        "constraint",
        "create",
        "credentials",
        "cross",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_user",
        "current_user_id",
        "default",
        "deferrable",
        "deflate",
        "defrag",
        "delta",
        "delta32k",
        "desc",
        "disable",
        "distinct",
        "do",
        "else",
        "emptyasnull",
        "enable",
        "encode",
        "encrypt",
        "encryption",
        "end",
        "except",
        "explicit",
        "false",
        "for",
        "foreign",
        "freeze",
        "from",
        "full",
        "globaldict256",
        "globaldict64k",
        "grant",
        "group",
        "gzip",
        "having",
        "identity",
        "ignore",
        "ilike",
        "in",
        "initially",
        "inner",
        "intersect",
        "interval",
        "into",
        "is",
        "isnull",
        "join",
        "language",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "lun",
        "luns",
        "lzo",
        "lzop",
        "minus",
        "mostly16",
        "mostly32",
        "mostly8",
        "natural",
        "new",
        "not",
        "notnull",
        "null",
        "nulls",
        "off",
        "offline",
        "offset",
        "oid",
        "old",
        "on",
        "only",
        "open",
        "or",
        "order",
        "outer",
        "overlaps",
        "parallel",
        "partition",
        "percent",
        "permissions",
        "pivot",
        "placing",
        "primary",
        "raw",
        "readratio",
        "recover",
        "references",
        "rejectlog",
        "resort",
        "respect",
        "restore",
        "right",
        "select",
        "session_user",
        "similar",
        "snapshot",
        "some",
        "sysdate",
        "system",
        "table",
        "tag",
        "tdes",
        "text255",
        "text32k",
        "then",
        "timestamp",
        "to",
        "top",
        "trailing",
        "true",
        "truncatecolumns",
        "union",
        "unique",
        "unnest",
        "unpivot",
        "user",
        "using",
        "verbose",
        "wallet",
        "when",
        "where",
        "with",
        "without",
    }
)


def is_plain_name(name: str) -> bool:
    """Return whether ``name`` can be used in a query without quoting."""
    return bool(PLAIN_NAME.match(name)) and name not in _RESERVED


def is_sensitive(column: str) -> bool:
    """Return whether a column's name suggests personal or secret data."""
    return bool(_SENSITIVE.search(column.lower()))


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """What a reader found out about one column.

    Attributes:
        name: The column's name, in lower case.
        type: The parameter type its values fit, or ``None`` if a query
            cannot take a value of this column as a parameter.
        distinct: How many different values it holds, when the reader counted.
        nulls: How many rows have no value, when the reader counted.
        examples: A few of its values, most common first.
    """

    name: str
    type: ParameterType | None
    distinct: int | None = None
    nulls: int | None = None
    examples: tuple[Example, ...] = ()


@dataclass(frozen=True, slots=True)
class TableInfo:
    """What a reader found out about one table.

    Attributes:
        name: The table's name as a query refers to it.
        columns: Its columns, in order.
        rows: How many rows it holds, when the reader counted.
    """

    name: str
    columns: tuple[ColumnInfo, ...]
    rows: int | None = None


def _key_of(table: TableInfo) -> ColumnInfo | None:
    """Return the column that identifies a row: a value per row, none missing."""
    candidates = [
        column
        for column in table.columns
        if column.type in _KEY_TYPES and not is_sensitive(column.name)
    ]
    counted = table.rows is not None
    unique = [
        column
        for column in candidates
        if counted and column.distinct == table.rows and not column.nulls and table.rows
    ]
    named = [
        column for column in (unique if counted else candidates) if _KEY_NAME.search(column.name)
    ]
    if named:
        return named[0]
    # Without a telling name, the first column is the key only if it was seen to be unique.
    return unique[0] if unique and unique[0] is table.columns[0] else None


def _groups_of(table: TableInfo, key: ColumnInfo | None) -> list[ColumnInfo]:
    """Return the columns that sort rows into a few groups, fewest groups first."""
    if table.rows is None:
        return []
    found = [
        column
        for column in table.columns
        if column is not key
        and column.type in _FILTER_TYPES
        and not is_sensitive(column.name)
        and column.distinct is not None
        and 2 <= column.distinct <= _MAX_GROUPS
        and column.distinct < table.rows
    ]
    return sorted(found, key=lambda column: (column.distinct or 0, column.name))


def _such_as(column: ColumnInfo) -> str:
    """Return a few of the column's values for a description, or nothing."""
    values = [
        str(value)
        for value in column.examples
        if isinstance(value, str) and 0 < len(value) <= _MAX_EXAMPLE and value.isprintable()
    ][:3]
    if len(values) < 2:
        return ""
    return ", such as " + ", ".join(values[:-1]) + " or " + values[-1]


def _statement(table: TableInfo, where: ColumnInfo | None, order: ColumnInfo) -> str:
    lines = [
        "SELECT " + ", ".join(column.name for column in table.columns),
        f"FROM {table.name}",
    ]
    if where is not None:
        lines.append(f"WHERE {where.name} = :{where.name}")
    lines.append(f"ORDER BY {order.name}")
    return "\n".join(lines) + "\n"


def _query(
    table: TableInfo,
    name: str,
    description: str,
    where: ColumnInfo | None,
    order: ColumnInfo,
    max_rows: int,
) -> PlannedQuery | None:
    if len(name) > MAX_QUERY_NAME or not is_plain_name(name):
        return None
    parameters: tuple[PlannedParameter, ...] = ()
    header = [f"-- description: {description}"]
    if where is not None and where.type is not None:
        example: Example = where.examples[0] if where.examples else _placeholder(where.type)
        parameters = (PlannedParameter(where.name, where.type.value, example),)
        header.append(f"-- param {where.name}: {where.type.value}")
    masked = tuple(column.name for column in table.columns if is_sensitive(column.name))
    classification = "confidential" if masked else "internal"
    header += [f"-- max_rows: {max_rows}", f"-- classification: {classification}"]
    return PlannedQuery(
        name=name,
        description=description,
        parameters=parameters,
        columns=tuple(column.name for column in table.columns),
        masked=masked,
        classification=classification,
        max_rows=max_rows,
        definition="\n".join(header) + "\n" + _statement(table, where, order),
        returns_rows=bool(table.rows) and (where is None or bool(where.examples)),
    )


def stand_in(kind: ParameterType | None, name: str, number: int) -> Example:
    """Return a made-up value of the right type for a column or a parameter.

    Args:
        kind: The type. Without one the value is text.
        name: The plain name of the column or parameter.
        number: Which made-up row this is, from 1. Different rows get different values.
    """
    if kind is ParameterType.INTEGER:
        return number
    if kind is ParameterType.NUMBER:
        return number + 0.5
    if kind is ParameterType.BOOLEAN:
        return number % 2 == 1
    if kind is ParameterType.DATE:
        return f"2026-01-0{number}"
    if kind is ParameterType.TIMESTAMP:
        return f"2026-01-0{number}T00:00:00Z"
    if "mail" in name:
        return f"person{number}@example.test"
    return f"{name}-{number}"


def _placeholder(kind: ParameterType) -> Example:
    """Return a value of the right type for a column whose values were not read."""
    if kind is ParameterType.INTEGER:
        return 1
    if kind is ParameterType.BOOLEAN:
        return True
    return "example"


def propose_queries(tables: Sequence[TableInfo], *, per_table: int = 3) -> list[PlannedQuery]:
    """Return the queries proposed for ``tables``, at most ``per_table`` for each.

    A table or a column whose name would need quoting is left out; the reader
    reports those. A table with no usable column gets no query.
    """
    proposed: list[PlannedQuery] = []
    for table in tables:
        usable = TableInfo(
            table.name,
            tuple(column for column in table.columns if is_plain_name(column.name)),
            table.rows,
        )
        if not is_plain_name(usable.name) or not usable.columns:
            continue
        key = _key_of(usable)
        order = key or usable.columns[0]
        found: list[PlannedQuery | None] = []
        if key is not None:
            found.append(
                _query(
                    usable,
                    f"{usable.name}_by_{key.name}",
                    f"The row of {usable.name} with one {key.name}.",
                    key,
                    order,
                    _KEY_ROWS,
                )
            )
        for group in _groups_of(usable, key):
            plain = f"The rows of {usable.name} with one {group.name}"
            telling = f"{plain}{_such_as(group)}."
            found.append(
                _query(
                    usable,
                    f"{usable.name}_by_{group.name}",
                    # A description is one line of the generated code as well.
                    telling if len(telling) <= MAX_DESCRIPTION else f"{plain}.",
                    group,
                    order,
                    _GROUP_ROWS,
                )
            )
        kept = [query for query in found if query is not None][:per_table]
        if not kept:
            everything = _query(
                usable,
                f"{usable.name}_all",
                f"Every row of {usable.name}, at most {_ALL_ROWS}.",
                None,
                order,
                _ALL_ROWS,
            )
            kept = [everything] if everything is not None else []
        proposed += kept
    return proposed

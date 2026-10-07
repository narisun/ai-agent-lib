"""Turning a named query plus obligations into one statement for a target engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from sqlglot import exp

from ai_agent_lib_core.adapters.obligations import (
    check_filter_columns,
    effective_row_cap,
    mask_rows,
)
from ai_agent_lib_core.adapters.sql.catalog import NamedQuery
from ai_agent_lib_core.contracts import Obligations, QueryResult, SourceMetadata

__all__ = [
    "CompiledQuery",
    "GovernedStatement",
    "build_statement",
    "compile_query",
    "finish_result",
]

_ALIAS = "governed_query"
_FILTER_PARAMETER = "_obl_{filter}_{value}"


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """A statement ready to run.

    Attributes:
        sql: The statement, in the target dialect.
        parameters: Bound values added for the row filters.
        row_cap: The most rows that may be returned. The statement asks for one
            more, so the caller can tell whether the result was cut off.
    """

    sql: str
    parameters: Mapping[str, object]
    row_cap: int


@dataclass(frozen=True, slots=True)
class GovernedStatement:
    """A named query wrapped with its obligations, before it is written for an engine.

    An adapter that has to adjust the statement for its engine, for example to
    give each bound parameter a type, works on ``expression`` and writes the
    SQL itself.

    Attributes:
        expression: The statement, as a syntax tree in the query's own dialect.
        parameters: Bound values added for the row filters.
        row_cap: The most rows that may be returned. The statement asks for one more.
    """

    expression: exp.Select
    parameters: Mapping[str, object]
    row_cap: int


def compile_query(
    query: NamedQuery, obligations: Obligations | None, *, target_dialect: str
) -> CompiledQuery:
    """Wrap ``query`` so that row filters and the row cap run inside the database.

    The row filters are applied to the query's result and the cap is applied
    after them, so a filtered-out row never uses up part of the cap. Filter
    values are always bound parameters.

    Raises:
        PolicyDenied: If a row filter names a column the query does not return.
    """
    statement = build_statement(query, obligations)
    return CompiledQuery(
        sql=statement.expression.sql(dialect=target_dialect),
        parameters=statement.parameters,
        row_cap=statement.row_cap,
    )


def build_statement(query: NamedQuery, obligations: Obligations | None) -> GovernedStatement:
    """Wrap ``query`` with its row filters and row cap, as a syntax tree.

    Raises:
        PolicyDenied: If a row filter names a column the query does not return.
    """
    filters = obligations.row_filters if obligations is not None else ()
    check_filter_columns(query.outputs, filters)
    row_cap = effective_row_cap(query.description.max_rows, obligations)

    inner = query.expression.copy()
    order = inner.args.get("order")
    if order is not None and inner.args.get("limit") is None and inner.args.get("offset") is None:
        # Hoist the ordering to the outer statement, where it is guaranteed to hold.
        inner.set("order", None)

    outer = exp.select("*").from_(inner.subquery(_ALIAS))
    parameters: dict[str, object] = {}
    for filter_index, row_filter in enumerate(filters):
        placeholders = []
        for value_index, value in enumerate(row_filter.values):
            name = _FILTER_PARAMETER.format(filter=filter_index, value=value_index)
            parameters[name] = value
            placeholders.append(exp.Placeholder(this=name))
        condition: exp.Expression = (
            exp.column(row_filter.column.lower()).isin(*placeholders)
            if placeholders
            else exp.false()  # an empty filter admits no row
        )
        outer = outer.where(condition)
    if order is not None:
        outer = outer.order_by(*(ordered.copy() for ordered in order.expressions))
    outer = outer.limit(row_cap + 1)
    return GovernedStatement(
        expression=outer, parameters=MappingProxyType(parameters), row_cap=row_cap
    )


def finish_result(
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    compiled: CompiledQuery,
    obligations: Obligations | None,
    source: SourceMetadata | None = None,
) -> QueryResult:
    """Apply the row cap and the column masks to fetched rows."""
    truncated = len(rows) > compiled.row_cap
    mask_columns = obligations.mask_columns if obligations is not None else frozenset()
    kept, masked = mask_rows(columns, rows[: compiled.row_cap], mask_columns)
    return QueryResult(
        columns=tuple(columns), rows=kept, truncated=truncated, masked_columns=masked, source=source
    )

"""Enforcing policy obligations on rows that are already in memory.

SQL sources push row filters and the row cap into the statement. Masking, and
everything for sources that cannot filter themselves, is done here. In both
cases enforcement happens inside the data layer, before a row is returned.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ai_agent_lib_core.contracts import MASK, Obligations, PolicyDenied, RowFilter

__all__ = ["check_filter_columns", "effective_row_cap", "filter_rows", "mask_rows"]


def effective_row_cap(query_max_rows: int, obligations: Obligations | None) -> int:
    """Return the lower of the query's own cap and the policy's."""
    if obligations is None or obligations.max_rows is None:
        return query_max_rows
    return min(query_max_rows, obligations.max_rows)


def check_filter_columns(columns: Iterable[str], filters: Iterable[RowFilter]) -> None:
    """Fail closed if a row filter names a column the result does not have.

    Raises:
        PolicyDenied: If a filter cannot be applied. Returning unfiltered rows
            instead would silently drop a policy condition.
    """
    available = {column.lower() for column in columns}
    for row_filter in filters:
        if row_filter.column.lower() not in available:
            raise PolicyDenied(
                f"the policy filters on column {row_filter.column!r}, which this query "
                "does not return, so the filter cannot be enforced",
                reason_code="obligation_unenforceable",
            )


def filter_rows(
    columns: Sequence[str], rows: Iterable[Sequence[object]], filters: Sequence[RowFilter]
) -> list[tuple[object, ...]]:
    """Return the rows that satisfy every filter."""
    check_filter_columns(columns, filters)
    positions = {column.lower(): index for index, column in enumerate(columns)}
    checks = [(positions[f.column.lower()], frozenset(f.values)) for f in filters]
    return [tuple(row) for row in rows if all(row[index] in allowed for index, allowed in checks)]


def mask_rows(
    columns: Sequence[str], rows: Iterable[Sequence[object]], mask_columns: frozenset[str]
) -> tuple[tuple[tuple[object, ...], ...], frozenset[str]]:
    """Replace the values of masked columns.

    Returns:
        The rows, and the names of the columns that were actually masked.
    """
    targets = [i for i, column in enumerate(columns) if column.lower() in mask_columns]
    if not targets:
        return tuple(tuple(row) for row in rows), frozenset()
    masked_rows = []
    for row in rows:
        values = list(row)
        for index in targets:
            values[index] = MASK
        masked_rows.append(tuple(values))
    return tuple(masked_rows), frozenset(columns[index] for index in targets)

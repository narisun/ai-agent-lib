"""Governed data access: named queries, their results and policy obligations."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from ai_agent_lib_core.contracts._validation import require_identifier
from ai_agent_lib_core.contracts.identity import Classification

__all__ = [
    "MASK",
    "DataSource",
    "Obligations",
    "ParameterType",
    "QueryDescription",
    "QueryParameter",
    "QueryResult",
    "RowFilter",
    "Scalar",
    "SourceMetadata",
]

MASK = "***"
"""What a masked value is replaced with."""

Scalar = str | int | float | bool
"""A value a row filter may compare against."""


class ParameterType(enum.StrEnum):
    """The types a query parameter may have."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"


@dataclass(frozen=True, slots=True)
class QueryParameter:
    """One declared parameter of a named query."""

    name: str
    type: ParameterType
    required: bool = True
    default: object = None


@dataclass(frozen=True, slots=True)
class QueryDescription:
    """What a caller may know about a named query.

    Attributes:
        name: The name the query is called by.
        description: What the query returns, in one line.
        parameters: The declared parameters.
        max_rows: The most rows the query may return.
        classification: How sensitive the returned data is.
    """

    name: str
    description: str
    parameters: tuple[QueryParameter, ...]
    max_rows: int
    classification: Classification = Classification.INTERNAL


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Where a result came from and how fresh it is.

    The library carries this metadata; it does not enforce freshness rules.

    Attributes:
        source: The name of the data source.
        retrieved_at: When the library fetched the data, as an aware datetime.
        as_of: The time the data itself describes, when the source says.
        uri: A reference to the underlying record or query, when there is one.
    """

    source: str
    retrieved_at: datetime
    as_of: datetime | None = None
    uri: str | None = None

    def __post_init__(self) -> None:
        require_identifier("source", self.source)
        for label, moment in (("retrieved_at", self.retrieved_at), ("as_of", self.as_of)):
            if moment is not None and moment.tzinfo is None:
                raise ValueError(f"{label} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RowFilter:
    """Keep only rows whose ``column`` holds one of ``values``."""

    column: str
    values: tuple[Scalar, ...]

    def __post_init__(self) -> None:
        require_identifier("column", self.column)
        object.__setattr__(self, "values", tuple(self.values))
        for value in self.values:
            if not isinstance(value, str | int | float | bool):
                raise TypeError("row filter values must be scalars")


@dataclass(frozen=True, slots=True)
class Obligations:
    """Conditions a policy attaches to an allowed request.

    Obligations only ever narrow what is returned.

    Attributes:
        row_filters: Every filter must hold for a row to be returned.
        mask_columns: Columns whose values are replaced before results leave.
            A mask applies to a returned column of that name. A query that
            returns the same data under another name is not masked by it, so
            queries keep the names the policy speaks of.
        max_rows: A row cap lower than the query's own.
        require_approval: A person must approve before the call runs.
    """

    row_filters: tuple[RowFilter, ...] = ()
    mask_columns: frozenset[str] = frozenset()
    max_rows: int | None = None
    require_approval: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_filters", tuple(self.row_filters))
        object.__setattr__(
            self, "mask_columns", frozenset(column.lower() for column in self.mask_columns)
        )
        if self.max_rows is not None and self.max_rows < 0:
            raise ValueError("max_rows must not be negative")

    @property
    def empty(self) -> bool:
        """Whether there is nothing to enforce."""
        return self == Obligations()


@dataclass(frozen=True, slots=True)
class QueryResult:
    """The rows a named query returned, after limits and obligations.

    Attributes:
        columns: The column names, in order.
        rows: The rows, each in column order.
        truncated: Whether more rows existed than the cap allowed.
        masked_columns: The columns whose values were masked.
        source: Where the data came from.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool = False
    masked_columns: frozenset[str] = field(default_factory=frozenset)
    source: SourceMetadata | None = None

    def as_dicts(self) -> list[dict[str, object]]:
        """Return the rows as dictionaries keyed by column name."""
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]

    def to_payload(self) -> dict[str, object]:
        """Return the result as plain JSON values, ready to hand back from a tool.

        The payload says what a reader needs to judge the rows: whether more
        existed, which columns were masked, and where and when the data was
        fetched. Dates and exact decimals are written as text.
        """
        payload: dict[str, object] = {
            "columns": list(self.columns),
            "rows": [[_json_value(value) for value in row] for row in self.rows],
            "truncated": self.truncated,
            "masked_columns": sorted(self.masked_columns),
        }
        if self.source is not None:
            payload["source"] = {
                "name": self.source.source,
                "retrieved_at": self.source.retrieved_at.isoformat(),
                "as_of": self.source.as_of.isoformat() if self.source.as_of else None,
                "uri": self.source.uri,
            }
        return payload


def _json_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


class DataSource(Protocol):
    """Read-only access to data through named, parameterized queries.

    A caller supplies a query name and parameter values. It never supplies
    query text, so nothing a model writes can become part of a statement.
    """

    def describe(self) -> Mapping[str, QueryDescription]:
        """Return the queries this source offers, by name."""
        ...

    async def query(
        self,
        name: str,
        parameters: Mapping[str, object] | None = None,
        *,
        obligations: Obligations | None = None,
    ) -> QueryResult:
        """Run the query called ``name``.

        Args:
            name: The query to run.
            parameters: Values for the query's declared parameters.
            obligations: Conditions to enforce before any row is returned.

        Raises:
            ValidationFailed: If the query is unknown or a parameter is invalid.
            PolicyDenied: If an obligation cannot be enforced.
            TransientError: If the source timed out or was unavailable.
        """
        ...

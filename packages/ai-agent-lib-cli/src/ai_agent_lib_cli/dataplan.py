"""What a generated MCP server offers over its data.

A plan lists the named queries a server exposes as tools: their parameters,
the columns they return, and the columns an analyst sees masked. The templates
follow a plan, whether it is the built-in sample or was proposed by a reader
from the developer's own data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import PurePosixPath
from typing import Any, Self

from ai_agent_lib_cli.toml_text import string_of, toml_list, toml_text
from ai_agent_lib_core.contracts import Classification, ParameterType

__all__ = [
    "API_RESPONSES_FILE",
    "MAX_DESCRIPTION",
    "MAX_QUERY_NAME",
    "ORIGINS",
    "SAMPLE",
    "DataPlan",
    "PlannedParameter",
    "PlannedQuery",
    "sample_plan",
]

SAMPLE = "sample"

MAX_QUERY_NAME = 48
"""The longest name a proposed query gets. With the server's ID it names a tool."""

MAX_DESCRIPTION = 84
"""The longest description a proposed query gets: it is one line of generated code."""

API_RESPONSES_FILE = PurePosixPath("tests/api_responses.json")
"""In a server over a REST API: what the API answers in the server's tests."""

ORIGINS = (SAMPLE, "csv", "openapi", "redshift")
"""Where a plan can come from: the built-in sample, or one of the readers."""

Example = str | int | float | bool

# What a tool's argument is declared as. Dates travel as text in ISO form.
_ANNOTATIONS: Mapping[ParameterType, str] = {
    ParameterType.STRING: "str",
    ParameterType.INTEGER: "int",
    ParameterType.NUMBER: "float",
    ParameterType.BOOLEAN: "bool",
    ParameterType.DATE: "str",
    ParameterType.TIMESTAMP: "str",
}
_SUFFIXES: Mapping[str, str] = {"duckdb_csv": ".sql", "rest": ".yaml"}


@dataclass(frozen=True, slots=True)
class PlannedParameter:
    """One argument of a planned query.

    Attributes:
        name: The parameter's name, which is also the tool argument's name.
        type: The declared type, a value of ``ParameterType``.
        example: A value the generated tests call the tool with.
    """

    name: str
    type: str
    example: Example

    def __post_init__(self) -> None:
        ParameterType(self.type)  # raises ValueError for a type that does not exist

    @property
    def annotation(self) -> str:
        """The Python type the tool declares for this argument."""
        return _ANNOTATIONS[ParameterType(self.type)]


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    """One named query, offered as one tool.

    Attributes:
        name: The name of the query, its file and its tool.
        description: One line on what it returns. A model reads it.
        parameters: The arguments.
        columns: The columns it returns, in order.
        masked: The columns an analyst sees masked.
        classification: How sensitive the result is, as a lower-case level name.
        max_rows: The most rows it returns.
        definition: The text of the query file.
        returns_rows: Whether a call with the example values is known to return rows.
    """

    name: str
    description: str
    parameters: tuple[PlannedParameter, ...]
    columns: tuple[str, ...]
    masked: tuple[str, ...]
    classification: str
    max_rows: int
    definition: str
    returns_rows: bool = True

    def __post_init__(self) -> None:
        Classification[self.classification.upper()]  # raises KeyError for an unknown level

    @property
    def signature(self) -> str:
        """The tool function's parameter list."""
        return ", ".join(f"{p.name}: {p.annotation}" for p in self.parameters)

    @property
    def arguments(self) -> str:
        """The mapping the tool passes to the query, as Python source."""
        return "{" + ", ".join(f'"{p.name}": {p.name}' for p in self.parameters) + "}"

    @property
    def example_arguments(self) -> str:
        """The arguments of an example call, as Python source."""
        return repr({p.name: p.example for p in self.parameters})


@dataclass(frozen=True, slots=True)
class DataPlan:
    """The data a generated MCP server reads and the queries it offers.

    Attributes:
        origin: Where the plan came from, one of ``ORIGINS``.
        source: The name of the data source in the service's configuration.
        kind: The kind of data source the service uses locally.
        queries: The queries, each offered as one tool. The first is the one
            the tests of a linked agent call.
        options: Options of the data source beyond where its files are.
        deployed: The data source's settings where the service is deployed,
            when they differ from the local ones.
        notes: What the developer has to know or do that the code cannot say.
            The server's README passes them on.
    """

    origin: str
    source: str
    kind: str
    queries: tuple[PlannedQuery, ...]
    options: Mapping[str, Any] = field(default_factory=dict)
    deployed: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of: {', '.join(ORIGINS)}")
        if self.kind not in _SUFFIXES:
            raise ValueError(f"kind must be one of: {', '.join(_SUFFIXES)}")
        if not self.queries:
            raise ValueError("a plan needs at least one query")
        names = [query.name for query in self.queries]
        if len(names) != len(set(names)):
            raise ValueError("two queries of a plan have the same name")

    @property
    def is_sample(self) -> bool:
        """Whether this is the built-in sample."""
        return self.origin == SAMPLE

    @property
    def deployed_setting(self) -> str:
        """The deployed data source as the value of the data sources setting, or nothing."""
        return json.dumps({self.source: dict(self.deployed)}) if self.deployed else ""

    @property
    def suffix(self) -> str:
        """The suffix of a query file of this kind of data source."""
        return _SUFFIXES[self.kind]

    @property
    def masked(self) -> tuple[str, ...]:
        """Every column an analyst sees masked, in any query."""
        return tuple(sorted({column for query in self.queries for column in query.masked}))

    def to_toml(self, table: str) -> list[str]:
        """Return the lines that record the plan under the TOML table ``table``."""
        # The order of the settings is kept: what is written from them must not
        # change when the plan is read back.
        lines = [
            "",
            f"[{table}]",
            f"origin = {toml_text(self.origin)}",
            f"source = {toml_text(self.source)}",
            f"kind = {toml_text(self.kind)}",
            f"options = {toml_text(json.dumps(dict(self.options)))}",
            f"deployed = {toml_text(json.dumps(dict(self.deployed)))}",
            f"notes = {toml_list(self.notes)}",
        ]
        for query in self.queries:
            parameters = ", ".join(
                f"{{ name = {toml_text(p.name)}, type = {toml_text(p.type)}, "
                f"example = {json.dumps(p.example, ensure_ascii=False)} }}"
                for p in query.parameters
            )
            lines += [
                "",
                f"[[{table}.queries]]",
                f"name = {toml_text(query.name)}",
                f"description = {toml_text(query.description)}",
                f"parameters = [{parameters}]",
                f"columns = {toml_list(query.columns)}",
                f"masked = {toml_list(query.masked)}",
                f"classification = {toml_text(query.classification)}",
                f"max_rows = {query.max_rows}",
                f"returns_rows = {json.dumps(query.returns_rows)}",
                f"definition = {toml_text(query.definition)}",
            ]
        return lines

    @classmethod
    def from_toml(cls, raw: Mapping[str, Any]) -> Self:
        """Read a plan from its parsed TOML table.

        Raises:
            KeyError, TypeError, ValueError: If the table is not a plan.
        """
        return cls(
            origin=string_of(raw, "origin"),
            source=string_of(raw, "source"),
            kind=string_of(raw, "kind"),
            options=dict(json.loads(string_of(raw, "options"))),
            deployed=dict(json.loads(raw.get("deployed", "{}"))),
            notes=tuple(raw.get("notes", ())),
            queries=tuple(
                PlannedQuery(
                    name=string_of(query, "name"),
                    description=string_of(query, "description"),
                    parameters=tuple(
                        PlannedParameter(
                            name=string_of(p, "name"),
                            type=string_of(p, "type"),
                            example=p["example"],
                        )
                        for p in query["parameters"]
                    ),
                    columns=tuple(query["columns"]),
                    masked=tuple(query["masked"]),
                    classification=string_of(query, "classification"),
                    max_rows=int(query["max_rows"]),
                    returns_rows=bool(query["returns_rows"]),
                    definition=string_of(query, "definition"),
                )
                for query in raw["queries"]
            ),
        )


_SAMPLE_QUERY_FILE = "templates/mcp-sample/queries/people_by_team.sql.jinja"


def sample_plan() -> DataPlan:
    """Return the plan of the hello-world server: a people directory in one CSV file."""
    return DataPlan(
        origin=SAMPLE,
        source="people",
        kind="duckdb_csv",
        queries=(
            PlannedQuery(
                name="people_by_team",
                description="The people in one team: platform, payments or risk.",
                parameters=(PlannedParameter("team", "string", "payments"),),
                columns=("person_id", "name", "team", "email"),
                masked=("email",),
                classification="confidential",
                max_rows=50,
                definition=resources.files("ai_agent_lib_cli")
                .joinpath(_SAMPLE_QUERY_FILE)
                .read_text(encoding="utf-8"),
            ),
        ),
    )

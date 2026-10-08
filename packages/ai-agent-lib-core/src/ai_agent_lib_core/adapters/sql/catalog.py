"""Named queries: SQL files with a declared interface.

A query file is ordinary SQL with a header of comment lines::

    -- description: Balances of the accounts in one region.
    -- param region: string
    -- param min_balance: number = 0
    -- max_rows: 100
    -- classification: restricted
    SELECT account_id, holder, balance
    FROM accounts
    WHERE region = :region AND balance >= :min_balance
    ORDER BY account_id

The file name is the query name. The SQL is written in the dialect of the real
database, which is the source of truth, and transpiled for local engines.
Queries are checked when they are loaded, so a mistake stops startup instead
of surfacing on the first call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ai_agent_lib_core.adapters.parameters import bind_parameters, coerce_parameter
from ai_agent_lib_core.contracts import (
    Classification,
    ConfigurationError,
    ParameterType,
    QueryDescription,
    QueryParameter,
    ValidationFailed,
)

__all__ = ["NamedQuery", "QueryCatalog"]

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PARAM = re.compile(r"^param\s+(?P<name>[a-z][a-z0-9_]*)$")
_HEADER = re.compile(r"^--\s*(?P<key>[^:]+?)\s*:\s*(?P<value>.*)$")


@dataclass(frozen=True, slots=True)
class NamedQuery:
    """A loaded, validated query.

    Attributes:
        description: The query's declared interface.
        expression: The parsed statement, in the source dialect.
        outputs: The names of the columns the query returns, lower-cased.
    """

    description: QueryDescription
    expression: exp.Query
    outputs: tuple[str, ...]


def _parse_header(name: str, lines: Iterable[str]) -> QueryDescription:
    description = ""
    max_rows: int | None = None
    classification = Classification.INTERNAL
    parameters: list[QueryParameter] = []
    for line in lines:
        match = _HEADER.match(line)
        if match is None:
            raise ConfigurationError(
                f"query {name!r}: a header line is not '-- key: value'",
                expected="comment lines such as '-- description: ...', '-- param region: string', "
                "'-- max_rows: 100', then the SQL",
                actual=line[:80],
            )
        key, value = match["key"].strip().lower(), match["value"].strip()
        param = _PARAM.match(key)
        if param is not None:
            parameters.append(_parse_parameter(name, param["name"], value))
        elif key == "description":
            description = value
        elif key == "max_rows":
            if not value.isdigit() or int(value) < 1:
                raise ConfigurationError(
                    f"query {name!r}: max_rows is not a positive whole number",
                    expected="'-- max_rows: 100': the most rows one call returns",
                    actual=value[:40],
                )
            max_rows = int(value)
        elif key == "classification":
            try:
                classification = Classification[value.upper()]
            except KeyError:
                allowed = ", ".join(level.name.lower() for level in Classification)
                raise ConfigurationError(
                    f"query {name!r}: the classification is not known",
                    expected=f"one of: {allowed}",
                    actual=value[:40],
                ) from None
        else:
            raise ConfigurationError(
                f"query {name!r}: unknown header key {key[:40]!r}",
                expected="description, param <name>, max_rows or classification",
                actual=key[:40],
            )
    if not description:
        raise ConfigurationError(
            f"query {name!r} has no description",
            expected="a header line '-- description: ...'; the model reads it to choose the tool",
            fix="add the line above the SQL",
        )
    if max_rows is None:
        raise ConfigurationError(
            f"query {name!r} has no row limit",
            expected="a header line '-- max_rows: <n>', the most rows one call returns",
            fix="add the line above the SQL",
        )
    names = [parameter.name for parameter in parameters]
    if len(names) != len(set(names)):
        twice = sorted({item for item in names if names.count(item) > 1})
        raise ConfigurationError(
            f"query {name!r}: a parameter is declared twice",
            expected="one '-- param <name>: <type>' line per parameter",
            actual=f"{', '.join(twice)} declared more than once",
        )
    return QueryDescription(
        name=name,
        description=description,
        parameters=tuple(parameters),
        max_rows=max_rows,
        classification=classification,
    )


def _parse_parameter(query: str, name: str, declaration: str) -> QueryParameter:
    type_text, has_default, default_text = (part.strip() for part in declaration.partition("="))
    try:
        kind = ParameterType(type_text.lower())
    except ValueError:
        allowed = ", ".join(member.value for member in ParameterType)
        raise ConfigurationError(
            f"query {query!r}: parameter {name!r} has an unknown type",
            expected=f"one of: {allowed}, as in '-- param {name}: string'",
            actual=type_text[:40],
        ) from None
    parameter = QueryParameter(name=name, type=kind, required=not has_default)
    if not has_default:
        return parameter
    try:
        default = coerce_parameter(parameter, default_text)
    except ValidationFailed:
        raise ConfigurationError(
            f"query {query!r}: the default of parameter {name!r} does not match its type",
            expected=f"a {kind.value}",
            actual=default_text[:40],
        ) from None
    return QueryParameter(name=name, type=kind, required=False, default=default)


def _parse_statement(name: str, sql: str, dialect: str) -> tuple[exp.Query, tuple[str, ...]]:
    try:
        statements = [statement for statement in sqlglot.parse(sql, read=dialect) if statement]
    except SqlglotError as exc:
        raise ConfigurationError(f"query {name!r}: the SQL cannot be parsed: {exc}") from None
    if len(statements) != 1:
        raise ConfigurationError(
            f"query {name!r}: a query file holds one statement",
            expected="exactly one SELECT",
            actual=f"{len(statements)} statements",
        )
    statement = statements[0]
    read_only = ConfigurationError(
        f"query {name!r} is not read-only",
        expected="a SELECT that changes nothing",
        actual="a statement that is not a SELECT, or one that writes (INSERT, UPDATE, INTO, ...)",
        fix="named queries only read; do writes elsewhere",
    )
    if not isinstance(statement, exp.Query):
        raise read_only
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    if any(statement.find_all(*forbidden)) or statement.find(exp.Into) is not None:
        raise read_only

    outputs: list[str] = []
    for column in statement.selects:
        if column.is_star or not column.alias_or_name:
            raise ConfigurationError(
                f"query {name!r}: a returned column has no name",
                expected="every returned column named: a column, or an expression with AS",
                actual="'SELECT *' or an unnamed expression",
                fix="list the columns, and name expressions: SUM(x) AS total",
            )
        outputs.append(column.alias_or_name.lower())
    if len(outputs) != len(set(outputs)):
        twice = sorted({item for item in outputs if outputs.count(item) > 1})
        raise ConfigurationError(
            f"query {name!r}: two returned columns share a name",
            expected="a distinct name for every returned column",
            actual=", ".join(twice),
            fix="rename one with AS",
        )

    order = statement.args.get("order")
    if order is not None:
        for ordered in order.expressions:
            target = ordered.this
            if (
                not isinstance(target, exp.Column)
                or target.table
                or target.name.lower() not in outputs
            ):
                raise ConfigurationError(
                    f"query {name!r}: ORDER BY names something the query does not return",
                    expected=f"names of returned columns: {', '.join(outputs)}",
                    actual=target.sql()[:60],
                )
    return statement, tuple(outputs)


def _load_file(path: Path, dialect: str) -> NamedQuery:
    name = path.stem
    if not _NAME.match(name):
        raise ConfigurationError(
            f"query file {path.name!r} has a name a tool cannot use",
            expected="lower-case letters, digits and underscores, starting with a letter",
            actual=path.stem[:60],
            fix="rename the file; its name is the query's name",
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"query file cannot be read: {path}") from exc
    try:
        return _parse_query(name, lines, dialect)
    except ConfigurationError as error:
        error.add_note(f"in {path}")
        raise


def _parse_query(name: str, lines: list[str], dialect: str) -> NamedQuery:
    split = next((i for i, line in enumerate(lines) if not line.startswith("--")), len(lines))
    description = _parse_header(name, (line for line in lines[:split] if line.strip("- ")))
    statement, outputs = _parse_statement(name, "\n".join(lines[split:]), dialect)

    declared = {parameter.name for parameter in description.parameters}
    used = {placeholder.name for placeholder in statement.find_all(exp.Placeholder)}
    if "" in used or any(not _NAME.match(item) for item in used):
        raise ConfigurationError(
            f"query {name!r}: a parameter is not written as ':name'",
            expected="placeholders such as :region, lower-case",
            fix="write each parameter as a colon and its declared name",
        )
    if used - declared:
        missing = sorted(used - declared)
        raise ConfigurationError(
            f"query {name!r} uses parameters it does not declare",
            expected="a '-- param <name>: <type>' header line for every :name in the SQL",
            actual=f"no declaration of {', '.join(missing)}",
            fix=f"add '-- param {missing[0]}: string' (or the right type) to the header",
        )
    if declared - used:
        unused = sorted(declared - used)
        raise ConfigurationError(
            f"query {name!r} declares parameters it does not use",
            expected="every declared parameter used in the SQL as :name",
            actual=f"{', '.join(unused)} not used",
            fix="use it in the SQL, or remove its '-- param' line",
        )
    return NamedQuery(description=description, expression=statement, outputs=outputs)


class QueryCatalog:
    """The named queries a SQL data source offers.

    Args:
        queries: The loaded queries.
        dialect: The dialect the queries are written in.
    """

    def __init__(self, queries: Iterable[NamedQuery], *, dialect: str) -> None:
        self._dialect = dialect
        ordered = sorted(queries, key=lambda query: query.description.name)
        self._queries: Mapping[str, NamedQuery] = MappingProxyType(
            {query.description.name: query for query in ordered}
        )
        if len(self._queries) != len(ordered):
            raise ConfigurationError("two queries share a name")

    @classmethod
    def load(cls, directory: Path, *, dialect: str = "redshift") -> QueryCatalog:
        """Load and validate every ``*.sql`` file in ``directory``.

        Raises:
            ConfigurationError: If the directory is missing or a query is invalid.
        """
        if not directory.is_dir():
            raise ConfigurationError(
                "the queries folder does not exist",
                expected="a folder of .sql files, one named query each",
                actual=f"nothing at {directory}",
                fix=(
                    "correct queries_dir; a relative path is relative to the folder of the "
                    "service's .env file"
                ),
            )
        return cls(
            (_load_file(path, dialect) for path in sorted(directory.glob("*.sql"))), dialect=dialect
        )

    @property
    def dialect(self) -> str:
        """The dialect the queries are written in."""
        return self._dialect

    def describe(self) -> Mapping[str, QueryDescription]:
        """Return the declared interface of every query, by name."""
        return MappingProxyType({name: query.description for name, query in self._queries.items()})

    def get(self, name: str) -> NamedQuery:
        """Return the query called ``name``.

        Raises:
            ValidationFailed: If there is no such query.
        """
        try:
            return self._queries[name]
        except KeyError:
            raise ValidationFailed("unknown query; a caller may only name a loaded query") from None

    def bind(self, name: str, parameters: Mapping[str, object] | None) -> dict[str, object]:
        """Validate ``parameters`` against the query's declaration and convert them.

        Raises:
            ValidationFailed: If a parameter is unknown, missing or of the wrong type.
        """
        return bind_parameters(self.get(name).description, parameters)

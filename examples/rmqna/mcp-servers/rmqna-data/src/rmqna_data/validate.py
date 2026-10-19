"""The gate between model-written SQL and the engine.

``validate_sql`` parses the text with sqlglot (the DuckDB dialect, the engine's own), checks the
parsed statement, and returns the SQL to run, rendered from the syntax tree. It never edits the
text: a keyword inside a string literal or a comment is data, and the engine receives only what
the tree says, so spacing, comments and a second statement hidden behind a comment cannot reach it.

The rules, each a refusal with its own ``error_kind`` and a message that tells the model what to
do instead (the agent's repair loop reads these):

1. Exactly one statement, and it is a plain ``SELECT`` (``WITH ... SELECT`` and ``FROM ... SELECT``
   included). Anything else is ``not_a_select``; a second statement is ``multiple_statements``.
2. No set operation (``UNION``, ``EXCEPT``, ``INTERSECT``) anywhere in the statement: ``set_operation``.
3. Every table is one of the loaded tables, named bare. A name defined by a ``WITH`` clause is
   resolved by scope, so it is not an unknown table where it is visible and is one where it is not.
   A qualified name (``main.accounts``, ``information_schema.tables``), a quoted file name and a
   table that does not exist are ``unknown_table``.
4. No function that reads a file, the environment or the system catalog, no function in a
   ``FROM`` position at all (``read_csv``, ``glob``...), and no row or memory generator anywhere
   (``range``, ``generate_series``, ``unnest``, ``repeat``, ``list_resize``): ``forbidden_function``.
5. No PIVOT or UNPIVOT in any form: ``not_a_select``. DuckDB expands a statement-form PIVOT into
   more than one statement, one of which creates a type.
6. Structural caps on the shape of the statement (:data:`MAX_JOIN_SOURCES` and
   :data:`MAX_CTE_AND_SUBQUERY_REFERENCES`), answered as ``parse_error``: a statement that is too
   tangled to plan is refused before DuckDB's binder and optimizer, which a timeout cannot interrupt.
7. The row limit is set at the syntax tree to ``row_cap + 1``, or lowered to it, so that a result
   one row longer than the cap tells the engine the output was cut. A smaller limit and the
   ``OFFSET`` are kept.

The engine adds further layers, which do not depend on this module being right: DuckDB is told to
touch no file or network once the tables are loaded, has a memory limit, and every call has a row
cap, a byte ceiling and a timeout. None of them makes a pathological statement impossible; see the
decision record (``docs/sql-governance.md``).
"""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import Final, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError, SqlglotError
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

__all__ = [
    "MAX_CTE_AND_SUBQUERY_REFERENCES",
    "MAX_JOIN_SOURCES",
    "MAX_SQL_CHARS",
    "ErrorKind",
    "SqlRefused",
    "close_matches",
    "validate_sql",
]

ErrorKind = Literal[
    "not_a_select",
    "multiple_statements",
    "set_operation",
    "unknown_table",
    "forbidden_function",
    "parse_error",
    "execution_error",
    "timeout",
]
"""The vocabulary of a refused or failed call. The agent's prompt and the evals rely on it."""

MAX_SQL_CHARS: Final = 20_000
"""The longest statement accepted. Analytic SQL is a few hundred characters; this is a backstop."""

MAX_JOIN_SOURCES: Final = 16
"""The most sources (tables and subqueries) one SELECT may join. Analytic SQL joins a handful."""

MAX_CTE_AND_SUBQUERY_REFERENCES: Final = 32
"""The most references to a CTE, plus subqueries, in one statement.

A CTE referenced twice by each of a chain of CTEs makes a plan that grows as a power of two, and
DuckDB's binder and optimizer, unlike its executor, do not stop when interrupted.
"""

_DIALECT: Final = "duckdb"

# What a statement that is not a SELECT is called in the message. Anything not listed is named
# after its syntax-tree class, so a new kind of statement is refused rather than missed.
_STATEMENT_NAMES: Final[dict[type[exp.Expr], str]] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Merge: "MERGE",
    exp.Create: "CREATE",
    exp.Drop: "DROP",
    exp.Alter: "ALTER",
    exp.Copy: "COPY",
    exp.Pragma: "PRAGMA",
    exp.Set: "SET",
    exp.Attach: "ATTACH",
    exp.Detach: "DETACH",
    exp.Install: "INSTALL",
    exp.Use: "USE",
    exp.Describe: "DESCRIBE",
    exp.Show: "SHOW",
    exp.Summarize: "SUMMARIZE",
    exp.Values: "VALUES",
    exp.Pivot: "PIVOT",
    exp.Analyze: "ANALYZE",
    exp.Transaction: "BEGIN",
    exp.Commit: "COMMIT",
    exp.Rollback: "ROLLBACK",
}
# The same kinds, looked for inside a statement that starts as a SELECT. A PIVOT is one of them in
# every form: the operator form (FROM t PIVOT (...)) is refused with the statement form, because
# DuckDB expands a dynamic pivot into a CREATE TYPE and a SELECT.
_NESTED_STATEMENTS: Final[tuple[type[exp.Expr], ...]] = (
    *(kind for kind in _STATEMENT_NAMES if kind is not exp.Values),
    exp.Command,
)

# Functions that read a file, the environment, the engine's settings or its catalog, or that run
# another statement. DuckDB is also told to touch no file once the tables are loaded; this list is
# the first line, so the model is told why, not shown an engine permission error.
_FORBIDDEN_FUNCTIONS: Final = frozenset(
    {
        "getenv",
        "glob",
        "current_setting",
        "query",
        "query_table",
        "sniff_csv",
        "checkpoint",
        "force_checkpoint",
        "which_secret",
        "write_log",
        "enable_logging",
        "disable_logging",
        "load_extension",
        "install_extension",
        "sql_auto_complete",
    }
)
_FORBIDDEN_PREFIXES: Final = ("read_", "duckdb_", "pragma_", "parquet_")
_FORBIDDEN_SUFFIXES: Final = ("_scan",)

# Functions that make rows or memory out of a number. There is no use for one over this data, and
# the engine's memory limit does not stop them (measured: repeat('a', 3e9) and unnest(range(0, 3e9))
# both grow past 5 GB under a 1 GB limit). A table function that makes rows is also refused in FROM.
_GENERATORS: Final = frozenset(
    {
        "range",
        "generate_series",
        "generate_subscripts",
        "unnest",
        "repeat",
        "list_resize",
        "array_resize",
    }
)


class _DropAll(logging.Filter):
    """Drop every record of the logger it is attached to."""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


@contextmanager
def _sqlglot_silenced() -> Iterator[None]:
    """Keep sqlglot's own log records out of the log while a statement is read.

    sqlglot warns about a statement it cannot place (falling back to a raw command, a scope it
    cannot traverse) and the warning quotes the statement. This server's logs never carry SQL, and
    a refusal goes to the caller, not the log. The filter lives only for the one synchronous call.
    """
    quiet = _DropAll()
    logger = logging.getLogger("sqlglot")
    logger.addFilter(quiet)
    try:
        yield
    finally:
        logger.removeFilter(quiet)


class SqlRefused(Exception):
    """A statement the engine will not run, or ran and could not finish.

    The server turns it into a normal tool result, ``{"error_kind": ..., "message": ...}``, so the
    agent can read it and repair its SQL.

    Attributes:
        error_kind: What went wrong, one of :data:`ErrorKind`.
        message: What to change, written for the model that wrote the SQL.
    """

    def __init__(self, error_kind: ErrorKind, message: str) -> None:
        super().__init__(f"{error_kind}: {message}")
        self.error_kind: ErrorKind = error_kind
        self.message = message

    def to_payload(self) -> dict[str, str]:
        """Return the structured error a tool call answers with."""
        return {"error_kind": self.error_kind, "message": self.message}


def validate_sql(sql: str, tables: Collection[str], row_cap: int) -> str:
    """Return ``sql`` as the engine should run it, or refuse it.

    Args:
        sql: The statement as the model wrote it.
        tables: The loaded tables, lower case. Only these may be read.
        row_cap: The most rows a result may carry. The returned statement asks for one more, so
            that a longer result is known to have been cut.

    Returns:
        The statement rendered from its syntax tree in the DuckDB dialect, with the row limit set.

    Raises:
        SqlRefused: With the ``error_kind`` and message of the first rule the statement breaks.
    """
    with _sqlglot_silenced():
        root = _single_select(sql)
        _refuse_set_operations(root)
        _refuse_nested_statements(root)
        _refuse_functions(root)
        _refuse_too_many_joins(root)
        references = _cte_references(root)
        _refuse_too_tangled(root, references)
        _refuse_unknown_tables(root, tables, references)
        _cap_rows(root, row_cap)
        try:
            # Comments are dropped so that nothing the model wrote in one can reach the engine, and
            # an unsupported clause is dropped quietly rather than warned about.
            return root.sql(dialect=_DIALECT, comments=False, unsupported_level=ErrorLevel.IGNORE)
        except RecursionError:
            raise _too_deep() from None


def _single_select(sql: str) -> exp.Select:
    """Parse ``sql`` and return its one statement, which must be a SELECT."""
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRefused(
            "parse_error",
            f"The statement is {len(sql)} characters long; the limit is {MAX_SQL_CHARS}. "
            "Ask for less in one query.",
        )
    try:
        # An empty statement parses as None, or as a Semicolon that only carries a comment.
        statements = [
            s
            for s in sqlglot.parse(sql, read=_DIALECT)
            if s is not None and not isinstance(s, exp.Semicolon)
        ]
    except ParseError as error:
        raise SqlRefused("parse_error", _describe_parse_error(error)) from None
    except SqlglotError:
        raise SqlRefused(
            "parse_error",
            "The SQL could not be read: it has an unterminated string, quoted name or comment, "
            "or a character that is not SQL.",
        ) from None
    except RecursionError:
        raise _too_deep() from None

    if not statements:
        raise SqlRefused("parse_error", "There is no SQL statement: the input is empty.")
    if len(statements) > 1:
        raise SqlRefused(
            "multiple_statements",
            f"Send one statement per call; this input holds {len(statements)}. "
            "Run each as a separate call.",
        )

    root = statements[0]
    # A statement wrapped in parentheses, with nothing of its own, is that statement.
    while isinstance(root, exp.Subquery) and not any(
        value for key, value in root.args.items() if key != "this"
    ):
        root = root.this
    if isinstance(root, exp.SetOperation):
        raise _set_operation(root)
    if not isinstance(root, exp.Select):
        raise _not_a_select(_statement_name(root, sql))
    if root.args.get("into") is not None:
        raise _not_a_select("SELECT ... INTO")
    return root


_FIRST_WORD: Final = re.compile(r"\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*([A-Za-z]+)", re.S)


def _statement_name(statement: exp.Expr, sql: str = "") -> str:
    """Name a statement for a message: its kind if known, otherwise the word it starts with."""
    if isinstance(statement, exp.Pivot):
        return "UNPIVOT" if statement.args.get("unpivot") else "PIVOT"
    for kind, name in _STATEMENT_NAMES.items():
        if isinstance(statement, kind):
            return name
    if isinstance(statement, exp.Command):
        return str(statement.this).upper()
    # sqlglot reads some statements it has no node for as an expression (CHECKPOINT as a column,
    # TABLE accounts as an alias); what the model wrote first names it better than the node does.
    first = _FIRST_WORD.match(sql)
    return first.group(1).upper() if first else "This statement"


def _not_a_select(name: str) -> SqlRefused:
    if name in {"PIVOT", "UNPIVOT"}:
        return SqlRefused(
            "not_a_select",
            f"{name} is not supported in any form. Reshape rows into columns with conditional "
            "aggregation (SUM(CASE WHEN ... THEN ... END)) and columns into rows with a join.",
        )
    return SqlRefused(
        "not_a_select",
        f"{name} is not allowed: this tool runs one read-only SELECT statement "
        "(it may start with WITH). It cannot change data, settings or files.",
    )


def _set_operation(node: exp.Expr) -> SqlRefused:
    name = node.key.upper()
    return SqlRefused(
        "set_operation",
        f"{name} is not supported: set operations are refused. Combine the data with a join, "
        "a CTE, or conditional aggregation, or run the queries as separate calls.",
    )


def _refuse_set_operations(root: exp.Select) -> None:
    for node in root.find_all(exp.SetOperation):
        raise _set_operation(node)


def _refuse_nested_statements(root: exp.Select) -> None:
    # A SELECT that holds another kind of statement (a data-changing CTE, say) is not a plain one.
    nested = root.find(*_NESTED_STATEMENTS)
    if nested is not None:
        raise _not_a_select(_statement_name(nested))


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous | exp.AnonymousAggFunc):
        return function.name.lower()
    return function.sql_name().lower()


def _table_function_name(function: exp.Func) -> str:
    if isinstance(function, exp.GenerateSeries):
        return "range/generate_series"  # sqlglot reads both spellings as one node
    return _function_name(function)


def _forbidden(name: str) -> bool:
    return (
        name in _FORBIDDEN_FUNCTIONS
        or name.startswith(_FORBIDDEN_PREFIXES)
        or name.endswith(_FORBIDDEN_SUFFIXES)
    )


def _refuse_functions(root: exp.Select) -> None:
    # A function in a FROM position makes rows out of something that is not a loaded table: a
    # file, the catalog, a generator. Whatever its name, it is refused.
    for table in root.find_all(exp.Table):
        source = table.this
        if isinstance(source, exp.Func):
            raise _table_function(_table_function_name(source))
        if not isinstance(source, exp.Identifier):
            raise SqlRefused("unknown_table", "A table must be named by a plain name.")
    for lateral in root.find_all(exp.Lateral):
        if isinstance(lateral.this, exp.Func):
            raise _table_function(_table_function_name(lateral.this))
    for function in root.find_all(exp.Func):
        name = _function_name(function)
        if isinstance(function, exp.Unnest | exp.Explode):
            name = "unnest"
        if name in _GENERATORS or isinstance(function, exp.GenerateSeries):
            raise SqlRefused(
                "forbidden_function",
                f"The function {_generator_name(function, name)}() is not available: it makes "
                "rows or memory out of a number, and nothing over these tables needs it. "
                "Aggregate or join the loaded tables instead.",
            )
        if _forbidden(name):
            raise SqlRefused(
                "forbidden_function",
                f"The function {name}() is not available: SQL here cannot read files, the "
                "environment or the engine's settings. Query the loaded tables only.",
            )


def _generator_name(function: exp.Func, name: str) -> str:
    # sqlglot reads range() and generate_series() as one node, so name both.
    return "range/generate_series" if isinstance(function, exp.GenerateSeries) else name


def _table_function(name: str) -> SqlRefused:
    return SqlRefused(
        "forbidden_function",
        f"The table function {name}() is not available in FROM: a table here is one of the "
        "loaded tables, named by its bare name.",
    )


def _refuse_too_many_joins(root: exp.Select) -> None:
    for select in root.find_all(exp.Select):
        sources = 1 + len(select.args.get("joins") or [])
        if sources > MAX_JOIN_SOURCES:
            raise SqlRefused(
                "parse_error",
                f"This SELECT joins {sources} sources; the limit is {MAX_JOIN_SOURCES}. "
                "Join only the tables the question needs, or compute part of it in a CTE.",
            )


def _refuse_too_tangled(root: exp.Select, cte_references: set[int]) -> None:
    count = len(cte_references) + sum(1 for _ in root.find_all(exp.Subquery))
    if count > MAX_CTE_AND_SUBQUERY_REFERENCES:
        raise SqlRefused(
            "parse_error",
            f"This statement refers to CTEs and subqueries {count} times; the limit is "
            f"{MAX_CTE_AND_SUBQUERY_REFERENCES}. Reference each CTE once where you can, or split "
            "the question into separate queries.",
        )


def _refuse_unknown_tables(root: exp.Select, tables: Collection[str], ctes: set[int]) -> None:
    known = {name.lower() for name in tables}
    for table in root.find_all(exp.Table):
        if id(table) in ctes:
            continue
        name = table.name
        qualified = bool(table.db or table.catalog)
        if not qualified and name.isascii() and name.lower() in known:
            continue
        shown = ".".join(part for part in (table.catalog, table.db, name) if part)
        raise SqlRefused("unknown_table", _unknown_table_message(shown, name, known, qualified))


def _unknown_table_message(shown: str, name: str, known: set[str], qualified: bool) -> str:
    message = f"Unknown table '{shown}'."
    if qualified:
        message += " Tables are named bare (FROM accounts, not FROM crm.accounts)."
    else:
        close = close_matches(name.lower(), known)
        if close:
            message += f" Did you mean {', '.join(repr(c) for c in close)}?"
    return message + " Query only the tables listed in the schema document."


def close_matches(name: str, candidates: Collection[str]) -> list[str]:
    """Return up to three of ``candidates`` that ``name`` resembles, best first."""
    return difflib.get_close_matches(name, sorted(candidates), n=3, cutoff=0.6)


def _cte_references(root: exp.Select) -> set[int]:
    """Return ``id()`` of every table node that names a CTE visible where it is written."""
    try:
        scopes = traverse_scope(root)
    except RecursionError:
        raise _too_deep() from None
    except Exception:  # sqlglot's optimizer raises its own errors on shapes it cannot place
        raise SqlRefused(
            "parse_error", "The tables of this statement could not be worked out; simplify it."
        ) from None
    references: set[int] = set()
    for scope in scopes:
        for table in scope.tables:
            source = scope.sources.get(table.name)
            if (
                isinstance(source, Scope)
                and source.scope_type is ScopeType.CTE
                and not table.db
                and not table.catalog
            ):
                references.add(id(table))
    return references


def _cap_rows(root: exp.Select, row_cap: int) -> None:
    """Set the outermost row limit to ``row_cap + 1`` or lower, keeping any offset."""
    ceiling = row_cap + 1
    clause = root.args.get("limit")
    wanted = ceiling
    if clause is not None:
        if isinstance(clause, exp.Fetch):
            count = clause.args.get("count") or exp.Literal.number(1)
        elif isinstance(clause, exp.Limit):
            count = clause.expression
        else:  # a kind of limit this validator does not know
            raise _limit_refused()
        options = clause.args.get("limit_options")
        if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
            raise _limit_refused()
        if isinstance(count, exp.Null) or (
            isinstance(count, exp.Var) and count.name.upper() == "ALL"
        ):
            wanted = ceiling  # "no limit" means everything, so the cap decides
        elif isinstance(count, exp.Literal) and not count.is_string and count.name.isdigit():
            wanted = min(int(count.name), ceiling)
        else:
            raise _limit_refused()
    root.set("limit", exp.Limit(expression=exp.Literal.number(wanted)))


def _limit_refused() -> SqlRefused:
    return SqlRefused(
        "parse_error",
        "LIMIT must be a plain whole number of rows (for example LIMIT 20): not a percentage, "
        "WITH TIES, a negative number, a fraction or an expression. The server also caps every "
        "result, so a LIMIT is only needed to ask for fewer rows.",
    )


def _describe_parse_error(error: ParseError) -> str:
    details = error.errors[0] if error.errors else {}
    description = str(details.get("description") or "the SQL could not be parsed")
    where = ""
    if details.get("line") is not None:
        where = f" at line {details['line']}, column {details.get('col')}"
    near = f"{details.get('start_context', '')}{details.get('highlight', '')}"
    context = f" near ...{near[-40:]}" if near else ""
    return f"The SQL could not be parsed: {description}{where}{context}"


def _too_deep() -> SqlRefused:
    return SqlRefused("parse_error", "The statement is nested too deeply to check; simplify it.")

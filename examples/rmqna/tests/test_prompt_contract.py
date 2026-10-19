"""What the prompt tells the model about the server is what the server does.

The instructions quote the server's error kinds, its limits and what it refuses; the cheat-sheet
names syntax the model is told it may use. None of that may drift from the server in either
direction: a limit the prompt overstates sends the model into a refusal, and a refusal the prompt
omits is a repair the model spends twice. These tests read the server's own constants and run the
cheat-sheet's own snippets through its validator and engine.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from ai_agent_lib_core.adapters import (
    FileRegistryOptions,
    FileRegistrySource,
)
from ai_agent_lib_core.contracts.structured import model_safe_name

from rmqna.prompts import SYSTEM_VERSION, load_prompt, schema_document
from rmqna_data import engine as engine_module
from rmqna_data import validate as validate_module
from rmqna_data.engine import RmqnaEngine
from rmqna_data.settings import DATA_DIR, SCHEMA_PATH
from rmqna_data.validate import ErrorKind, SqlRefused, validate_sql

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = load_prompt("system", SYSTEM_VERSION)


@pytest.fixture(scope="module")
def engine() -> Iterator[RmqnaEngine]:
    engine = RmqnaEngine(DATA_DIR)
    try:
        yield engine
    finally:
        engine.close()


def test_the_prompt_names_every_tool_the_registry_lists() -> None:
    registry = FileRegistrySource(
        FileRegistryOptions(
            agents_path=ROOT / "registry" / "agents.yaml",
            tools_path=ROOT / "registry" / "mcp-tools.yaml",
        )
    )
    server = registry.tools.get("rmqna-data")
    assert server is not None
    assert {tool.name for tool in server.tools} == {
        "rmqna-data.execute_sql",
        "rmqna-data.search_column_values",
        "rmqna-data.get_schema",
    }
    for tool in server.tools:
        # A model API lists a tool under its model-safe name, which is what the prompt gives.
        assert model_safe_name(tool.name) in INSTRUCTIONS


def test_the_agent_and_the_server_serve_one_schema_document() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_document()


def test_every_error_kind_the_server_returns_has_a_repair_in_the_prompt() -> None:
    kinds = typing.get_args(ErrorKind)
    assert len(kinds) == 8
    repair = INSTRUCTIONS.split("### Repairing a refused or failed statement")[1].split("###")[0]
    for kind in kinds:
        assert f"\n- {kind}: " in repair, kind


def test_the_limits_the_prompt_states_are_the_servers() -> None:
    sources = validate_module.MAX_JOIN_SOURCES
    joins = validate_module.MAX_JOINS_IN_STATEMENT
    references = validate_module.MAX_CTE_AND_SUBQUERY_REFERENCES
    assert f"at most {sources} joined sources in one SELECT" in INSTRUCTIONS
    assert f"at most {joins} joins and {references} CTE and subquery references" in INSTRUCTIONS
    assert f"cut at {engine_module.DEFAULT_ROW_CAP} rows" in INSTRUCTIONS
    assert f"{engine_module.DEFAULT_MAX_RESULT_BYTES // 2**20} MB" in INSTRUCTIONS
    assert f"in {engine_module.DEFAULT_MEMORY_LIMIT.replace('GB', ' GB')} of memory" in INSTRUCTIONS
    assert f"runs {engine_module.DEFAULT_TIMEOUT_SECONDS:g} seconds" in INSTRUCTIONS
    assert f"At most {engine_module.VALUES_CAP} come back" in INSTRUCTIONS


async def test_the_two_caps_behave_as_the_prompt_says(engine: RmqnaEngine) -> None:
    # The row cap cuts the result and says so.
    cut = await engine.execute("SELECT * FROM transactions")
    assert (cut.row_count, cut.truncated) == (engine_module.DEFAULT_ROW_CAP, True)

    # The byte ceiling does not cut: it refuses, as an execution_error, with the engine's words.
    with pytest.raises(SqlRefused) as refused:
        await engine.execute("SELECT list(t) FROM transactions t")
    assert refused.value.error_kind == "execution_error"
    ceiling = f"{engine_module.DEFAULT_MAX_RESULT_BYTES // 2**20} MB"
    refusal = f"larger than {ceiling} once serialized"
    assert refusal in refused.value.message

    # The prompt says the same: where the row cap sets truncated, and that the size refusal is an
    # execution_error that is neither cut nor flagged, quoted so the model can recognise it.
    rows = next(line for line in INSTRUCTIONS.splitlines() if "cut at 200 rows" in line)
    assert f"cut at {engine_module.DEFAULT_ROW_CAP} rows (truncated is true when it is)" in rows
    size = next(line for line in INSTRUCTIONS.splitlines() if line.startswith("- Size:"))
    assert refusal in size
    assert "execution_error, never cut and never flagged truncated" in size
    repair = INSTRUCTIONS.split("### Repairing a refused or failed statement")[1]
    assert f"The result is {refusal}" in repair
    # No sentence about the 2 MB ceiling says that truncated is set by it.
    for sentence in re.split(r"(?<=[.:;])\s", INSTRUCTIONS):
        if ceiling in sentence:
            assert "truncated is true" not in sentence, sentence
    # And the remedies the engine gives are the ones the prompt gives.
    for remedy in ["fewer columns", "filter", "aggregate", "LIMIT", "list, array_agg, string_agg"]:
        assert remedy in refused.value.message, remedy
        assert remedy in size, remedy


async def test_unknown_names_are_the_error_kinds_the_prompt_says(engine: RmqnaEngine) -> None:
    async def kind_of(call: typing.Awaitable[object]) -> str:
        with pytest.raises(SqlRefused) as refused:
            await call
        return refused.value.error_kind

    # In a statement an unknown table is unknown_table but an unknown column is DuckDB's own error;
    # search_column_values says unknown_table for either.
    assert await kind_of(engine.execute("SELECT uen FROM customers")) == "unknown_table"
    assert await kind_of(engine.execute("SELECT no_such FROM accounts")) == "execution_error"
    assert await kind_of(engine.values("customers", "name", "x")) == "unknown_table"
    assert await kind_of(engine.values("accounts", "no_such", "x")) == "unknown_table"
    bullet = next(line for line in INSTRUCTIONS.splitlines() if line.startswith("- unknown_table:"))
    assert "from search_column_values, a table or column" in bullet
    assert "An unknown column inside a statement is an execution_error" in bullet


# What the cheat-sheet says is not available. Each is refused by the real validator, with the
# error kind the repair list teaches for it.
REFUSED = [
    (
        "PIVOT",
        "SELECT * FROM accounts PIVOT (count(*) FOR segment IN ('Mid-Market'))",
        "not_a_select",
    ),
    ("UNPIVOT", "SELECT * FROM accounts UNPIVOT (v FOR k IN (employees))", "not_a_select"),
    ("range", "SELECT * FROM range(10)", "forbidden_function"),
    ("generate_series", "SELECT * FROM generate_series(1, 10)", "forbidden_function"),
    ("unnest", "SELECT unnest([1, 2, 3])", "forbidden_function"),
    ("repeat", "SELECT repeat('a', 3)", "forbidden_function"),
    ("generate_subscripts", "SELECT generate_subscripts([1, 2, 3], 1)", "forbidden_function"),
    ("list_resize", "SELECT list_resize([1], 3)", "forbidden_function"),
    ("array_resize", "SELECT array_resize([1], 3)", "forbidden_function"),
    ("equi_width_bins", "SELECT equi_width_bins(0.0, 1.0, 3, false)", "forbidden_function"),
    ("rpad", "SELECT rpad(name, 40, ' ') FROM accounts", "forbidden_function"),
    ("lpad", "SELECT lpad(name, 40, ' ') FROM accounts", "forbidden_function"),
    ("printf", "SELECT printf('%d', 1)", "forbidden_function"),
    ("format", "SELECT format('{}', 1)", "forbidden_function"),
    ("UNION", "SELECT uen FROM accounts UNION SELECT uen FROM facilities", "set_operation"),
    ("EXCEPT", "SELECT uen FROM accounts EXCEPT SELECT uen FROM facilities", "set_operation"),
    ("INTERSECT", "SELECT uen FROM accounts INTERSECT SELECT uen FROM facilities", "set_operation"),
    (
        "a recursive CTE",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t",
        "set_operation",
    ),
    ("a schema-qualified name", "SELECT uen FROM crm.accounts", "unknown_table"),
    ("main.", "SELECT uen FROM main.accounts", "unknown_table"),
    ("a file function", "SELECT * FROM read_csv('data/accounts.csv')", "forbidden_function"),
    ("a second statement", "SELECT 1; SELECT 2", "multiple_statements"),
    ("a change of data", "DELETE FROM accounts", "not_a_select"),
]


@pytest.mark.parametrize(("name", "sql", "kind"), REFUSED, ids=[case[0] for case in REFUSED])
def test_what_the_prompt_says_is_unavailable_is_refused(
    engine: RmqnaEngine, name: str, sql: str, kind: str
) -> None:
    with pytest.raises(SqlRefused) as refused:
        validate_sql(sql, engine.tables, engine_module.DEFAULT_ROW_CAP)
    assert refused.value.error_kind == kind


def test_the_prompt_lists_what_it_says_is_unavailable() -> None:
    unavailable = next(
        line for line in INSTRUCTIONS.splitlines() if line.startswith("- Not available")
    )
    # The generators the prompt lists are exactly the ones the server denies: one the prompt omits
    # is a repair the model spends twice, and one it adds is a refusal that never happens. A
    # function added to the server's list fails here until the prompt names it.
    listed = unavailable.split("PIVOT and UNPIVOT; ")[1].split(";")[0]
    assert set(re.findall(r"[a-z_]+", listed)) - {"and"} == validate_module._GENERATORS
    for phrase in ["PIVOT and UNPIVOT", "UNION, EXCEPT and INTERSECT", "recursive CTEs"]:
        assert phrase in unavailable, phrase
    assert "schema-qualified names" in unavailable
    assert "FROM accounts, never FROM crm.accounts" in unavailable


# The syntax the cheat-sheet and the instructions tell the model to use. Each runs.
CHEAT_SHEET = [
    "SELECT DATE '2026-06-30' + INTERVAL 90 DAY AS d",
    "SELECT DATE '2026-06-30' - INTERVAL 3 MONTH AS d",
    "SELECT CAST(date_trunc('month', DATE '2026-06-30') AS DATE) AS d",
    "SELECT date_diff('day', DATE '2026-01-01', DATE '2026-06-30') AS days",
    "SELECT year(DATE '2026-06-30') AS y, quarter(DATE '2026-06-30') AS q",
    "SELECT last_day(DATE '2026-06-15') AS d, strftime(DATE '2026-06-30', '%Y-%m') AS ym",
    "SELECT 7 // 2 AS integer_division, 7 / 2 AS division, 7 % 2 AS remainder",
    "SELECT 1 / NULLIF(0, 0) AS guarded, COALESCE(NULL, 1) AS c, 'a' || 'b' AS text",
    "SELECT name FROM accounts WHERE name ILIKE '%jones%'",
    "SELECT naics_code // 10 AS industry FROM accounts WHERE naics_code IS NOT NULL",
    "SELECT TRY_CAST(xref_value AS INT) AS n, ROUND(1.2345, 2) AS r FROM xref LIMIT 3",
    "SELECT facility_id, arg_max(utilization_pct, as_of) AS latest FROM utilization GROUP BY ALL",
    "SELECT COUNT(*) FILTER (WHERE in_compliance = false) AS breaches FROM covenants",
    "SELECT facility_id, drawn FROM utilization "
    "QUALIFY row_number() OVER (PARTITION BY facility_id ORDER BY as_of DESC) = 1",
    "SELECT CASE WHEN utilization_pct > 80 THEN 'high' ELSE 'ok' END AS band FROM utilization LIMIT 3",
    "WITH latest AS (SELECT facility_id FROM utilization) SELECT COUNT(*) AS n FROM latest",
]


@pytest.mark.parametrize("sql", CHEAT_SHEET)
async def test_the_syntax_the_cheat_sheet_recommends_runs(engine: RmqnaEngine, sql: str) -> None:
    result = await engine.execute(sql)
    assert result.row_count >= 1
    # The names the cheat-sheet uses are in it, so a snippet cannot drift from the text unseen.
    for name in re.findall(r"\b(arg_max|date_diff|last_day|strftime|QUALIFY|FILTER|NULLIF)\b", sql):
        assert name in INSTRUCTIONS

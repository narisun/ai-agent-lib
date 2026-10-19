"""The engine over the real data: what it loads, what it caps, and what it cannot be made to do.

Every test here runs DuckDB for real on the workspace's CSV copy. The validator has its own suite;
these tests are about the engine's discipline: the tables it loads, the limits it enforces, the
lock it puts on the engine after loading, and the results it hands back.
"""

from __future__ import annotations

import asyncio
import math
import re
import shutil
import time
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from ai_agent_lib_core.contracts import ConfigurationError

from rmqna_data.engine import ExecuteResult, RmqnaEngine, ValuesResult
from rmqna_data.settings import DATA_DIR, SCHEMA_PATH
from rmqna_data.validate import SqlRefused

NOT_QUERYABLE = {"data_model", "rm_scenarios"}


@pytest.fixture(scope="module")
def engine() -> Iterator[RmqnaEngine]:
    """One engine over the real data, shared: loading the 31 tables is the slow part."""
    shared = RmqnaEngine(DATA_DIR)
    yield shared
    shared.close()


def raw_cursor(engine: RmqnaEngine) -> duckdb.DuckDBPyConnection:
    """A cursor straight on the engine's DuckDB connection: what a validator bypass would get."""
    return engine._connection.cursor()


def scalar(result: ExecuteResult) -> int:
    """The one number a single-cell result holds."""
    cell = result.rows[0][0]
    assert isinstance(cell, int)
    return cell


async def refusal(engine: RmqnaEngine, sql: str) -> SqlRefused:
    with pytest.raises(SqlRefused) as caught:
        await engine.execute(sql)
    return caught.value


def test_the_data_dir_is_the_workspace_copy_of_the_data() -> None:
    assert DATA_DIR.is_dir()
    assert DATA_DIR.name == "data"
    assert (DATA_DIR / "accounts.csv").is_file()
    assert DATA_DIR.parent.name == "rmqna"  # examples/rmqna/data, not the service folder's own
    assert SCHEMA_PATH.is_file()
    assert SCHEMA_PATH.name == "schema_v1.md"


def test_tables_are_the_31_content_csvs(engine: RmqnaEngine) -> None:
    on_disk = {path.stem for path in DATA_DIR.glob("*.csv")}

    assert len(engine.tables) == 31
    assert engine.tables == frozenset(on_disk - NOT_QUERYABLE)
    assert not NOT_QUERYABLE & engine.tables


def test_the_dictionary_and_an_answer_key_are_never_loaded_even_when_present(
    tmp_path: Path,
) -> None:
    for name in ("accounts", "data_model"):
        shutil.copy(DATA_DIR / f"{name}.csv", tmp_path / f"{name}.csv")
    # rmprep's answer key is not in the workspace's copy of the data; a stand-in shows the rule.
    (tmp_path / "rm_scenarios.csv").write_text("scenario,answer\nx,1\n", encoding="utf-8")

    small = RmqnaEngine(tmp_path)
    try:
        assert small.tables == frozenset({"accounts"})
    finally:
        small.close()


def test_a_file_that_cannot_be_a_table_name_stops_the_load(tmp_path: Path) -> None:
    (tmp_path / "Bad-Name.csv").write_text("a\n1\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="table name"):
        RmqnaEngine(tmp_path)


def test_a_missing_data_folder_stops_the_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="data folder"):
        RmqnaEngine(tmp_path / "nowhere")


async def test_execute_returns_rows_columns_and_count(engine: RmqnaEngine) -> None:
    result = await engine.execute(
        "SELECT uen, name, country FROM accounts WHERE country = 'Canada' ORDER BY uen"
    )

    assert isinstance(result, ExecuteResult)
    assert result.columns == ["uen", "name", "country"]
    assert result.row_count == len(result.rows) > 0
    assert all(row[2] == "Canada" for row in result.rows)
    assert result.rows == sorted(result.rows, key=lambda row: str(row[0]))
    assert result.truncated is False
    assert result.elapsed_ms >= 0
    assert result.to_payload() == {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": False,
        "elapsed_ms": result.elapsed_ms,
    }


async def test_an_empty_result_is_a_result_not_an_error(engine: RmqnaEngine) -> None:
    result = await engine.execute("SELECT uen FROM accounts WHERE name = 'Nobody At All Inc.'")

    assert (result.columns, result.rows, result.row_count, result.truncated) == (
        ["uen"],
        [],
        0,
        False,
    )


async def test_truncation_is_flagged_at_the_cap(engine: RmqnaEngine) -> None:
    result = await engine.execute("SELECT * FROM transactions")

    assert result.row_count == 200
    assert len(result.rows) == 200
    assert result.truncated is True


async def test_a_result_of_exactly_the_cap_is_not_truncated(engine: RmqnaEngine) -> None:
    result = await engine.execute("SELECT * FROM transactions LIMIT 200")

    assert (result.row_count, result.truncated) == (200, False)


async def test_one_row_over_the_cap_is_truncated(engine: RmqnaEngine) -> None:
    result = await engine.execute("SELECT * FROM transactions LIMIT 201")

    assert (result.row_count, result.truncated) == (200, True)


async def test_a_model_limit_above_the_cap_cannot_lift_it(engine: RmqnaEngine) -> None:
    result = await engine.execute("SELECT * FROM transactions LIMIT 100000")

    assert (result.row_count, result.truncated) == (200, True)


async def test_the_cap_is_the_engines_to_set(tmp_path: Path) -> None:
    shutil.copy(DATA_DIR / "accounts.csv", tmp_path / "accounts.csv")
    small = RmqnaEngine(tmp_path, row_cap=5)
    try:
        result = await small.execute("SELECT uen FROM accounts ORDER BY uen")
        assert (result.row_count, result.truncated) == (5, True)
    finally:
        small.close()


async def test_an_offset_pages_through_the_rows(engine: RmqnaEngine) -> None:
    first = await engine.execute("SELECT uen FROM accounts ORDER BY uen LIMIT 10")
    second = await engine.execute("SELECT uen FROM accounts ORDER BY uen LIMIT 10 OFFSET 10")

    assert not {row[0] for row in first.rows} & {row[0] for row in second.rows}
    everything = await engine.execute("SELECT uen FROM accounts ORDER BY uen")
    assert first.rows + second.rows == everything.rows[:20]


async def test_external_access_is_disabled(engine: RmqnaEngine) -> None:
    # The validator refuses a file function before DuckDB sees it...
    error = await refusal(engine, f"SELECT * FROM read_csv('{DATA_DIR / 'accounts.csv'}')")
    assert error.error_kind == "forbidden_function"

    # ...and if one ever got past it, the engine itself is locked: checked here on the raw
    # connection with no validator in between.
    raw = raw_cursor(engine)
    try:
        with pytest.raises(duckdb.Error, match="file system operations are disabled"):
            raw.execute(f"SELECT * FROM read_csv('{DATA_DIR / 'accounts.csv'}')").fetchall()
        with pytest.raises(duckdb.Error, match="file system operations are disabled"):
            raw.execute("SELECT * FROM glob('*')").fetchall()
        with pytest.raises(duckdb.Error):
            raw.execute("SET enable_external_access = true")
        with pytest.raises(duckdb.Error):
            raw.execute("INSTALL httpfs")
        with pytest.raises(duckdb.Error):
            raw.execute(f"COPY accounts TO '{DATA_DIR.parent / 'leak.csv'}'")
        with pytest.raises(duckdb.Error):
            raw.execute(f"ATTACH '{DATA_DIR.parent / 'other.db'}' AS other")
    finally:
        raw.close()
    assert not (DATA_DIR.parent / "leak.csv").exists()
    assert not (DATA_DIR.parent / "other.db").exists()


async def test_the_configuration_cannot_be_changed_after_loading(engine: RmqnaEngine) -> None:
    raw = raw_cursor(engine)
    try:
        with pytest.raises(duckdb.Error, match="configuration has been locked"):
            raw.execute("SET threads = 64")
        with pytest.raises(duckdb.Error, match="configuration has been locked"):
            raw.execute("SET enable_external_access = true")
    finally:
        raw.close()


async def test_nothing_the_model_sends_changes_the_data(engine: RmqnaEngine) -> None:
    before = await engine.execute("SELECT count(*) AS n FROM accounts")
    hostile = [
        "DELETE FROM accounts",
        "DROP TABLE accounts",
        "SELECT 1; DROP TABLE accounts",
        "SELECT * FROM accounts WHERE name = 'x'; DELETE FROM accounts",
        "UPDATE accounts SET name = 'x'",
        "CREATE TABLE leak AS SELECT * FROM accounts",
        "COPY accounts TO 'leak.csv'",
        "ATTACH 'other.db'",
        "PRAGMA enable_external_access",
        "SET enable_external_access = true",
    ]

    for sql in hostile:
        with pytest.raises(SqlRefused):
            await engine.execute(sql)

    # A comment that mentions a statement is only a comment: this is one SELECT, and it runs.
    commented = await engine.execute("SELECT 1 AS one -- ; DROP TABLE accounts")
    assert commented.rows == [[1]]
    after = await engine.execute("SELECT count(*) AS n FROM accounts")
    assert after.rows == before.rows
    assert not (DATA_DIR.parent / "leak.csv").exists()
    assert not Path("leak.csv").exists()


async def test_the_engine_asks_duckdb_itself_before_running_what_the_validator_passed(
    engine: RmqnaEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The validator's parser is sqlglot's; the engine's is DuckDB's. Should the two ever read one
    # text differently, the engine refuses what DuckDB says is not exactly one SELECT. Here the
    # validator is made to pass hostile text, as if it had been fooled.
    before = await engine.execute("SELECT count(*) AS n FROM accounts")
    cases = {
        "SELECT 1; DROP TABLE accounts": "multiple_statements",
        "SELECT 1; SELECT 2": "multiple_statements",
        "DROP TABLE accounts": "not_a_select",
        "DELETE FROM accounts": "not_a_select",
        "SET enable_external_access = true": "not_a_select",
        "CALL pragma_version()": "not_a_select",
        "PIVOT accounts ON country USING count(*)": "multiple_statements",
    }

    for hostile, kind in cases.items():
        monkeypatch.setattr("rmqna_data.engine.validate_sql", lambda *_args, h=hostile: h)
        with pytest.raises(SqlRefused) as caught:
            await engine.execute("SELECT 1")
        assert caught.value.error_kind == kind, hostile

    monkeypatch.undo()
    after = await engine.execute("SELECT count(*) AS n FROM accounts")
    assert after.rows == before.rows
    assert engine.tables == frozenset(engine.column_types)


async def test_quotes_and_backslashes_in_a_string_cannot_break_out(engine: RmqnaEngine) -> None:
    # The same strings, run by DuckDB: each is one value in one SELECT, whatever it contains.
    for value in ["east' OR '1'='1", "a\\", "a\\' ; DROP TABLE accounts; --", "'; --", "x'' ;"]:
        escaped = value.replace("'", "''")
        result = await engine.execute(f"SELECT '{escaped}' AS v")
        assert result.rows == [[value]], value
    assert scalar(await engine.execute("SELECT count(*) FROM accounts")) > 0


async def test_a_timeout_interrupts_the_query(tmp_path: Path) -> None:
    shutil.copy(DATA_DIR / "transactions.csv", tmp_path / "transactions.csv")
    quick = RmqnaEngine(tmp_path, timeout_seconds=0.5)
    try:
        started = time.perf_counter()
        error = await refusal(
            quick,
            "SELECT count(*) FROM transactions a, transactions b, transactions c, "
            "transactions d WHERE (a.amount * b.amount + c.amount) % 7 = d.amount % 5",
        )
        elapsed = time.perf_counter() - started

        assert error.error_kind == "timeout"
        assert "0.5 seconds" in error.message
        assert elapsed < 5  # interrupted, not left to run to the end of a 10^17-row join

        # The interrupted query left the engine usable.
        result = await quick.execute("SELECT count(*) AS n FROM transactions")
        assert scalar(result) > 0
    finally:
        quick.close()


async def test_a_cancelled_call_interrupts_the_query(tmp_path: Path) -> None:
    shutil.copy(DATA_DIR / "transactions.csv", tmp_path / "transactions.csv")
    patient = RmqnaEngine(tmp_path, timeout_seconds=60)
    try:
        task = asyncio.create_task(
            patient.execute(
                "SELECT count(*) FROM transactions a, transactions b, transactions c, "
                "transactions d WHERE (a.amount * b.amount + c.amount) % 7 = d.amount % 5"
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # If the query were still running, this would wait behind it.
        started = time.perf_counter()
        await patient.execute("SELECT 1")
        assert time.perf_counter() - started < 5
    finally:
        patient.close()


async def test_calls_overlap_each_on_its_own_cursor(engine: RmqnaEngine) -> None:
    results = await asyncio.gather(
        *(engine.execute(f"SELECT {n} AS n, count(*) AS c FROM accounts") for n in range(8))
    )

    assert [result.rows[0][0] for result in results] == list(range(8))
    assert len({result.rows[0][1] for result in results}) == 1


async def test_execution_errors_come_back_structured(engine: RmqnaEngine) -> None:
    error = await refusal(engine, "SELECT no_such_column FROM accounts")

    assert error.error_kind == "execution_error"
    # DuckDB's own text, verbatim: the repair loop reads the candidates it offers.
    assert 'Referenced column "no_such_column" not found' in error.message
    assert error.to_payload() == {"error_kind": "execution_error", "message": error.message}


async def test_a_type_error_names_the_functions_that_would_fit(engine: RmqnaEngine) -> None:
    error = await refusal(engine, "SELECT substr(naics_code, 1, 5) FROM accounts")

    assert error.error_kind == "execution_error"
    assert "substr" in error.message


async def test_a_refusal_never_reaches_the_engine_as_an_execution_error(
    engine: RmqnaEngine,
) -> None:
    assert (await refusal(engine, "SELECT * FROM nosuch")).error_kind == "unknown_table"
    assert (await refusal(engine, "SELECT 1 UNION SELECT 2")).error_kind == "set_operation"
    assert (await refusal(engine, "SELECT 1; SELECT 2")).error_kind == "multiple_statements"
    assert (await refusal(engine, "DROP TABLE accounts")).error_kind == "not_a_select"
    assert (await refusal(engine, "SELEC 1")).error_kind == "parse_error"


async def test_results_are_json_safe(engine: RmqnaEngine) -> None:
    result = await engine.execute(
        "SELECT DATE '2026-06-30' AS d, TIMESTAMP '2026-06-30 12:30:00' AS ts, "
        "CAST(12.50 AS DECIMAL(10, 2)) AS money, 'x'::BLOB AS b, INTERVAL 3 DAY AS iv, "
        "[1, 2] AS xs, {'a': 1} AS s, 1e400::DOUBLE AS inf, 'nan'::DOUBLE AS nan, "
        "NULL AS nothing, true AS flag, 12345678901234567890 AS big"
    )

    row = dict(zip(result.columns, result.rows[0], strict=True))
    assert row["d"] == "2026-06-30"
    assert row["ts"] == "2026-06-30T12:30:00"
    assert row["money"] == 12.5
    assert row["b"] == "78"
    assert row["iv"] == "3 days, 0:00:00"
    assert row["xs"] == [1, 2]
    assert row["s"] == {"a": 1}
    assert row["inf"] == "Infinity"
    assert row["nan"] == "NaN"
    assert row["nothing"] is None
    assert row["flag"] is True
    assert row["big"] == 12345678901234567890

    import json

    json.dumps(result.to_payload(), allow_nan=False)  # a strict JSON consumer can read it


async def test_the_columns_keep_the_names_the_query_gave(engine: RmqnaEngine) -> None:
    result = await engine.execute('SELECT 1 AS "Total Exposure", 2 AS N')

    assert result.columns == ["Total Exposure", "N"]


# --- value lookup ---------------------------------------------------------------------------


async def test_values_resolves_a_name_fragment(engine: RmqnaEngine) -> None:
    result = await engine.values("accounts", "name", "jones")

    assert isinstance(result, ValuesResult)
    assert any(value.startswith("Jones Dairy Farm") for value in result.values)
    assert result.truncated is False
    assert result.to_payload() == {"values": result.values, "truncated": False}


async def test_values_is_case_insensitive_and_sorted(engine: RmqnaEngine) -> None:
    lower = await engine.values("accounts", "name", "jones dairy")
    upper = await engine.values("ACCOUNTS", "Name", "JONES DAIRY")

    assert lower.values == upper.values == sorted(lower.values)
    assert lower.values


async def test_values_casts_non_text_columns(engine: RmqnaEngine) -> None:
    # RF5: employees is a number, naics_code a number, customer_since a date; all can be searched.
    employees = await engine.values("accounts", "employees", "00")
    assert employees.values
    assert all("00" in value for value in employees.values)

    naics = await engine.values("accounts", "naics_code", "1112")
    assert naics.values
    assert all(value.startswith("1112") or "1112" in value for value in naics.values)

    dated = await engine.values("accounts", "customer_since", "2012-08")
    assert dated.values
    assert all(value.startswith("2012-08") or "2012-08" in value for value in dated.values)


async def test_values_is_capped_and_says_so(engine: RmqnaEngine) -> None:
    result = await engine.values("transactions", "currency", "")
    assert result.truncated is False  # few currencies: all of them

    many = await engine.values("accounts", "uen", "")
    assert len(many.values) == 25
    assert many.truncated is True


async def test_values_treats_wildcards_as_text(engine: RmqnaEngine) -> None:
    assert (await engine.values("accounts", "name", "%")).values == []
    assert (await engine.values("accounts", "name", "_")).values == []
    assert (await engine.values("accounts", "name", "\\")).values == []


async def test_values_binds_the_search_as_a_parameter(engine: RmqnaEngine) -> None:
    result = await engine.values("accounts", "name", "x' OR '1'='1'; DROP TABLE accounts; --")

    assert result.values == []
    assert scalar(await engine.execute("SELECT count(*) AS n FROM accounts")) > 0


async def test_values_ignores_null_and_repeated_values(engine: RmqnaEngine) -> None:
    result = await engine.values("accounts", "country", "")

    assert result.values == ["Canada", "USA"]


async def test_values_refuses_an_unknown_table_naming_it(engine: RmqnaEngine) -> None:
    with pytest.raises(SqlRefused) as caught:
        await engine.values("customers", "name", "x")

    assert caught.value.error_kind == "unknown_table"
    assert "'customers'" in caught.value.message


async def test_values_refuses_an_unknown_column_naming_which(engine: RmqnaEngine) -> None:
    with pytest.raises(SqlRefused) as caught:
        await engine.values("accounts", "nme", "x")

    assert caught.value.error_kind == "unknown_table"
    assert "'nme'" in caught.value.message
    assert "'accounts'" in caught.value.message
    assert "name" in caught.value.message  # the near miss


async def test_values_takes_identifiers_from_the_loaded_schema_only(engine: RmqnaEngine) -> None:
    for table, column in [
        ("accounts; DROP TABLE accounts", "name"),
        ("accounts", 'name" FROM accounts; --'),
        ("information_schema.tables", "table_name"),
        ("data_model", "table"),
        ("rm_scenarios", "uen"),
    ]:
        with pytest.raises(SqlRefused) as caught:
            await engine.values(table, column, "x")
        assert caught.value.error_kind == "unknown_table", (table, column)


# --- the engine and the schema document -----------------------------------------------------


def _documented_columns() -> dict[str, list[tuple[str, str]]]:
    """Each table's columns as the committed schema document states them: (name, type)."""
    document = SCHEMA_PATH.read_text(encoding="utf-8")
    tables: dict[str, list[tuple[str, str]]] = {}
    for block in re.finditer(r"^### (\w+)\n(.*?)(?=^#{2,4} |\Z)", document, re.S | re.M):
        table = block.group(1)
        tables[table] = [
            (column.group(1), column.group(2))
            for column in re.finditer(
                r"^- (\w+) ([A-Z][A-Z0-9_]*(?:\(\d+(?:,\s*\d+)?\))?)(?= )", block.group(2), re.M
            )
        ]
    return tables


def test_the_engine_loads_every_column_with_the_type_the_schema_document_states(
    engine: RmqnaEngine,
) -> None:
    # The model writes SQL from the document, so a column the engine types differently (a
    # NAICS code the dictionary calls a string and DuckDB loads as BIGINT) is a failure waiting
    # to happen. The document was rendered from loaded types; this holds the engine to it.
    documented = _documented_columns()

    assert set(documented) == set(engine.tables)
    mismatches = []
    for table, columns in documented.items():
        loaded = list(engine.column_types[table].items())
        if columns != loaded:
            mismatches.append((table, columns, loaded))
    assert not mismatches
    assert sum(len(columns) for columns in documented.values()) == 301


def test_the_naics_code_is_the_number_the_document_says_it_is(engine: RmqnaEngine) -> None:
    assert engine.column_types["accounts"]["naics_code"] == "BIGINT"
    assert engine.column_types["accounts"]["sic_code"] == "VARCHAR"
    assert engine.column_types["utilization"]["as_of"] == "DATE"


# --- the validator rewrites the statement; the answer must not change -----------------------

CORPUS = [
    "SELECT uen, name FROM accounts ORDER BY uen",
    "SELECT country, count(*) AS n FROM accounts GROUP BY country ORDER BY country",
    "SELECT country, count(*) AS n FROM accounts GROUP BY ALL ORDER BY ALL",
    "SELECT date_trunc('year', customer_since) AS y, count(*) AS n FROM accounts "
    "GROUP BY 1 ORDER BY 1",
    "SELECT strftime(customer_since, '%Y-%m') AS m, count(*) FROM accounts GROUP BY 1 ORDER BY 1",
    "SELECT * EXCLUDE (business_description, locations) FROM accounts ORDER BY uen",
    "SELECT uen, name, row_number() OVER (PARTITION BY country ORDER BY name) AS rn "
    "FROM accounts QUALIFY rn <= 3 ORDER BY country, rn",
    "FROM accounts SELECT uen, annual_revenue WHERE annual_revenue > 1e8 ORDER BY uen",
    "SELECT a.uen, sum(f.our_hold) AS exposure FROM accounts a JOIN facilities f USING (uen) "
    "GROUP BY a.uen ORDER BY exposure DESC, a.uen",
    "WITH latest AS (SELECT facility_id, max(as_of) AS as_of FROM utilization GROUP BY 1) "
    "SELECT u.facility_id, u.utilization_pct FROM utilization u JOIN latest l "
    "ON l.facility_id = u.facility_id AND l.as_of = u.as_of ORDER BY u.facility_id",
    "SELECT naics_code // 10 AS industry, count(*) FROM accounts GROUP BY 1 ORDER BY 1",
    "SELECT uen::VARCHAR AS u, annual_revenue::BIGINT AS r FROM accounts ORDER BY uen",
    "SELECT try_cast(xref_value AS INT) AS v, xref_name FROM xref "
    "WHERE xref_type = 'risk_rating' AND try_cast(xref_value AS INT) IS NOT NULL ORDER BY v",
    "SELECT name FROM accounts WHERE name ILIKE '%farm%' ORDER BY name",
    "SELECT name, customer_since + INTERVAL 30 DAY AS d FROM accounts ORDER BY name LIMIT 5",
    "SELECT date_diff('day', DATE '2026-01-01', DATE '2026-06-30') AS days",
    "SELECT count(*) FILTER (WHERE country = 'USA') AS usa, count(*) AS total FROM accounts",
    "SELECT arg_max(name, annual_revenue) AS biggest FROM accounts",
    "SELECT list(DISTINCT country ORDER BY country) AS countries FROM accounts",
    "SELECT segment, median(annual_revenue) AS m FROM accounts GROUP BY segment ORDER BY segment",
    "SELECT CASE WHEN employees > 1000 THEN 'large' ELSE 'small' END AS size, count(*) "
    "FROM accounts GROUP BY 1 ORDER BY 1",
    "SELECT coalesce(connected_uen, 'none') AS c FROM accounts ORDER BY uen LIMIT 3",
    "SELECT uen FROM accounts WHERE uen IN (SELECT uen FROM contacts) ORDER BY uen",
    "SELECT uen FROM accounts a WHERE EXISTS (SELECT 1 FROM contacts c WHERE c.uen = a.uen) "
    "ORDER BY uen",
    "SELECT string_split(name, ' ')[1] AS first_word FROM accounts ORDER BY uen LIMIT 5",
    "SELECT regexp_extract(name, '^(\\w+)', 1) AS w FROM accounts ORDER BY uen LIMIT 5",
    "SELECT * FROM accounts a LEFT JOIN LATERAL (SELECT count(*) AS n FROM contacts c "
    "WHERE c.uen = a.uen) k ON true ORDER BY a.uen LIMIT 5",
    "SELECT round(avg(annual_revenue), 2) AS r, stddev_pop(annual_revenue) AS s FROM accounts",
    # The joins the schema document itself teaches: they must run as written.
    "SELECT m.uen, x.xref_value FROM credit_metrics m JOIN xref x "
    "ON x.xref_type = 'risk_rating' AND x.xref_name = m.risk_rating "
    "AND TRY_CAST(x.xref_value AS INT) IS NOT NULL ORDER BY m.uen",
    "SELECT a.uen, b.sector FROM accounts a JOIN benchmarks b "
    "ON a.naics_code // 10 = b.naics_industry ORDER BY a.uen, b.sector",
    # More of the DuckDB a model will write.
    "SELECT * FROM accounts PIVOT (count(*) FOR country IN ('USA', 'Canada')) ORDER BY ALL",
    "SELECT country, segment, count(*) FROM accounts GROUP BY ROLLUP (country, segment) "
    "ORDER BY ALL",
    "SELECT country, segment, count(*) FROM accounts "
    "GROUP BY GROUPING SETS ((country), (segment), ()) ORDER BY ALL",
    "SELECT uen, sum(annual_revenue) OVER (PARTITION BY country ORDER BY uen "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running FROM accounts ORDER BY uen",
    "SELECT list_transform([1, 2, 3], x -> x * 2) AS doubled",
    "SELECT a.uen FROM accounts a SEMI JOIN contacts c ON a.uen = c.uen ORDER BY a.uen",
    "SELECT a.uen FROM accounts a ANTI JOIN contacts c ON a.uen = c.uen ORDER BY a.uen",
    "SELECT DISTINCT ON (country) country, name FROM accounts ORDER BY country, name",
    "SELECT COLUMNS('.*_pct') FROM utilization ORDER BY ALL LIMIT 5",
    "SELECT * REPLACE (upper(name) AS name) FROM accounts ORDER BY uen LIMIT 3",
]


@pytest.fixture(scope="module")
def reference() -> Iterator[duckdb.DuckDBPyConnection]:
    """An independent DuckDB over the same CSVs, for what the queries should answer."""
    connection = duckdb.connect(":memory:")
    for path in sorted(DATA_DIR.glob("*.csv")):
        if path.stem not in NOT_QUERYABLE:
            connection.read_csv(str(path)).create(path.stem)
    yield connection
    connection.close()


def _same(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left):
        return math.isnan(right)
    return left == right


@pytest.mark.parametrize("sql", CORPUS)
async def test_the_engine_answers_what_duckdb_answers_for_the_models_own_sql(
    engine: RmqnaEngine, reference: duckdb.DuckDBPyConnection, sql: str
) -> None:
    expected = reference.execute(f"SELECT * FROM ({sql}) LIMIT 200").fetchall()

    got = await engine.execute(sql)

    assert len(got.rows) == len(expected), sql
    for got_row, expected_row in zip(got.rows, expected, strict=True):
        assert len(got_row) == len(expected_row)
        for got_cell, expected_cell in zip(got_row, expected_row, strict=True):
            normalised = _plain(expected_cell)
            assert _same(got_cell, normalised) or str(got_cell) == str(normalised), (
                sql,
                got_cell,
                normalised,
            )


def _plain(value: object) -> object:
    """What the engine's JSON-safe conversion makes of a Python value DuckDB returned."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value

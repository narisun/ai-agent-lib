"""The engine over the real data: what it loads, what it caps, and what it cannot be made to do.

Every test here runs DuckDB for real on the workspace's CSV copy. The validator has its own suite;
these tests are about the engine's discipline: the tables it loads, the limits it enforces, the
lock it puts on the engine after loading, and the results it hands back.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from ai_agent_lib_core.contracts import ConfigurationError

from rmqna_data import engine as engine_module
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


# A 6-way cross join of the 46 clients, hashing each combination: about 9 billion rows, so it never
# finishes, yet it streams in a few megabytes (the engine has no temp directory, so a statement that
# had to materialize its join would fail at the memory limit instead of running), and its executor
# checks for an interrupt every few thousand rows.
TORTURE = (
    "SELECT sum(length(md5(a.uen || b.uen || c.uen || d.uen || e.uen || f.uen))) "
    "FROM accounts a, accounts b, accounts c, accounts d, accounts e, accounts f"
)


async def settled(engine: RmqnaEngine, within: float) -> bool:
    """Wait until no worker thread is executing a statement; say whether that happened in time."""
    deadline = time.perf_counter() + within
    while engine.running and time.perf_counter() < deadline:
        await asyncio.sleep(0.02)
    return engine.running == 0


@pytest.fixture
def accounts_only(tmp_path: Path) -> Path:
    shutil.copy(DATA_DIR / "accounts.csv", tmp_path / "accounts.csv")
    return tmp_path


async def test_a_timeout_interrupts_the_query(accounts_only: Path) -> None:
    quick = RmqnaEngine(accounts_only, timeout_seconds=0.5)
    try:
        started = time.perf_counter()
        error = await refusal(quick, TORTURE)
        elapsed = time.perf_counter() - started

        assert error.error_kind == "timeout"
        assert "0.5 seconds" in error.message
        assert elapsed < 5  # the caller was answered, not left to wait for the join

        # The point of the interrupt: the worker thread has really finished, soon after the
        # caller was told. (Without the interrupt it would still be joining 10^17 row pairs.)
        assert await settled(quick, within=3)
        assert quick.running == 0

        # And the interrupted query left the engine usable.
        result = await quick.execute("SELECT count(*) AS n FROM accounts")
        assert scalar(result) > 0
    finally:
        quick.close()


async def test_a_cancelled_call_interrupts_the_query(accounts_only: Path) -> None:
    patient = RmqnaEngine(accounts_only, timeout_seconds=60)
    try:
        task = asyncio.create_task(patient.execute(TORTURE))
        await asyncio.sleep(0.3)
        assert patient.running == 1  # it is really executing
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await settled(patient, within=3)
        assert (await patient.execute("SELECT 1")).rows == [[1]]
    finally:
        patient.close()


async def test_the_interrupt_tests_can_fail_when_nothing_is_interrupted(
    accounts_only: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A test that passes whatever the code does proves nothing. Take the interrupt away and the
    # worker keeps going after the caller was answered: the same check the two tests above make
    # (settled) now says no. The cursors are kept so that the test can stop the worker itself.
    cursors: list[duckdb.DuckDBPyConnection] = []
    monkeypatch.setattr("rmqna_data.engine._interrupt", cursors.append)
    quick = RmqnaEngine(accounts_only, timeout_seconds=0.3)
    try:
        error = await refusal(quick, TORTURE)
        assert error.error_kind == "timeout"

        assert not await settled(quick, within=1.0)
        assert quick.running == 1
    finally:
        for cursor in cursors:
            cursor.interrupt()
        assert await settled(quick, within=3)
        quick.close()


async def test_the_runaways_the_timeout_cannot_stop_never_reach_duckdb(
    engine: RmqnaEngine,
) -> None:
    # Each of these ran for 20 seconds or more and took 5 to 12 GB after the timeout had fired:
    # DuckDB's binder, optimizer and one huge scalar do not honour an interrupt. They are refused
    # before DuckDB is asked, so no worker thread is ever started for them.
    chain = ", ".join(f"c{i} AS (SELECT a.x FROM c{i - 1} a, c{i - 1} b)" for i in range(1, 27))
    runaways = [
        ("forbidden_function", "SELECT sum(x) FROM (SELECT unnest(range(0, 3000000000)) AS x)"),
        (
            "parse_error",
            "SELECT count(*) FROM accounts a1 "
            + " ".join(f"JOIN accounts a{i} ON a{i}.uen = a1.uen" for i in range(2, 401)),
        ),
        ("parse_error", f"WITH c0 AS (SELECT 1 AS x), {chain} SELECT count(*) FROM c26"),
    ]

    started = time.perf_counter()
    for kind, sql in runaways:
        error = await refusal(engine, sql)
        assert error.error_kind == kind
        assert engine.running == 0

    assert time.perf_counter() - started < 1


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


async def test_a_timestamp_with_a_time_zone_is_returned_not_an_import_error(
    engine: RmqnaEngine,
) -> None:
    # DuckDB needs pytz to hand a TIMESTAMPTZ to Python; without it SELECT now() failed.
    result = await engine.execute(
        "SELECT now() AS n, current_timestamp AS c, TIMESTAMPTZ '2026-06-30 12:00:00+00' AS fixed"
    )

    row = dict(zip(result.columns, result.rows[0], strict=True))
    assert row["fixed"] == "2026-06-30T12:00:00+00:00"  # UTC, whatever the machine's zone is
    assert isinstance(row["n"], str)
    assert row["n"].endswith("+00:00")
    assert isinstance(row["c"], str)
    json.dumps(result.to_payload(), allow_nan=False)


async def test_a_result_that_is_one_huge_row_is_refused_by_size_not_returned(
    engine: RmqnaEngine,
) -> None:
    # One row, 30,771 rows' worth: the row cap cannot see it, the byte ceiling can.
    started = time.perf_counter()
    error = await refusal(engine, "SELECT list(t) FROM transactions t")
    elapsed = time.perf_counter() - started

    assert error.error_kind == "execution_error"
    assert "larger than 2 MB" in error.message
    assert "Narrow the selection" in error.message
    assert elapsed < 10
    assert await settled(engine, within=3)


async def test_the_byte_ceiling_is_the_engines_to_set(tmp_path: Path) -> None:
    shutil.copy(DATA_DIR / "accounts.csv", tmp_path / "accounts.csv")
    tight = RmqnaEngine(tmp_path, max_result_bytes=2_000)
    try:
        assert (await tight.execute("SELECT uen FROM accounts LIMIT 5")).row_count == 5
        error = await refusal(tight, "SELECT * FROM accounts")
        assert error.error_kind == "execution_error"
        assert "larger than 0.00190735 MB" in error.message
    finally:
        tight.close()


async def test_the_byte_ceiling_counts_what_the_json_weighs_not_characters(
    tmp_path: Path,
) -> None:
    # A control character is one character and six bytes of JSON (\\u0001), so a string of them
    # weighs six times what a count of its characters says. In review, 1.9 million of them passed
    # a 2 MB ceiling counted in characters and serialized to about 11 MB.
    for name in ("accounts", "contacts"):
        shutil.copy(DATA_DIR / f"{name}.csv", tmp_path / f"{name}.csv")
    tight = RmqnaEngine(tmp_path, max_result_bytes=20_000)
    try:
        # 5,474 rows from the cross join: 5,474 characters either way, 32,844 bytes as escapes.
        plain = await tight.execute("SELECT string_agg('a', '') AS s FROM accounts a, contacts c")
        assert len(str(plain.rows[0][0])) == 5474

        # Six bytes each as an escape (a control character; a non-ASCII one, in the escaped form
        # that is the most it can weigh): over the ceiling.
        for text in ("chr(1)", "chr(233)"):
            error = await refusal(
                tight, f"SELECT string_agg({text}, '') FROM accounts a, contacts c"
            )
            assert error.error_kind == "execution_error", text
            assert "larger than" in error.message, text
        # Two bytes each (a newline is \\n): 10,948 bytes, under it.
        newlines = await tight.execute("SELECT string_agg(chr(10), '') FROM accounts a, contacts c")
        assert len(str(newlines.rows[0][0])) == 5474
    finally:
        tight.close()


def test_what_the_budget_spends_is_never_less_than_what_the_json_weighs() -> None:
    values: list[object] = [
        None,
        True,
        12345678901234567890,
        1.5,
        float("nan"),
        Decimal("12.50"),
        "plain",
        'quote" and \\ backslash and \n newline',
        "\x01\x02\x1f control",
        "caf\u00e9 \u2014 \U0001f600",
        date(2026, 6, 30),
        datetime(2026, 6, 30, 12, 30),
        [1, "two", None, [3.5, "\x01"]],
        {"k\x01": [1, 2], "plain": {"nested": "\u00e9"}},
        b"\x00\xff",
    ]
    for value in values:
        budget = engine_module._Budget(1_000_000)
        converted = engine_module._json_safe(value, budget)
        spent = 1_000_000 - budget.left
        weight = len(json.dumps(converted, separators=(",", ":"), allow_nan=False)) + 1  # + comma
        assert spent >= weight, (value, spent, weight)


async def test_an_ordinary_wide_result_is_far_under_the_byte_ceiling(engine: RmqnaEngine) -> None:
    for table in sorted(engine.tables):
        result = await engine.execute(f"SELECT * FROM {table}")
        assert result.row_count > 0, table
        assert len(json.dumps(result.to_payload(), default=str)) < 2 * 1024 * 1024, table


async def test_rows_are_converted_in_the_worker_thread_not_on_the_event_loop(
    engine: RmqnaEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: set[int] = set()
    original = engine_module._json_safe

    def recording(value: object, budget: object) -> object:
        threads.add(threading.get_ident())
        return original(value, budget)  # type: ignore[arg-type]

    monkeypatch.setattr(engine_module, "_json_safe", recording)

    await engine.execute("SELECT uen, name FROM accounts LIMIT 3")

    assert threads
    assert threading.get_ident() not in threads


async def test_the_session_time_zone_is_utc_so_answers_do_not_depend_on_the_machine(
    engine: RmqnaEngine,
) -> None:
    raw = raw_cursor(engine)
    try:
        assert raw.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)
    finally:
        raw.close()


async def test_the_memory_limit_is_set_and_locked(engine: RmqnaEngine) -> None:
    raw = raw_cursor(engine)
    try:
        assert raw.execute("SELECT current_setting('memory_limit')").fetchone() == ("953.6 MiB",)
        with pytest.raises(duckdb.Error, match="configuration has been locked"):
            raw.execute("SET memory_limit = '64GB'")
    finally:
        raw.close()


async def test_nothing_is_written_to_disk_when_a_statement_does_not_fit_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DuckDB spills what does not fit in memory to a .tmp folder in the working directory, bounded
    # only by free disk space: this shape (a window function over a cross join) wrote 3.6 GB in
    # 8 seconds in review. With no temp directory it fails at the memory limit instead.
    work = tmp_path / "cwd"
    data = tmp_path / "data"
    work.mkdir()
    data.mkdir()
    shutil.copy(DATA_DIR / "transactions.csv", data / "transactions.csv")
    monkeypatch.chdir(work)
    small = RmqnaEngine(data, memory_limit="256MB", timeout_seconds=5)
    try:
        error = await refusal(
            small,
            "SELECT max(rn) FROM (SELECT row_number() OVER (ORDER BY a.amount, b.amount) AS rn "
            "FROM transactions a, transactions b)",
        )

        assert error.error_kind == "execution_error"  # not a timeout: it failed at the limit
        assert "Out of Memory" in error.message
        assert list(work.iterdir()) == []  # no .tmp, nothing at all
        assert await settled(small, within=3)
        assert (await small.execute("SELECT 1")).rows == [[1]]
    finally:
        small.close()


async def test_the_temp_directory_is_empty_and_locked(engine: RmqnaEngine) -> None:
    raw = raw_cursor(engine)
    try:
        assert raw.execute("SELECT current_setting('temp_directory')").fetchone() == ("",)
        with pytest.raises(duckdb.Error, match="configuration has been locked"):
            raw.execute("SET temp_directory = '/tmp/spill'")
    finally:
        raw.close()


async def test_a_statement_that_runs_out_of_memory_comes_back_as_an_error(
    tmp_path: Path,
) -> None:
    # Doubling a string 30 times asks for gigabytes from a scalar; the limit stops it cleanly.
    shutil.copy(DATA_DIR / "accounts.csv", tmp_path / "accounts.csv")
    small = RmqnaEngine(tmp_path, memory_limit="256MB")
    try:
        chain = ", ".join(f"c{i} AS (SELECT x || x AS x FROM c{i - 1})" for i in range(1, 29))
        error = await refusal(
            small, f"WITH c0 AS (SELECT 'abcdefgh' AS x), {chain} SELECT length(x) FROM c28"
        )

        assert error.error_kind == "execution_error"
        assert "Out of Memory" in error.message
        assert await settled(small, within=3)
        assert (await small.execute("SELECT 1")).rows == [[1]]
    finally:
        small.close()


def test_a_memory_limit_that_is_not_a_size_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="memory_limit"):
        RmqnaEngine(tmp_path, memory_limit="1GB'; DROP TABLE accounts; --")


async def test_running_counts_workers_and_returns_to_zero(engine: RmqnaEngine) -> None:
    assert engine.running == 0
    await asyncio.gather(*(engine.execute("SELECT count(*) FROM accounts") for _ in range(4)))
    assert await settled(engine, within=3)


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

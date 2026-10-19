"""The containment contract: what the validator lets through, and what it refuses.

The validator is the only gate between model-written SQL and the engine, so these tests are the
specification of that gate. Each refusal names its ``error_kind`` (the vocabulary the agent's
repair loop reads) and says what the model should do instead. The validator works on the parsed
statement, never on the text, so a keyword inside a string or a comment is not a statement.
"""

from __future__ import annotations

import contextlib
import logging

import pytest
import sqlglot
from sqlglot import exp

from rmqna_data.validate import MAX_SQL_CHARS, SqlRefused, validate_sql

TABLES = frozenset({"accounts", "contacts", "facilities", "utilization", "xref"})
CAP = 200


def accepted(sql: str) -> str:
    """The rewritten statement the engine would run."""
    return validate_sql(sql, TABLES, CAP)


def refused(sql: str) -> SqlRefused:
    with pytest.raises(SqlRefused) as caught:
        validate_sql(sql, TABLES, CAP)
    return caught.value


def limit_of(sql: str) -> int:
    """The row limit of a rewritten statement, read back from its syntax tree."""
    limit = sqlglot.parse_one(sql, read="duckdb").args["limit"]
    return int(limit.expression.name)


def offset_of(sql: str) -> int:
    offset = sqlglot.parse_one(sql, read="duckdb").args["offset"]
    return int(offset.expression.name)


def test_a_plain_select_passes_and_gains_the_limit() -> None:
    rewritten = accepted("SELECT uen, name FROM accounts WHERE country = 'USA' ORDER BY name")

    assert limit_of(rewritten) == CAP + 1
    # Everything the model wrote is still there; only the limit was added.
    assert "WHERE country = 'USA'" in rewritten
    assert "ORDER BY name" in rewritten


def test_a_cte_passes_and_its_alias_is_not_an_unknown_table() -> None:
    rewritten = accepted(
        "WITH big AS (SELECT uen FROM facilities WHERE commitment > 1000000), "
        "named AS (SELECT a.name FROM accounts a JOIN big USING (uen)) "
        "SELECT * FROM named JOIN big ON 1 = 1"
    )

    assert limit_of(rewritten) == CAP + 1


def test_a_cte_alias_is_not_a_table_where_the_cte_cannot_be_seen() -> None:
    # The CTE x lives inside the first subquery; the second x names no table that exists.
    error = refused("SELECT * FROM (WITH x AS (SELECT 1 AS a) SELECT * FROM x) AS q, x")

    assert error.error_kind == "unknown_table"
    assert "'x'" in error.message


def test_a_cte_may_be_referenced_more_than_once_and_under_an_alias() -> None:
    rewritten = accepted(
        "WITH big AS (SELECT uen FROM facilities) "
        "SELECT b1.uen FROM big AS b1 JOIN big b2 ON b1.uen = b2.uen "
        "WHERE b1.uen IN (SELECT uen FROM big)"
    )

    assert limit_of(rewritten) == CAP + 1


def test_a_cte_may_shadow_a_loaded_table_name() -> None:
    rewritten = accepted("WITH accounts AS (SELECT uen FROM contacts) SELECT * FROM accounts")

    assert limit_of(rewritten) == CAP + 1


def test_a_cte_body_is_checked_like_any_other_query() -> None:
    error = refused("WITH x AS (SELECT * FROM secrets) SELECT * FROM x")

    assert error.error_kind == "unknown_table"
    assert "secrets" in error.message


def test_a_trailing_semicolon_is_tolerated() -> None:
    assert limit_of(accepted("SELECT 1;")) == CAP + 1
    assert limit_of(accepted("SELECT 1 ;  ;")) == CAP + 1


def test_a_comment_after_the_statement_is_not_a_second_statement() -> None:
    assert limit_of(accepted("SELECT 1; -- a note for the reader")) == CAP + 1
    assert limit_of(accepted("SELECT 1; /* closing remark */")) == CAP + 1
    assert limit_of(accepted("-- what this does\nSELECT 1;\n-- end")) == CAP + 1


def test_two_statements_are_refused() -> None:
    error = refused("SELECT 1; DROP TABLE accounts")

    assert error.error_kind == "multiple_statements"
    assert "one statement" in error.message


def test_a_second_statement_hidden_behind_a_comment_is_still_a_second_statement() -> None:
    # The comment closes before the semicolon; the DROP is a statement, not part of a comment.
    assert refused("SELECT 1 /* harmless */; DROP TABLE accounts").error_kind == (
        "multiple_statements"
    )
    assert refused("SELECT 1; -- done\nDELETE FROM accounts").error_kind == "multiple_statements"


def test_text_after_a_line_comment_marker_is_a_comment_not_a_statement() -> None:
    # What follows -- is a comment, so this is one SELECT. Rendering must not let the text
    # escape: the comment is dropped, and nothing of it reaches the engine.
    rewritten = accepted("SELECT 1 -- */ ; DROP TABLE accounts")

    assert "DROP" not in rewritten
    assert ";" not in rewritten
    assert "--" not in rewritten
    assert "/*" not in rewritten


def test_a_block_comment_that_tries_to_end_early_cannot_open_a_second_statement() -> None:
    rewritten = accepted("SELECT 1 /* a */ + /* b */ 2 /* ; DROP TABLE accounts */")

    assert "DROP" not in rewritten
    assert "/*" not in rewritten


@pytest.mark.parametrize(
    ("sql", "kind_word"),
    [
        ("INSERT INTO accounts VALUES (1)", "INSERT"),
        ("UPDATE accounts SET name = 'x'", "UPDATE"),
        ("DELETE FROM accounts", "DELETE"),
        ("CREATE TABLE t AS SELECT 1", "CREATE"),
        ("CREATE VIEW v AS SELECT 1", "CREATE"),
        ("DROP TABLE accounts", "DROP"),
        ("ALTER TABLE accounts ADD COLUMN x INT", "ALTER"),
        (
            "MERGE INTO accounts USING contacts ON accounts.uen = contacts.uen "
            "WHEN MATCHED THEN DELETE",
            "MERGE",
        ),
        ("WITH x AS (SELECT 1) INSERT INTO accounts SELECT * FROM x", "INSERT"),
        ("WITH x AS (SELECT 1) DELETE FROM accounts", "DELETE"),
    ],
)
def test_each_write_and_ddl_statement_kind_is_refused(sql: str, kind_word: str) -> None:
    error = refused(sql)

    assert error.error_kind == "not_a_select"
    assert kind_word in error.message
    assert "SELECT" in error.message  # and says what is allowed instead


@pytest.mark.parametrize(
    ("sql", "kind_word"),
    [
        ("PRAGMA database_list", "PRAGMA"),
        ("ATTACH 'other.db' AS other", "ATTACH"),
        ("DETACH other", "DETACH"),
        ("COPY accounts TO 'out.csv'", "COPY"),
        ("INSTALL httpfs", "INSTALL"),
        ("LOAD httpfs", "LOAD"),
        ("SET enable_external_access = true", "SET"),
        ("CALL pragma_version()", "CALL"),
        ("USE other", "USE"),
        ("DESCRIBE accounts", "DESCRIBE"),
        ("SHOW TABLES", "SHOW"),
        ("SUMMARIZE accounts", "SUMMARIZE"),
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("VALUES (1), (2)", "VALUES"),
        ("BEGIN", "BEGIN"),
        ("COMMIT", "COMMIT"),
        ("SELECT * INTO copy_of_accounts FROM accounts", "INTO"),
    ],
)
def test_pragma_attach_copy_install_load_set_are_refused(sql: str, kind_word: str) -> None:
    error = refused(sql)

    assert error.error_kind == "not_a_select"
    assert kind_word in error.message


@pytest.mark.parametrize(
    ("sql", "operation"),
    [
        ("SELECT uen FROM accounts UNION SELECT uen FROM contacts", "UNION"),
        ("SELECT uen FROM accounts UNION ALL SELECT uen FROM contacts", "UNION"),
        ("SELECT uen FROM accounts EXCEPT SELECT uen FROM contacts", "EXCEPT"),
        ("SELECT uen FROM accounts INTERSECT SELECT uen FROM contacts", "INTERSECT"),
        ("(SELECT 1) UNION (SELECT 2)", "UNION"),
        # Nested ones are refused too: a CTE would otherwise get the same thing past the check.
        ("WITH u AS (SELECT 1 AS n UNION ALL SELECT 2) SELECT * FROM u", "UNION"),
        (
            "SELECT * FROM accounts WHERE uen IN (SELECT uen FROM a EXCEPT SELECT uen FROM b)",
            "EXCEPT",
        ),
    ],
)
def test_a_set_operation_is_refused_naming_it(sql: str, operation: str) -> None:
    error = refused(sql)

    assert error.error_kind == "set_operation"
    assert operation in error.message


def test_an_unknown_table_is_refused_naming_it() -> None:
    error = refused("SELECT * FROM customers")

    assert error.error_kind == "unknown_table"
    assert "'customers'" in error.message


def test_an_unknown_table_in_a_join_a_subquery_or_a_scalar_is_found() -> None:
    for sql in [
        "SELECT * FROM accounts a JOIN nosuch n ON n.uen = a.uen",
        "SELECT * FROM accounts WHERE uen IN (SELECT uen FROM nosuch)",
        "SELECT (SELECT max(x) FROM nosuch) FROM accounts",
        "SELECT * FROM accounts WHERE EXISTS (SELECT 1 FROM nosuch)",
        "SELECT * FROM (SELECT * FROM nosuch) q",
        "SELECT * FROM accounts, nosuch",
    ]:
        error = refused(sql)
        assert error.error_kind == "unknown_table", sql
        assert "nosuch" in error.message, sql


def test_a_near_miss_suggests_the_table_it_resembles() -> None:
    error = refused("SELECT * FROM account")

    assert error.error_kind == "unknown_table"
    assert "accounts" in error.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM crm.accounts",
        "SELECT * FROM main.accounts",
        "SELECT * FROM memory.main.accounts",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM duckdb_tables",
        "SELECT * FROM 'accounts.csv'",
        'SELECT * FROM "../data/accounts.csv"',
    ],
)
def test_a_qualified_or_catalog_or_file_name_is_not_a_table(sql: str) -> None:
    # Tables are named bare, as the schema document says; a schema prefix, the system catalog
    # and a quoted file name all fail the same check.
    assert refused(sql).error_kind == "unknown_table"


def test_table_names_match_without_regard_to_case() -> None:
    assert limit_of(accepted("SELECT * FROM ACCOUNTS")) == CAP + 1
    assert limit_of(accepted('SELECT * FROM "Accounts"')) == CAP + 1


def test_a_look_alike_of_a_table_name_is_not_that_table() -> None:
    # U+212A (Kelvin sign) lower-cases to ASCII k; DuckDB would not treat it as the ASCII name.
    error = refused(f'SELECT * FROM "{chr(0x212A)}ontacts"')

    assert error.error_kind == "unknown_table"


@pytest.mark.parametrize(
    ("sql", "function"),
    [
        ("SELECT * FROM read_csv('accounts.csv')", "read_csv"),
        ("SELECT * FROM read_csv_auto('accounts.csv')", "read_csv_auto"),
        ("SELECT * FROM read_parquet('x.parquet')", "read_parquet"),
        ("SELECT * FROM parquet_scan('x.parquet')", "parquet_scan"),
        ("SELECT * FROM read_json('x.json')", "read_json"),
        ("SELECT * FROM read_json_auto('x.json')", "read_json_auto"),
        ("SELECT * FROM read_ndjson('x.json')", "read_ndjson"),
        ("SELECT * FROM glob('*')", "glob"),
        ("SELECT * FROM accounts, read_csv('x.csv')", "read_csv"),
        ("SELECT * FROM main.read_csv('x.csv')", "read_csv"),
        ("SELECT * FROM duckdb_tables()", "duckdb_tables"),
        ("SELECT * FROM duckdb_settings()", "duckdb_settings"),
        ("SELECT * FROM pragma_database_list()", "pragma_database_list"),
        ("SELECT * FROM query_table('accounts')", "query_table"),
        ("SELECT * FROM range(10)", "range"),
        ("SELECT * FROM generate_series(1, 10)", "generate_series"),
        ("SELECT * FROM unnest([1, 2, 3])", "unnest"),
        ("SELECT * FROM accounts, LATERAL unnest([1, 2])", "unnest"),
        ("SELECT getenv('HOME')", "getenv"),
        ("SELECT main.getenv('HOME')", "getenv"),
        ("SELECT current_setting('home_directory')", "current_setting"),
        ("SELECT read_text('/etc/passwd')", "read_text"),
        ("SELECT read_blob('/etc/passwd')", "read_blob"),
        ("SELECT * FROM accounts WHERE name = getenv('USER')", "getenv"),
        ("SELECT (SELECT count(*) FROM read_csv('x.csv'))", "read_csv"),
        ("WITH x AS (SELECT * FROM read_csv('x.csv')) SELECT * FROM x", "read_csv"),
    ],
)
def test_table_functions_are_refused(sql: str, function: str) -> None:
    error = refused(sql)

    assert error.error_kind == "forbidden_function"
    assert function in error.message.lower()


def test_ordinary_functions_are_not_in_the_way() -> None:
    # A denylist that caught everyday SQL would be a denial of service on the model.
    for sql in [
        "SELECT date_trunc('month', customer_since), count(*) FROM accounts GROUP BY 1",
        "SELECT strftime(customer_since, '%Y') FROM accounts",
        "SELECT regexp_matches(name, 'Inc') FROM accounts",
        "SELECT list_aggregate([1, 2, 3], 'sum')",
        "SELECT unnest([1, 2, 3])",
        "SELECT range(5)",
        "SELECT arg_max(name, annual_revenue) FROM accounts",
        "SELECT json_extract_string('{\"a\": 1}', '$.a')",
        "SELECT uen FROM accounts QUALIFY row_number() OVER (ORDER BY name) = 1",
        "SELECT * EXCLUDE (name) FROM accounts",
        "SELECT * FROM (VALUES (1), (2)) AS t(x)",
    ]:
        assert limit_of(accepted(sql)) == CAP + 1, sql


def test_a_large_limit_is_lowered_and_a_small_one_kept() -> None:
    assert limit_of(accepted("SELECT * FROM accounts LIMIT 100000")) == CAP + 1
    assert limit_of(accepted(f"SELECT * FROM accounts LIMIT {CAP + 1}")) == CAP + 1
    assert limit_of(accepted(f"SELECT * FROM accounts LIMIT {CAP + 2}")) == CAP + 1
    assert limit_of(accepted("SELECT * FROM accounts LIMIT 5")) == 5
    assert limit_of(accepted(f"SELECT * FROM accounts LIMIT {CAP}")) == CAP
    assert limit_of(accepted("SELECT * FROM accounts LIMIT 0")) == 0


def test_an_offset_survives_the_limit_rewrite() -> None:
    lowered = accepted("SELECT * FROM accounts ORDER BY uen LIMIT 99999 OFFSET 40")
    kept = accepted("SELECT * FROM accounts ORDER BY uen LIMIT 10 OFFSET 40")
    offset_only = accepted("SELECT * FROM accounts ORDER BY uen OFFSET 40")

    assert (limit_of(lowered), offset_of(lowered)) == (CAP + 1, 40)
    assert (limit_of(kept), offset_of(kept)) == (10, 40)
    assert (limit_of(offset_only), offset_of(offset_only)) == (CAP + 1, 40)


def test_only_the_outermost_limit_is_capped() -> None:
    rewritten = accepted(
        "SELECT * FROM (SELECT * FROM accounts ORDER BY uen LIMIT 5000) AS q LIMIT 3000"
    )

    tree = sqlglot.parse_one(rewritten, read="duckdb")
    limits = sorted(int(node.expression.name) for node in tree.find_all(exp.Limit))
    assert limits == [CAP + 1, 5000]


def test_a_fetch_first_clause_is_the_same_as_a_limit() -> None:
    assert limit_of(accepted("SELECT * FROM accounts FETCH FIRST 7 ROWS ONLY")) == 7
    assert limit_of(accepted("SELECT * FROM accounts FETCH FIRST 70000 ROWS ONLY")) == CAP + 1


def test_limit_all_and_limit_null_mean_everything_so_the_cap_applies() -> None:
    assert limit_of(accepted("SELECT * FROM accounts LIMIT ALL")) == CAP + 1
    assert limit_of(accepted("SELECT * FROM accounts LIMIT NULL")) == CAP + 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM accounts LIMIT 10 PERCENT",
        "SELECT * FROM accounts LIMIT 5 + 5",
        "SELECT * FROM accounts LIMIT (SELECT 3)",
        "SELECT * FROM accounts LIMIT -1",
        "SELECT * FROM accounts LIMIT 2.5",
        "SELECT * FROM accounts ORDER BY uen LIMIT 5 WITH TIES",
    ],
)
def test_a_limit_that_is_not_a_plain_row_count_is_refused_not_guessed(sql: str) -> None:
    # Rewriting such a limit would change what the model asked for, so it is asked to restate it.
    error = refused(sql)

    assert error.error_kind == "parse_error"
    assert "LIMIT" in error.message


def test_select_star_is_allowed() -> None:
    assert limit_of(accepted("SELECT * FROM accounts")) == CAP + 1
    assert limit_of(accepted("SELECT a.*, c.* FROM accounts a JOIN contacts c USING (uen)")) == (
        CAP + 1
    )


def test_a_hostile_string_value_is_just_a_value() -> None:
    hostile = "east' OR '1'='1"
    escaped = hostile.replace("'", "''")

    rewritten = accepted(f"SELECT * FROM accounts WHERE state = '{escaped}'")

    tree = sqlglot.parse_one(rewritten, read="duckdb")
    literals = [node.this for node in tree.find_all(exp.Literal) if node.is_string]
    assert literals == [hostile]


def test_a_semicolon_and_keywords_inside_a_string_are_data() -> None:
    sql = "SELECT * FROM accounts WHERE name = 'a; DROP TABLE accounts; --'"

    rewritten = accepted(sql)

    assert limit_of(rewritten) == CAP + 1
    assert "'a; DROP TABLE accounts; --'" in rewritten


def test_a_backslash_in_a_string_cannot_close_the_literal_early() -> None:
    # DuckDB reads a backslash as an ordinary character. The rewritten text must say the same
    # thing the model's text said, so a quote after a backslash still ends the string.
    sql = "SELECT * FROM accounts WHERE name = 'a\\' OR name = 'b'"

    rewritten = accepted(sql)

    tree = sqlglot.parse_one(rewritten, read="duckdb")
    literals = [node.this for node in tree.find_all(exp.Literal) if node.is_string]
    assert literals == ["a\\", "b"]


def test_a_statement_that_does_not_parse_is_a_parse_error_with_a_position() -> None:
    error = refused("SELEC * FROM accounts")

    assert error.error_kind == "parse_error"
    assert "\x1b" not in error.message  # no terminal colour codes in a message for a model

    unbalanced = refused("SELECT (1 + 2 FROM accounts")
    assert unbalanced.error_kind == "parse_error"
    assert "line 1" in unbalanced.message


def test_empty_and_comment_only_input_is_a_parse_error() -> None:
    for sql in ["", "   ", ";", "-- nothing here", "/* nothing */ ;"]:
        assert refused(sql).error_kind == "parse_error", repr(sql)


def test_oversized_and_absurdly_nested_input_is_refused_not_crashed() -> None:
    assert refused("SELECT '" + "x" * MAX_SQL_CHARS + "'").error_kind == "parse_error"

    nested = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    assert refused(nested).error_kind == "parse_error"


def test_a_parenthesised_select_is_unwrapped() -> None:
    assert limit_of(accepted("(SELECT uen FROM accounts)")) == CAP + 1


def test_validation_writes_nothing_to_the_log(caplog: pytest.LogCaptureFixture) -> None:
    # sqlglot warns when it falls back to a raw command or drops a clause, and the warning quotes
    # the statement. This server's logs never carry SQL, so validation must not provoke it.
    caplog.set_level(logging.DEBUG)
    marker = "zqloggingmarker4417"

    for sql in [
        f"LOAD {marker}",
        f"EXECUTE {marker}",
        f"SELECT '{marker}' FROM accounts FOR UPDATE",
        f"SELECT * FROM {marker}",
    ]:
        with contextlib.suppress(SqlRefused):
            accepted(sql)

    assert marker not in caplog.text
    assert caplog.records == []


def test_the_rewrite_is_idempotent() -> None:
    once = accepted("SELECT uen FROM accounts ORDER BY uen LIMIT 5000 OFFSET 3")

    assert accepted(once) == once


def test_the_original_text_is_never_returned_unparsed() -> None:
    # What runs is the statement rendered from the syntax tree, so spacing and comments of the
    # model's text do not reach the engine.
    rewritten = accepted("SELECT   uen   -- the key\nFROM\taccounts")

    assert rewritten == f"SELECT uen FROM accounts LIMIT {CAP + 1}"


def test_the_row_cap_is_the_callers_to_set() -> None:
    assert limit_of(validate_sql("SELECT 1", TABLES, 10)) == 11
    assert limit_of(validate_sql("SELECT 1 LIMIT 50", TABLES, 10)) == 11
    assert limit_of(validate_sql("SELECT 1 LIMIT 3", TABLES, 10)) == 3

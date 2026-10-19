"""The containment contract: what the validator lets through, and what it refuses.

The validator is the only gate between model-written SQL and the engine, so these tests are the
specification of that gate. Each refusal names its ``error_kind`` (the vocabulary the agent's
repair loop reads) and says what the model should do instead. The validator works on the parsed
statement, never on the text, so a keyword inside a string or a comment is not a statement.
"""

from __future__ import annotations

import contextlib
import logging
import time

import pytest
import sqlglot
from sqlglot import exp

from rmqna_data.validate import (
    MAX_CTE_AND_SUBQUERY_REFERENCES,
    MAX_JOIN_SOURCES,
    MAX_SQL_CHARS,
    SqlRefused,
    validate_sql,
)

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


@pytest.mark.parametrize(
    ("sql", "function"),
    [
        # The reviewer's exact runaway: a 3-billion-element list unnested inside a subquery.
        ("SELECT sum(x) FROM (SELECT unnest(range(0, 3000000000)) AS x)", "unnest|range"),
        ("SELECT unnest([1, 2, 3])", "unnest"),
        ("SELECT range(5)", "range"),
        ("SELECT generate_series(1, 10)", "generate_series"),
        ("SELECT [x * 2 FOR x IN range(5)]", "range"),
        ("SELECT len(list_resize([1], 3000000000))", "list_resize"),
        ("SELECT len(array_resize([1], 3000000000))", "array_resize"),
        ("SELECT length(repeat('a', 3000000000))", "repeat"),
        # A string of any width out of one number: 8 GB, 25 seconds past the timeout, in review.
        ("SELECT rpad(name, 20000000, 'y') FROM accounts", "rpad"),
        (
            "SELECT length(rpad(a.name, 20000000, 'y')) FROM accounts a JOIN contacts c "
            "ON c.uen = a.uen",
            "rpad",
        ),
        ("SELECT lpad(name, 20000000, 'y') FROM accounts", "lpad"),
        ("SELECT rpad('', 1900000, chr(1))", "rpad"),  # 11 MB of JSON escapes in review
        ("SELECT printf('%2000000000d', 1)", "printf"),
        ("SELECT PRINTF('%5d', employees) FROM accounts", "printf"),
        ("SELECT format('{:>2000000000}', 1)", "format"),
        ("SELECT \"rpad\"(name, 5, 'y') FROM accounts", "rpad"),
        ("SELECT * FROM accounts WHERE length(name) > length(repeat('x', 3))", "repeat"),
    ],
)
def test_a_generator_of_rows_or_memory_is_refused_anywhere_in_the_statement(
    sql: str, function: str
) -> None:
    started = time.perf_counter()
    error = refused(sql)

    assert error.error_kind == "forbidden_function"
    assert any(name in error.message for name in function.split("|"))
    assert time.perf_counter() - started < 1  # refused at validation, never handed to DuckDB


def test_the_named_generators_are_not_a_blanket_bound_on_integer_arguments() -> None:
    # A large literal is not itself the problem: an epoch is ordinary, and so are wide limits.
    for sql in [
        "SELECT to_timestamp(1750000000)",
        "SELECT epoch_ms(1750000000000)",
        "SELECT * FROM accounts WHERE employees < 4000000000",
        "SELECT left(name, 2000000000), substr(name, 1, 3000000000) FROM accounts",
    ]:
        assert limit_of(accepted(sql)) == CAP + 1, sql


def test_ordinary_functions_are_not_in_the_way() -> None:
    # A denylist that caught everyday SQL would be a denial of service on the model.
    for sql in [
        "SELECT date_trunc('month', customer_since), count(*) FROM accounts GROUP BY 1",
        "SELECT strftime(customer_since, '%Y') FROM accounts",
        "SELECT regexp_matches(name, 'Inc') FROM accounts",
        "SELECT list_aggregate([1, 2, 3], 'sum')",
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


@pytest.mark.parametrize(
    ("sql", "word"),
    [
        ("PIVOT accounts ON country USING count(*)", "PIVOT"),
        ("SELECT * FROM accounts PIVOT (count(*) FOR country IN ('USA', 'Canada'))", "PIVOT"),
        ("SELECT * FROM accounts UNPIVOT (v FOR k IN (country, segment))", "UNPIVOT"),
        (
            "WITH p AS (PIVOT (SELECT 'x' AS c FROM accounts) ON c USING count(*)) SELECT * FROM p",
            "PIVOT",
        ),
        ("SELECT * FROM accounts, LATERAL (PIVOT accounts ON country USING count(*))", "PIVOT"),
        ("WITH u AS (UNPIVOT accounts ON country INTO NAME k VALUE v) SELECT * FROM u", "UNPIVOT"),
        ("SELECT * FROM (PIVOT accounts ON country USING count(*)) q", "PIVOT"),
    ],
)
def test_pivot_and_unpivot_are_refused_in_every_form(sql: str, word: str) -> None:
    error = refused(sql)

    assert error.error_kind == "not_a_select"
    assert word in error.message
    assert "not supported" in error.message
    assert "CASE" in error.message  # and says what to do instead


def test_a_self_join_of_hundreds_of_sources_is_refused_before_duckdb_sees_it() -> None:
    # The reviewer's second runaway: 400 sources in one SELECT, well under the length cap.
    sql = "SELECT count(*) FROM accounts a1 " + " ".join(
        f"JOIN accounts a{i} ON a{i}.uen = a1.uen" for i in range(2, 401)
    )
    assert len(sql) < MAX_SQL_CHARS
    started = time.perf_counter()

    error = refused(sql)

    assert error.error_kind == "parse_error"
    assert "399 joins" in error.message
    assert time.perf_counter() - started < 1


def paren_group(index: int, size: int) -> str:
    """``(accounts gNa0 JOIN accounts gNa1 ON ... )``: sqlglot hangs the joins on the group's table."""
    joins = " ".join(
        f"JOIN accounts g{index}a{i} ON g{index}a{i}.uen = g{index}a0.uen" for i in range(1, size)
    )
    return f"(accounts g{index}a0 {joins})"


def test_parenthesized_join_groups_do_not_hide_how_many_joins_there_are() -> None:
    # The reviewer's shape: 16 top-level groups of 25 relations each, 400 relations in all. Each
    # SELECT has 15 joins of its own and the groups carry the rest, so a per-SELECT count passed it.
    sql = "SELECT count(*) FROM " + ", ".join(paren_group(g, 25) for g in range(16))
    assert len(sql) < MAX_SQL_CHARS
    started = time.perf_counter()

    error = refused(sql)

    assert error.error_kind == "parse_error"
    assert "joins; the limit is" in error.message
    assert time.perf_counter() - started < 1


def test_a_single_parenthesized_group_of_hundreds_is_refused_too() -> None:
    sql = "SELECT count(*) FROM " + paren_group(0, 400)

    assert len(sql) < MAX_SQL_CHARS
    assert refused(sql).error_kind == "parse_error"


def test_comma_layouts_cannot_hide_the_joins_either() -> None:
    top_level = "SELECT 1 FROM " + ", ".join(f"accounts a{i}" for i in range(400))
    in_a_group = "SELECT 1 FROM (" + ", ".join(f"accounts a{i}" for i in range(400)) + ")"
    in_a_subquery = (
        "SELECT 1 FROM accounts WHERE uen IN (SELECT a0.uen FROM " + paren_group(0, 40) + ")"
    )

    for sql in (top_level, in_a_group, in_a_subquery):
        assert refused(sql).error_kind == "parse_error", sql[:60]


def test_joins_spread_over_ctes_are_counted_together() -> None:
    ctes = ", ".join(
        f"c{i} AS (SELECT a.uen FROM accounts a "
        + " ".join(f"JOIN contacts k{j} ON k{j}.uen = a.uen" for j in range(4))
        + ")"
        for i in range(9)
    )

    assert refused(f"WITH {ctes} SELECT * FROM c0").error_kind == "parse_error"  # 36 joins
    assert limit_of(accepted("WITH " + ctes.split(", c3 AS")[0] + " SELECT * FROM c0")) == CAP + 1


def test_the_join_cap_counts_comma_joins_and_applies_per_select() -> None:
    at_cap = "SELECT 1 FROM " + ", ".join(f"accounts a{i}" for i in range(MAX_JOIN_SOURCES))
    over = "SELECT 1 FROM " + ", ".join(f"accounts a{i}" for i in range(MAX_JOIN_SOURCES + 1))
    nested = (
        "SELECT 1 FROM accounts a1 JOIN contacts c ON c.uen = a1.uen "
        "WHERE a1.uen IN (SELECT uen FROM " + ", ".join(f"xref x{i}" for i in range(17)) + ")"
    )

    assert limit_of(accepted(at_cap)) == CAP + 1
    assert refused(over).error_kind == "parse_error"
    assert refused(nested).error_kind == "parse_error"  # the subquery's own SELECT is over


def doubling_chain(levels: int) -> str:
    """A chain of CTEs, each reading the one before it twice: a plan that doubles per level."""
    ctes = ["c0 AS (SELECT 1 AS x)"] + [
        f"c{i} AS (SELECT a.x FROM c{i - 1} a, c{i - 1} b)" for i in range(1, levels + 1)
    ]
    return "WITH " + ", ".join(ctes) + f" SELECT count(*) FROM c{levels}"


def test_a_doubling_cte_chain_is_refused_by_the_structural_cap() -> None:
    # The reviewer's first runaway: 26 levels, each reading the previous one twice (52 references,
    # and one more from the final SELECT).
    started = time.perf_counter()

    error = refused(doubling_chain(26))

    assert error.error_kind == "parse_error"
    assert "53 times" in error.message
    assert str(MAX_CTE_AND_SUBQUERY_REFERENCES) in error.message
    assert time.perf_counter() - started < 1


def test_an_ordinary_cte_heavy_query_is_under_the_cap() -> None:
    # 8 levels read twice each is 16 references: ordinary analytic SQL, nowhere near the cap.
    assert limit_of(accepted(doubling_chain(8))) == CAP + 1
    subqueries = (
        "SELECT * FROM accounts WHERE uen IN (SELECT uen FROM contacts) "
        "AND uen IN (SELECT uen FROM facilities) AND EXISTS (SELECT 1 FROM xref)"
    )
    assert limit_of(accepted(subqueries)) == CAP + 1


def test_subqueries_count_toward_the_same_cap() -> None:
    sql = "SELECT * FROM accounts WHERE " + " AND ".join(
        f"uen IN (SELECT uen FROM contacts WHERE title = 't{i}')"
        for i in range(MAX_CTE_AND_SUBQUERY_REFERENCES + 1)
    )

    assert refused(sql).error_kind == "parse_error"


def test_naming_a_refused_statement_cannot_be_made_to_take_forever() -> None:
    # A regular expression for "comments, then a word" backtracked exponentially on this input:
    # 28 block comments before a character that is not a letter took 19 seconds, on the event loop,
    # before the call could even be given a timeout.
    started = time.perf_counter()
    for sql in [
        "/*a*/ " * 28 + "1",
        "/*a*/ " * 400 + "1",
        "/**/ " * 400 + "'x'",
        "-- a\n" * 400 + "1",
        "/* " * 400,
        "/*" * 2000 + "1",
    ]:
        error = refused(sql)
        assert error.error_kind in {"not_a_select", "parse_error"}, sql[:30]

    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    ("sql", "word"),
    [
        ("CHECKPOINT", "CHECKPOINT"),
        ("TABLE accounts", "TABLE"),
        ("ANALYZE", "ANALYZE"),
        ("-- note\nCHECKPOINT", "CHECKPOINT"),
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("VACUUM", "VACUUM"),
    ],
)
def test_a_refused_statement_is_named_for_what_it_is(sql: str, word: str) -> None:
    error = refused(sql)

    assert error.error_kind == "not_a_select"
    assert error.message.startswith(f"{word} is not allowed")
    for odd in ("Column", "Alias", "Analyze statement", " a "):
        assert odd not in error.message.split(" is not allowed")[0]


def test_validation_writes_nothing_to_the_log(caplog: pytest.LogCaptureFixture) -> None:
    # sqlglot warns when it falls back to a raw command, cannot traverse a scope or drops a clause,
    # and the warning quotes the statement. This server's logs never carry SQL, so validation must
    # not let any of them through, whatever the statement provokes.
    caplog.set_level(logging.DEBUG)
    marker = "zqloggingmarker4417"

    for sql in [
        f"LOAD {marker}",
        f"EXECUTE {marker}",
        f"SELECT '{marker}' FROM accounts FOR UPDATE",
        f"SELECT * FROM {marker}",
        # The reviewer's leak inputs: sqlglot's scope traversal logs "Cannot traverse scope ...".
        f"WITH p AS (PIVOT (SELECT '{marker}' AS c FROM accounts) ON c USING count(*)) "
        "SELECT * FROM p",
        f"SELECT * FROM accounts, LATERAL (PIVOT (SELECT '{marker}' AS c FROM accounts) "
        "ON c USING count(*))",
        f"WITH u AS (UNPIVOT (SELECT '{marker}' AS c, 1 AS n FROM accounts) ON c, n) "
        "SELECT * FROM u",
        f"SELECT '{marker}' AS c FROM accounts PIVOT (count(*) FOR c IN ('a'))",
    ]:
        with contextlib.suppress(SqlRefused):
            accepted(sql)

    assert marker not in caplog.text
    assert caplog.records == []


def test_sqlglots_scope_warning_is_silenced_without_leaning_on_the_pivot_refusal(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The PIVOT refusal comes before scope analysis, so the reviewer's inputs no longer reach the
    # step that warned. Take that refusal away and the warning is provoked; the filter must hold.
    monkeypatch.setattr("rmqna_data.validate._refuse_nested_statements", lambda _root: None)
    caplog.set_level(logging.DEBUG)
    marker = "zqscopemarker2208"
    sql = f"WITH p AS (PIVOT (SELECT '{marker}' AS c FROM accounts) ON c USING count(*)) SELECT * FROM p"

    with contextlib.suppress(SqlRefused):
        accepted(sql)

    assert marker not in caplog.text
    assert caplog.records == []

    # Without the filter the same input does log the statement (proof the test can fail).
    monkeypatch.setattr("rmqna_data.validate._sqlglot_silenced", contextlib.nullcontext)
    with contextlib.suppress(SqlRefused):
        accepted(sql)
    assert marker in caplog.text


def test_the_silence_is_scoped_to_validation() -> None:
    # Outside validate_sql sqlglot logs as it always did; the filter is gone when the call ends.
    accepted("SELECT 1")

    assert logging.getLogger("sqlglot").filters == []


def test_the_silence_is_lifted_even_when_the_statement_is_refused() -> None:
    with pytest.raises(SqlRefused):
        validate_sql("DROP TABLE accounts", TABLES, CAP)

    assert logging.getLogger("sqlglot").filters == []


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

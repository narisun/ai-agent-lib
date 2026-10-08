"""Named queries are validated when loaded and bound strictly when called."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ai_agent_lib_core.adapters.sql import QueryCatalog, compile_query
from ai_agent_lib_core.contracts import (
    Classification,
    ConfigurationError,
    Obligations,
    ParameterType,
    PolicyDenied,
    RowFilter,
    ValidationFailed,
)

QUERIES = Path(__file__).parent / "queries"
HEADER = "-- description: A test query.\n-- max_rows: 10\n"


@pytest.fixture(scope="module")
def catalog() -> QueryCatalog:
    return QueryCatalog.load(QUERIES)


def load_one(tmp_path: Path, body: str, name: str = "q") -> QueryCatalog:
    (tmp_path / f"{name}.sql").write_text(body, encoding="utf-8")
    return QueryCatalog.load(tmp_path)


# -------------------------------------------------------------------- loading


def test_the_header_declares_the_query_interface(catalog: QueryCatalog) -> None:
    described = catalog.describe()["accounts_by_region"]
    assert described.description == "Balances of the accounts in one region."
    assert described.max_rows == 3
    assert described.classification is Classification.RESTRICTED
    region, min_balance = described.parameters
    assert (region.name, region.type, region.required) == ("region", ParameterType.STRING, True)
    assert (min_balance.name, min_balance.type, min_balance.required, min_balance.default) == (
        "min_balance",
        ParameterType.NUMBER,
        False,
        Decimal("0"),
    )
    assert catalog.get("accounts_by_region").outputs == (
        "account_id",
        "holder",
        "region",
        "balance",
    )
    assert catalog.dialect == "redshift"
    assert sorted(catalog.describe()) == ["accounts_by_region", "largest_accounts"]


def test_classification_defaults_to_internal(catalog: QueryCatalog) -> None:
    assert catalog.describe()["largest_accounts"].classification is Classification.INTERNAL


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("-- max_rows: 10\nSELECT a FROM t", "description is required"),
        ("-- description: x\nSELECT a FROM t", "max_rows is required"),
        ("-- description: x\n-- max_rows: 0\nSELECT a FROM t", "positive integer"),
        ("-- description: x\n-- max_rows: 10\n-- owner: me\nSELECT a FROM t", "unknown header key"),
        (HEADER + "-- classification: secret\nSELECT a FROM t", "classification must be one of"),
        (HEADER + "-- param p: blob\nSELECT a FROM t WHERE b = :p", "one of the types"),
        (HEADER + "-- param p: integer = x\nSELECT a FROM t WHERE b = :p", "default"),
        (HEADER + "-- param p: string\n-- param p: string\nSELECT a FROM t WHERE b = :p", "twice"),
        (HEADER + "SELECT a FROM t WHERE b = :p", "used but not declared"),
        (HEADER + "-- param p: string\nSELECT a FROM t", "declared but not used"),
        (HEADER + "SELECT a FROM t WHERE b = ?", "written as ':name'"),
        (HEADER + "SELECT FROM WHERE", "cannot be parsed"),
        (HEADER + "SELECT a FROM t; SELECT b FROM t", "exactly one statement"),
        (HEADER + "", "exactly one statement"),
        (HEADER + "DELETE FROM t", "read-only"),
        (HEADER + "UPDATE t SET a = 1", "read-only"),
        (HEADER + "CREATE TABLE x AS SELECT a FROM t", "read-only"),
        (HEADER + "DROP TABLE t", "read-only"),
        (HEADER + "SELECT a INTO other FROM t", "read-only"),
        (HEADER + "SELECT * FROM t", "must be named"),
        (HEADER + "SELECT a, a + 1 FROM t", "must be named"),
        (HEADER + "SELECT a, b AS a FROM t", "share a name"),
        (HEADER + "SELECT a FROM t ORDER BY hidden", "ORDER BY must use"),
        (HEADER + "SELECT a FROM t ORDER BY a + 1", "ORDER BY must use"),
    ],
)
def test_an_invalid_query_file_stops_loading(tmp_path: Path, body: str, problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem):
        load_one(tmp_path, body)


def test_the_file_name_must_be_a_plain_identifier(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="lower-case"):
        load_one(tmp_path, HEADER + "SELECT a FROM t", name="Bad-Name")


def test_a_missing_directory_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="the queries folder does not exist"):
        QueryCatalog.load(tmp_path / "missing")


# -------------------------------------------------------------------- binding


def test_parameters_are_converted_to_their_declared_types(catalog: QueryCatalog) -> None:
    bound = catalog.bind("accounts_by_region", {"region": "east", "min_balance": 100.5})
    assert bound == {"region": "east", "min_balance": Decimal("100.5")}
    assert catalog.bind("accounts_by_region", {"region": "east"})["min_balance"] == Decimal("0")
    assert catalog.bind("largest_accounts", {"opened_after": "2024-02-29"}) == {
        "opened_after": date(2024, 2, 29)
    }
    assert catalog.bind("largest_accounts", None) == {"opened_after": date(2000, 1, 1)}


@pytest.mark.parametrize(
    ("parameters", "problem"),
    [
        ({}, "needs parameter 'region'"),
        ({"region": None}, "needs parameter 'region'"),
        ({"region": "east", "extra": 1}, "was given parameters it does not declare"),
        ({"region": 7}, "'region' is not a string\n  expected: a string\n  got: a number"),
        ({"region": "east", "min_balance": "lots"}, "got: text of 4 characters"),
        ({"region": "east", "min_balance": True}, "got: true or false"),
        ({"region": "east", "min_balance": float("nan")}, "'min_balance' is not a number"),
    ],
)
def test_bad_parameters_are_rejected_without_echoing_values(
    catalog: QueryCatalog, parameters: dict[str, object], problem: str
) -> None:
    with pytest.raises(ValidationFailed, match=problem) as caught:
        catalog.bind("accounts_by_region", parameters)
    assert "lots" not in str(caught.value)


def test_every_parameter_type_has_strict_conversion(tmp_path: Path) -> None:
    catalog = load_one(
        tmp_path,
        HEADER
        + "-- param i: integer\n-- param b: boolean\n-- param d: date\n-- param t: timestamp\n"
        + "SELECT a FROM x WHERE i = :i AND b = :b AND d = :d AND t = :t",
    )
    good = {"i": "42", "b": "true", "d": date(2026, 1, 2), "t": "2026-01-02T03:04:05+00:00"}
    bound = catalog.bind("q", good)
    assert bound["i"] == 42
    assert bound["b"] is True
    assert bound["d"] == date(2026, 1, 2)
    assert isinstance(bound["t"], datetime)
    for name, bad in [("i", 1.5), ("i", True), ("b", 1), ("d", "tomorrow"), ("t", 12345)]:
        with pytest.raises(ValidationFailed, match=f"'{name}' is not an? "):
            catalog.bind("q", {**good, name: bad})


def test_an_unknown_query_is_rejected(catalog: QueryCatalog) -> None:
    with pytest.raises(ValidationFailed, match="unknown query"):
        catalog.get("drop_everything")
    with pytest.raises(ValidationFailed, match="unknown query"):
        catalog.bind("SELECT 1", {})


# ------------------------------------------------------------------ compiling

# Golden statements. Redshift SQL is the source; these are what DuckDB is given.
GOLDEN_PLAIN = (
    "SELECT * FROM (SELECT account_id, COALESCE(holder, '?') AS holder, region, balance "
    "FROM accounts WHERE region = $region AND balance >= $min_balance) AS governed_query "
    "ORDER BY account_id LIMIT 4"
)
GOLDEN_FILTERED = (
    "SELECT * FROM (SELECT account_id, COALESCE(holder, '?') AS holder, region, balance "
    "FROM accounts WHERE region = $region AND balance >= $min_balance) AS governed_query "
    "WHERE region IN ($_obl_0_0, $_obl_0_1) AND holder IN ($_obl_1_0) "
    "ORDER BY account_id LIMIT 3"
)
GOLDEN_TOP_N = (
    "SELECT * FROM (SELECT account_id, region, balance FROM accounts "
    "WHERE opened >= $opened_after ORDER BY balance DESC NULLS FIRST LIMIT 2) AS governed_query "
    "ORDER BY balance DESC NULLS FIRST LIMIT 51"
)


def test_redshift_sql_is_transpiled_and_capped_one_row_over(catalog: QueryCatalog) -> None:
    compiled = compile_query(catalog.get("accounts_by_region"), None, target_dialect="duckdb")
    assert compiled.sql == GOLDEN_PLAIN
    assert compiled.row_cap == 3
    assert compiled.parameters == {}


def test_row_filters_run_before_the_cap_as_bound_parameters(catalog: QueryCatalog) -> None:
    obligations = Obligations(
        row_filters=(RowFilter("REGION", ("east", "west")), RowFilter("holder", ("Ann",))),
        max_rows=2,
    )
    compiled = compile_query(
        catalog.get("accounts_by_region"), obligations, target_dialect="duckdb"
    )
    assert compiled.sql == GOLDEN_FILTERED
    assert compiled.row_cap == 2
    assert compiled.parameters == {"_obl_0_0": "east", "_obl_0_1": "west", "_obl_1_0": "Ann"}
    assert compiled.sql.index("WHERE region IN") < compiled.sql.index("LIMIT")


def test_a_top_n_query_keeps_its_own_limit_and_ordering(catalog: QueryCatalog) -> None:
    # Redshift sorts NULLs first on DESC and DuckDB does not, so the transpiler says so.
    compiled = compile_query(catalog.get("largest_accounts"), None, target_dialect="duckdb")
    assert compiled.sql == GOLDEN_TOP_N


def test_the_same_query_compiles_for_its_own_dialect(catalog: QueryCatalog) -> None:
    compiled = compile_query(catalog.get("accounts_by_region"), None, target_dialect="redshift")
    assert "COALESCE(holder, '?')" in compiled.sql
    assert "NULLS" not in compiled.sql
    assert compiled.sql.endswith("ORDER BY account_id LIMIT 4")


def test_a_hostile_value_is_only_ever_a_bound_parameter(catalog: QueryCatalog) -> None:
    hostile = "east' OR '1'='1'; DROP TABLE accounts; --"
    bound = catalog.bind("accounts_by_region", {"region": hostile})
    obligations = Obligations(row_filters=(RowFilter("region", (hostile,)),))
    compiled = compile_query(
        catalog.get("accounts_by_region"), obligations, target_dialect="duckdb"
    )
    assert bound["region"] == hostile
    assert compiled.parameters["_obl_0_0"] == hostile
    assert "DROP" not in compiled.sql
    assert "OR '1'" not in compiled.sql


def test_an_empty_row_filter_admits_no_rows(catalog: QueryCatalog) -> None:
    obligations = Obligations(row_filters=(RowFilter("region", ()),))
    compiled = compile_query(
        catalog.get("accounts_by_region"), obligations, target_dialect="duckdb"
    )
    assert "WHERE FALSE" in compiled.sql


def test_a_filter_on_a_column_the_query_does_not_return_fails_closed(
    catalog: QueryCatalog,
) -> None:
    obligations = Obligations(row_filters=(RowFilter("tenant_id", ("t-1",)),))
    with pytest.raises(PolicyDenied) as caught:
        compile_query(catalog.get("accounts_by_region"), obligations, target_dialect="duckdb")
    assert caught.value.reason_code == "obligation_unenforceable"


def test_a_result_becomes_a_plain_json_payload() -> None:
    import json
    from datetime import UTC

    from ai_agent_lib_core.contracts import QueryResult, SourceMetadata

    fetched = datetime(2026, 3, 1, 6, 30, tzinfo=UTC)
    result = QueryResult(
        columns=("account_id", "opened", "balance", "active", "note"),
        rows=((4411, date(2019, 3, 14), Decimal("1250.10"), True, None),),
        truncated=True,
        masked_columns=frozenset({"note", "holder"}),
        source=SourceMetadata(source="ledger", retrieved_at=fetched, uri="datasource://ledger/q"),
    )
    payload = result.to_payload()
    assert payload == {
        "columns": ["account_id", "opened", "balance", "active", "note"],
        "rows": [[4411, "2019-03-14", "1250.10", True, None]],
        "truncated": True,
        "masked_columns": ["holder", "note"],
        "source": {
            "name": "ledger",
            "retrieved_at": "2026-03-01T06:30:00+00:00",
            "as_of": None,
            "uri": "datasource://ledger/q",
        },
    }
    assert json.loads(json.dumps(payload)) == payload
    assert "source" not in QueryResult(columns=("a",), rows=()).to_payload()

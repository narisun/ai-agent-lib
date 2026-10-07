"""The Redshift data source: typed parameters, one deadline, and no leaked messages."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from botocore import exceptions as aws
from botocore.stub import Stubber

from ai_agent_lib_aws.data_redshift import RedshiftDataOptions, RedshiftDataSource
from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.testing import FakeRedshiftData, client_error, offline_sessions
from ai_agent_lib_core.contracts import (
    AgentLibError,
    ConfigurationError,
    Obligations,
    ProviderSelection,
    RowFilter,
    ServiceConfig,
    TransientError,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.testing import Fakes, FrozenClock, write_accounts_files

TYPED_QUERY = (
    "-- description: One query with a parameter of every type.\n"
    "-- param holder: string\n"
    "-- param note: string = \n"
    "-- param least: integer = 0\n"
    "-- param balance: number = 0\n"
    "-- param active: boolean = true\n"
    "-- param opened: date = 2020-01-01\n"
    "-- param seen: timestamp = 2020-01-01T00:00:00\n"
    "-- max_rows: 2\n"
    "SELECT account_id, holder, balance\n"
    "FROM accounts\n"
    "WHERE holder = :holder AND region <> :note AND account_id >= :least\n"
    "  AND balance >= :balance AND :active AND :opened < :seen AND holder = :holder\n"
    "ORDER BY account_id\n"
)


class Waits:
    """Records how long the adapter asked to wait, without waiting."""

    def __init__(self) -> None:
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)
        await asyncio.sleep(0)


def options(queries_dir: Path, **changes: Any) -> RedshiftDataOptions:
    settings: dict[str, Any] = {"queries_dir": queries_dir, "database": "dev", "workgroup": "eap"}
    settings.update(changes)
    return RedshiftDataOptions.model_validate(settings)


async def started(
    tmp_path: Path, **changes: Any
) -> tuple[RedshiftDataSource, FakeRedshiftData, Waits]:
    data_dir, queries_dir = write_accounts_files(tmp_path)
    (queries_dir / "typed.sql").write_text(TYPED_QUERY, encoding="utf-8")
    database, waits = FakeRedshiftData(data_dir), Waits()
    source = RedshiftDataSource(
        "accounts",
        options(queries_dir, **changes),
        offline_sessions(),
        FrozenClock(),
        client=database,
        sleep=waits,
    )
    await source.start()
    return source, database, waits


async def test_the_requests_and_the_replies_have_the_shapes_of_the_real_service(
    tmp_path: Path,
) -> None:
    _, queries_dir = write_accounts_files(tmp_path)
    sessions = offline_sessions()
    source = RedshiftDataSource(
        "accounts", options(queries_dir), sessions, FrozenClock(), sleep=Waits()
    )
    await source.start()
    metadata = [
        {"name": "account_id", "label": "account_id", "typeName": "int8"},
        {"name": "holder", "label": "HOLDER", "typeName": "varchar"},
        {"name": "region", "label": "region", "typeName": "varchar"},
        {"name": "balance", "label": "balance", "typeName": "numeric"},
    ]
    with Stubber(sessions.client("redshift-data")) as service:
        service.add_response(
            "execute_statement",
            {"Id": "s-1", "Database": "dev", "WorkgroupName": "eap"},
            {
                "Database": "dev",
                "WorkgroupName": "eap",
                "StatementName": "accounts.accounts_by_region",
                "Sql": (
                    "SELECT * FROM (SELECT account_id, holder, region, balance FROM accounts "
                    "WHERE region = CAST(:p3 AS VARCHAR) "
                    "AND balance >= CAST(:p2 AS DECIMAL(38, 10))) AS governed_query "
                    "WHERE holder IN (CAST(:p1 AS VARCHAR)) ORDER BY account_id LIMIT 4"
                ),
                "Parameters": [
                    {"name": "p1", "value": "Eve"},
                    {"name": "p2", "value": "0"},
                    {"name": "p3", "value": "west"},
                ],
            },
        )
        service.add_response("describe_statement", {"Id": "s-1", "Status": "PICKED"}, {"Id": "s-1"})
        service.add_response(
            "describe_statement",
            {"Id": "s-1", "Status": "FINISHED", "HasResultSet": True},
            {"Id": "s-1"},
        )
        service.add_response(
            "get_statement_result",
            {
                "ColumnMetadata": metadata,
                "Records": [
                    [
                        {"longValue": 5520},
                        {"stringValue": "Eve"},
                        {"stringValue": "west"},
                        {"stringValue": "12004.55"},
                    ]
                ],
                "TotalNumRows": 2,
                "NextToken": "page-2",
            },
            {"Id": "s-1"},
        )
        service.add_response(
            "get_statement_result",
            {
                "ColumnMetadata": metadata,
                "Records": [
                    [
                        {"longValue": 5599},
                        {"stringValue": "Eve"},
                        {"stringValue": "west"},
                        {"isNull": True},
                    ]
                ],
                "TotalNumRows": 2,
            },
            {"Id": "s-1", "NextToken": "page-2"},
        )
        result = await source.query(
            "accounts_by_region",
            {"region": "west"},
            obligations=Obligations(row_filters=(RowFilter("holder", ("Eve",)),)),
        )
        service.assert_no_pending_responses()
    assert result.columns == ("account_id", "holder", "region", "balance")
    assert result.rows == (
        (5520, "Eve", "west", Decimal("12004.55")),
        (5599, "Eve", "west", None),
    )
    assert not result.truncated
    assert result.source is not None
    assert result.source.uri == "datasource://accounts/accounts_by_region"


async def test_every_parameter_is_sent_as_text_and_cast_to_its_type(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path)
    result = await source.query(
        "typed",
        {
            "holder": "Eve",
            "least": 1,
            "balance": "12004.5",
            "opened": date(2021, 2, 3),
            "seen": datetime(2024, 5, 6, 7, 8, 9),  # noqa: DTZ001 - a value without a zone
        },
        obligations=Obligations(row_filters=(RowFilter("account_id", (5520, 4411)),)),
    )
    assert [row[0] for row in result.rows] == [5520]
    request = database.requests[0]
    sql = request["Sql"]
    values = {item["name"]: item["value"] for item in request["Parameters"]}
    assert all(isinstance(value, str) and value for value in values.values())
    by_cast = {
        f"CAST(:{name} AS {kind})": value
        for name, value in values.items()
        for kind in (
            "VARCHAR",
            "BIGINT",
            "DECIMAL(38, 10)",
            "BOOLEAN",
            "DATE",
            "TIMESTAMP",
        )
        if f"CAST(:{name} AS {kind})" in sql
    }
    assert sorted(by_cast.values()) == sorted(
        ["Eve", "1", "12004.5", "true", "2021-02-03", "2024-05-06 07:08:09", "5520", "4411"]
    )
    # A parameter used twice is bound once; an empty string is written as a constant.
    assert sql.count(":") == len(values) + 1
    assert "region <> ''" in sql
    assert "Eve" not in sql


async def test_a_filter_value_is_cast_by_its_own_type(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path)
    await source.query(
        "all_accounts",
        obligations=Obligations(row_filters=(RowFilter("balance", (10.0, True)),)),
    )
    sql = database.requests[0]["Sql"]
    assert "AS DOUBLE PRECISION)" in sql
    assert "AS BOOLEAN)" in sql


class Scripted:
    """A client that answers one statement with a fixed result."""

    def __init__(self, columns: list[tuple[str, str]], record: list[dict[str, Any]]) -> None:
        self._metadata = [{"name": name, "typeName": kind} for name, kind in columns]
        self._record = record
        self.statement_id: str | None = "s-1"

    def execute_statement(self, **request: Any) -> dict[str, Any]:
        del request
        return {"Id": self.statement_id} if self.statement_id else {}

    def describe_statement(self, *, Id: str) -> dict[str, Any]:  # noqa: N803 - the SDK's name
        return {"Id": Id, "Status": "FINISHED"}

    def get_statement_result(self, *, Id: str) -> dict[str, Any]:  # noqa: N803 - the SDK's name
        del Id
        return {"ColumnMetadata": self._metadata, "Records": [self._record]}


async def scripted_source(tmp_path: Path, client: Scripted) -> RedshiftDataSource:
    queries_dir = tmp_path / "queries"
    queries_dir.mkdir()
    names = ", ".join(str(column["name"]) for column in client._metadata)
    (queries_dir / "everything.sql").write_text(
        f"-- description: One row.\n-- max_rows: 5\nSELECT {names} FROM t\n", encoding="utf-8"
    )
    source = RedshiftDataSource(
        "warehouse", options(queries_dir), offline_sessions(), FrozenClock(), client=client
    )
    await source.start()
    return source


async def test_values_come_back_as_the_types_of_their_columns(tmp_path: Path) -> None:
    client = Scripted(
        [
            ("opened", "date"),
            ("seen", "timestamp"),
            ("synced", "timestamptz"),
            ("balance", "numeric"),
            ("active", "bool"),
            ("score", "float8"),
            ("note", "varchar"),
            ("odd_date", "date"),
            ("odd_number", "numeric"),
        ],
        [
            {"stringValue": "2024-05-06"},
            {"stringValue": "2024-05-06 07:08:09"},
            {"stringValue": "2024-05-06 07:08:09+00"},
            {"stringValue": "10.50"},
            {"booleanValue": True},
            {"doubleValue": 0.25},
            {"stringValue": "2024-05-06"},
            {"stringValue": "not a date"},
            {"stringValue": "not a number"},
        ],
    )
    result = await (await scripted_source(tmp_path, client)).query("everything")
    assert result.rows == (
        (
            date(2024, 5, 6),
            datetime(2024, 5, 6, 7, 8, 9),  # noqa: DTZ001 - the column has no zone
            datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC),
            Decimal("10.50"),
            True,
            0.25,
            "2024-05-06",
            "not a date",
            "not a number",
        ),
    )


async def test_a_statement_that_gets_no_id_fails(tmp_path: Path) -> None:
    client = Scripted([("a", "int4")], [{"longValue": 1}])
    client.statement_id = None
    source = await scripted_source(tmp_path, client)
    with pytest.raises(AgentLibError, match="was not accepted"):
        await source.query("everything")


async def test_reading_stops_one_row_past_the_cap(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path)
    database.page_size = 1
    calls: list[str | None] = []
    read = database.get_statement_result

    def counting(*, Id: str, NextToken: str | None = None) -> dict[str, Any]:  # noqa: N803
        calls.append(NextToken)
        return read(Id=Id, NextToken=NextToken)

    database.get_statement_result = counting  # type: ignore[method-assign]
    result = await source.query("all_accounts", obligations=Obligations(max_rows=2))
    assert (len(result.rows), result.truncated) == (2, True)
    assert calls == [None, "1", "2"]


async def test_waiting_backs_off_and_stays_under_two_seconds_a_check(tmp_path: Path) -> None:
    source, database, waits = await started(tmp_path)
    database.polls_before_done = 12
    await source.query("all_accounts")
    assert waits.asked[:3] == pytest.approx([0.1, 0.15, 0.225])
    assert waits.asked == sorted(waits.asked)
    assert max(waits.asked) == 2.0
    assert len(waits.asked) == 12


async def test_a_statement_the_database_rejects_fails_without_its_message(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path)
    database.end_as = "FAILED"
    with pytest.raises(AgentLibError, match="statement-1") as caught:
        await source.query("accounts_by_region", {"region": "a-secret-value"})
    assert not isinstance(caught.value, TransientError | ConfigurationError)
    assert "a-secret-value" not in str(caught.value)
    assert "the database's own message" not in str(caught.value)


async def test_a_stopped_statement_can_be_tried_again(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path)
    database.end_as = "ABORTED"
    with pytest.raises(TransientError, match="stopped") as caught:
        await source.query("all_accounts")
    assert caught.value.retryable


async def test_a_statement_that_runs_too_long_is_cancelled(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path, timeout_seconds=0.05)
    database.end_as = "STARTED"
    with pytest.raises(TransientError, match=r"longer than 0\.05 seconds"):
        await source.query("all_accounts")
    assert database.cancelled == ["statement-1"]


async def test_a_cancelled_call_cancels_its_statement(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path)
    database.end_as = "STARTED"
    call = asyncio.ensure_future(source.query("all_accounts"))
    while not database.requests:
        await asyncio.sleep(0.005)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert database.cancelled == ["statement-1"]


@pytest.mark.parametrize(
    ("failure", "expected", "text"),
    [
        (client_error("ValidationException", "ExecuteStatement"), AgentLibError, "Validation"),
        (
            client_error("AccessDeniedException", "ExecuteStatement", status=403),
            ConfigurationError,
            None,
        ),
        (
            client_error("ThrottlingException", "ExecuteStatement", status=429),
            TransientError,
            None,
        ),
        (aws.EndpointConnectionError(endpoint_url="https://redshift-data"), TransientError, None),
        (aws.UnauthorizedSSOTokenError(), AgentLibError, None),
    ],
)
async def test_an_error_from_aws_is_mapped_and_never_copied(
    tmp_path: Path, failure: BaseException, expected: type[AgentLibError], text: str | None
) -> None:
    source, database, _ = await started(tmp_path)
    database.fail_with = failure
    with pytest.raises(expected, match=text) as caught:
        await source.query("accounts_by_region", {"region": "west"})
    assert "the service's own message" not in str(caught.value)


async def test_validation_runs_one_statement_against_the_database(tmp_path: Path) -> None:
    source, database, _ = await started(tmp_path)
    await source.validate()
    assert database.requests[0]["Sql"] == "SELECT 1 AS ready"
    assert "Parameters" not in database.requests[0]
    database.fail_with = client_error("ResourceNotFoundException", "ExecuteStatement")
    with pytest.raises(ConfigurationError, match="ResourceNotFoundException"):
        await source.validate()
    database.fail_with = client_error("AccessDeniedException", "ExecuteStatement", status=403)
    with pytest.raises(ConfigurationError):
        await source.validate()


async def test_a_source_that_is_not_started_or_has_no_queries_is_not_ready(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    data_dir, _ = write_accounts_files(tmp_path)
    source = RedshiftDataSource(
        "accounts",
        options(empty),
        offline_sessions(),
        FrozenClock(),
        client=FakeRedshiftData(data_dir),
    )
    with pytest.raises(ConfigurationError, match="not started"):
        await source.validate()
    with pytest.raises(RuntimeError, match="not started"):
        source.describe()
    await source.start()
    with pytest.raises(RuntimeError, match="already started"):
        await source.start()
    with pytest.raises(ConfigurationError, match="no queries"):
        await source.validate()
    await source.aclose()
    await source.aclose()
    assert "eap" in repr(source)


async def test_a_cluster_is_named_with_its_database_identity(tmp_path: Path) -> None:
    source, database, _ = await started(
        tmp_path, workgroup=None, cluster_id="warehouse", db_user="accounts_mcp"
    )
    await source.query("all_accounts")
    request = database.requests[0]
    assert (request["ClusterIdentifier"], request["DbUser"]) == ("warehouse", "accounts_mcp")
    assert "WorkgroupName" not in request
    assert "SecretArn" not in request


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workgroup": None}, "exactly one"),
        ({"cluster_id": "warehouse"}, "exactly one"),
        ({"db_user": "someone"}, "provisioned cluster"),
        (
            {"workgroup": None, "cluster_id": "c", "db_user": "u", "secret_arn": "arn:aws:s"},
            "not both",
        ),
        ({"database": ""}, "database"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"dialect": "postgres"}, "dialect"),
    ],
)
def test_options_name_one_target_and_one_identity(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        options(tmp_path, **changes)


async def test_a_service_gets_the_source_from_configuration(tmp_path: Path) -> None:
    _, queries_dir = write_accounts_files(tmp_path)
    fakes = Fakes()
    sessions = offline_sessions()
    config = ServiceConfig.for_testing(
        data_sources={
            "accounts": ProviderSelection(
                "redshift_data",
                {"queries_dir": str(queries_dir), "database": "dev", "workgroup": "eap"},
            )
        }
    )
    providers = register_aws_adapters(fakes.providers(), sessions=sessions)
    async with ServiceContainer(config, providers, clock=fakes.clock) as services:
        source = services.data_source("accounts")
        assert sorted(source.describe()) == ["accounts_by_region", "all_accounts"]

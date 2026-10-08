"""What is specific to the DuckDB CSV data source; the shared rules are in the contract suite."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import duckdb
import pytest

from ai_agent_lib_core.adapters import DuckDbCsvDataSource, DuckDbCsvOptions
from ai_agent_lib_core.contracts import (
    AgentLibError,
    Classification,
    ConfigurationError,
    DeploymentEnv,
    Principal,
    ProviderSelection,
    RequestContext,
    ServiceConfig,
    TransientError,
)
from ai_agent_lib_core.di import DATA_PORT, ServiceContainer, ServiceProviders
from ai_agent_lib_core.testing import Fakes, FrozenClock, write_accounts_files

SLOW_QUERY = (
    "-- description: Counts far more combinations than can finish in time.\n"
    "-- max_rows: 1\n"
    "SELECT COUNT(*) AS combinations\n"
    "FROM accounts a, accounts b, accounts c, accounts d, accounts e, accounts f,\n"
    "     accounts g, accounts h, accounts i, accounts j, accounts k, accounts l,\n"
    "     accounts m, accounts n\n"
)


def make_source(tmp_path: Path, **options: object) -> DuckDbCsvDataSource:
    data_dir, queries_dir = write_accounts_files(tmp_path)
    return DuckDbCsvDataSource(
        "accounts",
        DuckDbCsvOptions.model_validate(
            {"data_dir": data_dir, "queries_dir": queries_dir, **options}
        ),
        FrozenClock(),
    )


async def test_each_csv_file_becomes_a_table_named_after_it(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    (tmp_path / "data" / "branches.csv").write_text("branch,region\nB1,east\n", encoding="utf-8")
    (tmp_path / "queries" / "branches.sql").write_text(
        "-- description: Branches.\n-- max_rows: 10\nSELECT branch, region FROM branches\n",
        encoding="utf-8",
    )
    await source.start()
    try:
        result = await source.query("branches")
        assert result.as_dicts() == [{"branch": "B1", "region": "east"}]
        assert result.source is not None
        assert result.source.uri == "datasource://accounts/branches"
    finally:
        await source.aclose()


async def test_redshift_functions_run_locally(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    (tmp_path / "queries" / "holders.sql").write_text(
        "-- description: Holders, with a Redshift-only function.\n-- max_rows: 2\n"
        "SELECT NVL(holder, '?') AS Holder FROM accounts ORDER BY Holder DESC\n",
        encoding="utf-8",
    )
    await source.start()
    try:
        result = await source.query("holders")
        assert result.columns == ("holder",)
        assert [row[0] for row in result.rows] == ["Fay", "Eve"]
    finally:
        await source.aclose()


async def test_the_engine_cannot_read_files_after_loading(tmp_path: Path) -> None:
    secret = tmp_path / "secret.csv"
    secret.write_text("password\nhunter2\n", encoding="utf-8")
    source = make_source(tmp_path)
    await source.start()
    try:
        connection = source._connection
        assert connection is not None
        with pytest.raises(duckdb.PermissionException):
            connection.execute(f"SELECT * FROM read_csv_auto('{secret.as_posix()}')")
        with pytest.raises(duckdb.Error):
            connection.execute("SET enable_external_access = true")
    finally:
        await source.aclose()


async def test_a_query_that_does_not_fit_the_tables_stops_startup(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    (tmp_path / "queries" / "broken.sql").write_text(
        "-- description: Names a column that is not there.\n-- max_rows: 5\n"
        "SELECT account_id, iban FROM accounts\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="query 'broken' does not fit"):
        await source.start()


@pytest.mark.parametrize("file_name", ["2024-accounts.csv", "my accounts.csv"])
async def test_a_file_that_cannot_be_a_table_name_stops_startup(
    tmp_path: Path, file_name: str
) -> None:
    source = make_source(tmp_path)
    (tmp_path / "data" / file_name).write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="cannot be a table name"):
        await source.start()


async def test_missing_directories_are_configuration_errors(tmp_path: Path) -> None:
    _, queries_dir = write_accounts_files(tmp_path)
    no_data = DuckDbCsvDataSource(
        "accounts",
        DuckDbCsvOptions(data_dir=tmp_path / "absent", queries_dir=queries_dir),
        FrozenClock(),
    )
    with pytest.raises(ConfigurationError, match="the data folder does not exist"):
        await no_data.start()
    no_queries = DuckDbCsvDataSource(
        "accounts",
        DuckDbCsvOptions(data_dir=tmp_path / "data", queries_dir=tmp_path / "absent"),
        FrozenClock(),
    )
    with pytest.raises(ConfigurationError, match="the queries folder does not exist"):
        await no_queries.start()


async def test_a_missing_engine_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "duckdb", None)
    with pytest.raises(ConfigurationError, match=r"ai-agent-lib-core\[duckdb\]"):
        await make_source(tmp_path).start()


async def test_a_query_that_runs_too_long_is_stopped(tmp_path: Path) -> None:
    source = make_source(tmp_path, timeout_seconds=0.05)
    (tmp_path / "queries" / "slow.sql").write_text(SLOW_QUERY, encoding="utf-8")
    await source.start()
    try:
        with pytest.raises(TransientError, match=r"ran longer than 0\.05 seconds") as caught:
            await source.query("slow")
        assert caught.value.retryable
        # The engine was interrupted, so it is free for the next call straight away.
        quick = await asyncio.wait_for(source.query("all_accounts"), timeout=5)
        assert len(quick.rows) == 6
    finally:
        await source.aclose()


async def test_an_engine_error_does_not_repeat_values_in_its_message(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    (tmp_path / "queries" / "by_id.sql").write_text(
        "-- description: One account.\n-- param account: string\n-- max_rows: 1\n"
        "SELECT account_id FROM accounts WHERE account_id = CAST(:account AS INTEGER)\n",
        encoding="utf-8",
    )
    await source.start()
    try:
        with pytest.raises(AgentLibError) as caught:
            await source.query("by_id", {"account": "not-a-number-7731"})
        assert "7731" not in str(caught.value)
        assert isinstance(caught.value.__cause__, duckdb.Error)
        assert not caught.value.retryable
    finally:
        await source.aclose()


async def test_lifecycle_is_checked(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    with pytest.raises(RuntimeError, match="not started"):
        source.describe()
    with pytest.raises(ConfigurationError, match="not started"):
        await source.validate()
    await source.start()
    await source.validate()
    with pytest.raises(RuntimeError, match="already started"):
        await source.start()
    await source.aclose()
    await source.aclose()


async def test_a_source_with_no_queries_fails_validation(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    for path in (tmp_path / "queries").glob("*.sql"):
        path.unlink()
    await source.start()
    try:
        with pytest.raises(ConfigurationError, match="has no queries"):
            await source.validate()
    finally:
        await source.aclose()


def test_options_reject_unknown_keys_and_bad_timeouts(tmp_path: Path) -> None:
    selection = ProviderSelection(
        provider="duckdb_csv",
        options={"data_dir": str(tmp_path), "queries_dir": str(tmp_path), "timeout_seconds": 0},
    )
    with pytest.raises(ConfigurationError, match="timeout_seconds"):
        selection.parse_options(DuckDbCsvOptions)
    with pytest.raises(ConfigurationError, match="data_dir"):
        ProviderSelection(provider="duckdb_csv", options={"queries_dir": "q"}).parse_options(
            DuckDbCsvOptions
        )


# ------------------------------------------------------------------ container


def duckdb_config(tmp_path: Path, **overrides: object) -> ServiceConfig:
    data_dir, queries_dir = write_accounts_files(tmp_path)
    selection = ProviderSelection(
        provider="duckdb_csv",
        options={"data_dir": str(data_dir), "queries_dir": str(queries_dir)},
    )
    return ServiceConfig.for_testing(data_sources={"ledger": selection}, **overrides)


def providers_with_duckdb() -> ServiceProviders:
    default = ServiceProviders.default().lookup(DATA_PORT, "duckdb_csv")
    return (
        Fakes()
        .providers()
        .register(DATA_PORT, default.name, default.factory, local_only=default.local_only)
    )


async def test_the_container_builds_a_named_source_from_its_own_options(tmp_path: Path) -> None:
    fakes = Fakes()
    registry = providers_with_duckdb()
    async with ServiceContainer(duckdb_config(tmp_path), registry, clock=fakes.clock) as services:
        await services.validate()
        source = services.data_source("ledger")
        caller = RequestContext(
            principal=Principal(subject="u-1", tenant="t-1"),
            application="ledger-mcp",
            request_id="r-1",
            thread_id="th-1",
            classification_ceiling=Classification.RESTRICTED,
        )
        result = await source.query("accounts_by_region", {"region": "west"}, context=caller)
        assert result.source is not None
        assert result.source.source == "ledger"
        assert result.source.retrieved_at == fakes.clock.now()
    with pytest.raises(RuntimeError, match="not started"):
        source.describe()


@pytest.mark.parametrize("env", [DeploymentEnv.DEV, DeploymentEnv.PROD])
async def test_the_csv_source_is_refused_outside_local(tmp_path: Path, env: DeploymentEnv) -> None:
    config = duckdb_config(tmp_path, deployment_env=env)
    with pytest.raises(ConfigurationError, match="local development only"):
        await ServiceContainer(config, providers_with_duckdb()).start()

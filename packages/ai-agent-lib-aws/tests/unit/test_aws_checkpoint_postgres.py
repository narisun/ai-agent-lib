"""The PostgreSQL checkpoint store: IAM sign-in, TLS, and tables it never creates."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from ai_agent_lib_aws.checkpoint_postgres import (
    PostgresCheckpointBackend,
    PostgresCheckpointOptions,
)
from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_aws.testing import offline_session, offline_sessions
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    CredentialsExpiredError,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.testing import Fakes

HOST = "state.cluster-abc.eu-west-1.rds.amazonaws.com"
CURRENT = len(AsyncPostgresSaver.MIGRATIONS) - 1


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


class FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def execute(self, query: str) -> FakeCursor:
        self._pool.queries.append(query)
        if self._pool.query_fails is not None:
            raise self._pool.query_fails
        return FakeCursor(self._pool.row)


class FakePool:
    """Stands in for the connection pool: it records how it was built and used."""

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.row: dict[str, Any] | None = {"v": CURRENT}
        self.open_fails: BaseException | None = None
        self.query_fails: BaseException | None = None
        self.queries: list[str] = []
        self.opened: list[dict[str, Any]] = []
        self.closed = 0

    async def open(self, **arguments: Any) -> None:
        self.opened.append(arguments)
        if self.open_fails is not None:
            raise self.open_fails

    async def close(self) -> None:
        self.closed += 1

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[FakeConnection]:
        yield FakeConnection(self)


class Pools:
    """A pool factory that hands out one prepared pool."""

    def __init__(self) -> None:
        self.pool = FakePool()

    def __call__(self, **settings: Any) -> FakePool:
        self.pool.settings = settings
        return self.pool


def options(**changes: Any) -> PostgresCheckpointOptions:
    settings: dict[str, Any] = {"host": HOST, "database": "agents", "user": "accounts_agent"}
    settings.update(changes)
    return PostgresCheckpointOptions.model_validate(settings)


def backend(pools: Pools | None = None, **changes: Any) -> PostgresCheckpointBackend:
    return PostgresCheckpointBackend(
        options(**changes), offline_sessions(), pool_factory=pools or Pools()
    )


async def test_a_connection_signs_in_with_an_iam_token_over_checked_tls() -> None:
    arguments = await backend().connection_arguments()
    token = arguments.pop("password")
    assert arguments == {
        "host": HOST,
        "port": 5432,
        "dbname": "agents",
        "user": "accounts_agent",
        "application_name": "ai-agent-lib",
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        "sslmode": "verify-full",
        "sslrootcert": "system",
    }
    endpoint, _, query = token.partition("/?")
    signed = parse_qs(query)
    assert endpoint == f"{HOST}:5432"
    assert (signed["Action"], signed["DBUser"]) == (["connect"], ["accounts_agent"])
    assert "eu-west-1/rds-db/aws4_request" in signed["X-Amz-Credential"][0]
    assert signed["X-Amz-Signature"][0]


async def test_every_new_connection_gets_its_own_token() -> None:
    sessions = offline_sessions()
    signer = sessions.client("rds")
    sign = signer.generate_db_auth_token
    asked: list[dict[str, Any]] = []

    def counting(**arguments: Any) -> str:
        asked.append(arguments)
        return f"{sign(**arguments)}#{len(asked)}"

    signer.generate_db_auth_token = counting
    pools = Pools()
    store = PostgresCheckpointBackend(options(port=6543), sessions, pool_factory=pools)
    await store.start()
    connect = pools.pool.settings["kwargs"]
    first, second = await connect(), await connect()
    assert first["password"] != second["password"]
    assert asked[0] == {
        "DBHostname": HOST,
        "Port": 6543,
        "DBUsername": "accounts_agent",
        "Region": "eu-west-1",
    }


async def test_the_certificate_authorities_can_come_from_a_file(tmp_path: Path) -> None:
    bundle = tmp_path / "rds-global-bundle.pem"
    arguments = await backend(ca_bundle=bundle).connection_arguments()
    assert (arguments["sslmode"], arguments["sslrootcert"]) == ("verify-full", str(bundle))


async def test_only_code_can_turn_tls_off_or_replace_the_token() -> None:
    async def local_password() -> str:
        return "local"

    store = PostgresCheckpointBackend(
        options(), offline_sessions(), token_source=local_password, tls=False
    )
    arguments = await store.connection_arguments()
    assert arguments["password"] == "local"
    assert "sslmode" not in arguments
    with pytest.raises(ValueError, match="tls"):
        options(tls=False)


async def test_signing_in_needs_a_region() -> None:
    class NoRegion(AwsSessionFactory):
        @property
        def region(self) -> str | None:
            return None

    store = PostgresCheckpointBackend(options(), NoRegion(session_factory=offline_session))
    with pytest.raises(ConfigurationError, match="needs an AWS region"):
        await store.connection_arguments()


async def test_starting_opens_the_pool_and_checks_the_tables() -> None:
    pools = Pools()
    store = backend(pools, pool_min_size=2, pool_max_size=7, connect_timeout_seconds=3)
    with pytest.raises(RuntimeError, match="not started"):
        _ = store.checkpointer
    with pytest.raises(ConfigurationError, match="not started"):
        await store.validate()
    await store.start()
    settings = pools.pool.settings
    assert (settings["min_size"], settings["max_size"], settings["timeout"]) == (2, 7, 3)
    assert settings["open"] is False
    assert settings["max_lifetime"] == 1800
    assert pools.pool.opened == [{"wait": True, "timeout": 3}]
    assert pools.pool.queries == ["SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1"]
    assert isinstance(store.checkpointer, AsyncPostgresSaver)
    await store.validate()
    assert len(pools.pool.queries) == 2
    with pytest.raises(RuntimeError, match="already started"):
        await store.start()
    await store.aclose()
    await store.aclose()
    assert pools.pool.closed == 1
    assert HOST in repr(store)


@pytest.mark.parametrize(
    ("row", "message"),
    [(None, "version -1"), ({"v": CURRENT - 1}, f"needs version {CURRENT}")],
)
async def test_tables_that_are_out_of_date_stop_startup(
    row: dict[str, Any] | None, message: str
) -> None:
    pools = Pools()
    pools.pool.row = row
    with pytest.raises(ConfigurationError, match=message):
        await backend(pools).start()
    assert pools.pool.closed == 1


async def test_tables_from_a_newer_library_are_accepted() -> None:
    pools = Pools()
    pools.pool.row = {"v": CURRENT + 3}
    await backend(pools).start()


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (psycopg.errors.UndefinedTable("relation does not exist"), "do not exist"),
        (psycopg.errors.InsufficientPrivilege("permission denied"), "may not read"),
        (psycopg.OperationalError("the server's own message"), "could not be reached"),
        (PoolTimeout("no connection"), "could not be reached"),
        (CredentialsExpiredError("sign in again"), "sign in again"),
    ],
)
async def test_a_database_that_cannot_be_used_stops_startup(
    failure: BaseException, message: str
) -> None:
    pools = Pools()
    pools.pool.query_fails = failure
    with pytest.raises(ConfigurationError, match=message) as caught:
        await backend(pools).start()
    assert "the server's own message" not in str(caught.value)
    assert pools.pool.closed == 1


@pytest.mark.parametrize(
    "failure",
    [psycopg.OperationalError("the server's own message"), PoolTimeout("no connection")],
)
async def test_a_pool_that_cannot_open_stops_startup(failure: BaseException) -> None:
    pools = Pools()
    pools.pool.open_fails = failure
    with pytest.raises(ConfigurationError, match="could not be reached") as caught:
        await backend(pools).start()
    assert "the server's own message" not in str(caught.value)
    assert pools.pool.closed == 1
    assert pools.pool.queries == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"host": ""}, "host"),
        ({"port": 0}, "port"),
        ({"database": ""}, "database"),
        ({"user": ""}, "user"),
        ({"pool_min_size": 5, "pool_max_size": 2}, "pool_min_size"),
        ({"connect_timeout_seconds": 0}, "connect_timeout_seconds"),
        ({"password": "hunter2"}, "password"),
    ],
)
def test_options_are_checked(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        options(**changes)


async def test_a_service_selects_the_store_by_name() -> None:
    fakes = Fakes()
    providers = register_aws_adapters(fakes.providers(), sessions=offline_sessions())
    assert providers.lookup(Section.CHECKPOINT, "postgres").local_only is False
    config = ServiceConfig.for_testing(
        sections={Section.CHECKPOINT: ProviderSelection("postgres", {"host": HOST})}
    )
    with pytest.raises(ConfigurationError, match="database"):
        async with ServiceContainer(config, providers, clock=fakes.clock):
            pass

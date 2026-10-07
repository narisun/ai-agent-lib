"""The PostgreSQL checkpoint store against a real PostgreSQL server.

The server comes from the ``pgserver`` package, which carries its own
PostgreSQL and runs it from a temporary directory over a local socket. Nothing
has to be installed or started first::

    uv run pytest -m integration packages/ai-agent-lib-aws/tests/integration

The server trusts local connections, so these tests replace the IAM token and
turn TLS off, which only code can do. Signing in to Amazon RDS with IAM is not
covered here; it needs a real database.
"""

from __future__ import annotations

import itertools
import shutil
import tempfile
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ai_agent_lib_aws.checkpoint_postgres import (
    PostgresCheckpointBackend,
    PostgresCheckpointOptions,
)
from ai_agent_lib_aws.testing import offline_sessions
from ai_agent_lib_core.contracts import CheckpointBackend, ConfigurationError
from ai_agent_lib_core.testing.langgraph_contracts import CheckpointBackendContract

pytestmark = pytest.mark.integration

pgserver = pytest.importorskip("pgserver", reason="install pgserver to run these tests")

ADMIN = "postgres"
_numbers = itertools.count(1)


@pytest.fixture(scope="module")
def server() -> Iterator[Any]:
    # A short path: a socket's path has a small length limit.
    directory = Path(tempfile.mkdtemp(prefix="eap-pg-"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        running = pgserver.get_server(directory, cleanup_mode="stop")
    try:
        yield running
    finally:
        running.cleanup()
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def database(server: Any) -> str:
    """A new, empty database."""
    name = f"checkpoints_{next(_numbers)}"
    server.psql(f"CREATE DATABASE {name}")
    return name


async def _no_password() -> str:
    return ""


def store(server: Any, database: str, user: str = ADMIN) -> PostgresCheckpointBackend:
    options = PostgresCheckpointOptions(
        host=str(server.pgdata), database=database, user=user, pool_min_size=1, pool_max_size=4
    )
    return PostgresCheckpointBackend(
        options, offline_sessions(), token_source=_no_password, tls=False
    )


def new_role(server: Any) -> str:
    """Create a database role that can sign in and owns nothing."""
    role = f"service_{next(_numbers)}"
    server.psql(f"CREATE ROLE {role} LOGIN")
    return role


def grant_rows(server: Any, database: str, role: str) -> None:
    """Give ``role`` what a service needs: rows, and no way to change a table."""
    server.psql(
        f"\\c {database}\n"
        f"REVOKE CREATE ON SCHEMA public FROM PUBLIC; "
        f"GRANT SELECT ON checkpoint_migrations TO {role}; "
        f"GRANT SELECT, INSERT, UPDATE, DELETE "
        f"ON checkpoints, checkpoint_blobs, checkpoint_writes TO {role};"
    )


class TestPostgresCheckpointBackend(CheckpointBackendContract):
    """The whole suite, signed in as a role that can only read and write rows."""

    @pytest.fixture(autouse=True)
    def _database(self, server: Any, database: str) -> None:
        self._server, self._name = server, database

    async def make_backend(self, tmp_path: Path) -> CheckpointBackend:
        await store(self._server, self._name).create_tables()
        role = new_role(self._server)
        grant_rows(self._server, self._name, role)
        backend = store(self._server, self._name, user=role)
        await backend.start()
        return backend


async def test_a_service_does_not_start_without_its_tables(server: Any, database: str) -> None:
    backend = store(server, database)
    with pytest.raises(ConfigurationError, match="do not exist"):
        await backend.start()
    await backend.create_tables()
    await backend.create_tables()  # creating them again changes nothing
    await backend.start()
    await backend.validate()
    await backend.aclose()


async def test_a_role_without_grants_cannot_start_and_cannot_create_tables(
    server: Any, database: str
) -> None:
    role = new_role(server)
    server.psql(f"\\c {database}\nREVOKE CREATE ON SCHEMA public FROM PUBLIC;")
    with pytest.raises(ConfigurationError, match="could not be created"):
        await store(server, database, user=role).create_tables()
    await store(server, database).create_tables()
    with pytest.raises(ConfigurationError, match="may not read"):
        await store(server, database, user=role).start()
    grant_rows(server, database, role)
    service = store(server, database, user=role)
    await service.start()
    await service.aclose()


async def test_a_database_that_is_not_there_stops_startup(server: Any) -> None:
    with pytest.raises(ConfigurationError, match="could not be reached"):
        await store(server, "no_such_database").start()

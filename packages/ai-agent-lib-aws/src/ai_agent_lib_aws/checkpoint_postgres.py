"""A checkpoint store on PostgreSQL, for Amazon RDS and Aurora.

The service signs in with IAM database authentication: every new connection
gets a fresh, short-lived token signed with the service's own role, so there
is no database password to store or rotate. The connection is always TLS.

The service never creates or changes tables. An administrator does that once,
and again after an upgrade that needs it, with a database role that may:
see :meth:`PostgresCheckpointBackend.create_tables`. The service's own role
needs only to read and write rows.

This is the only module of the package that imports LangGraph.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Self

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import Field, model_validator

from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import AgentLibError, ConfigurationError, OptionsModel, describe

__all__ = ["PostgresCheckpointBackend", "PostgresCheckpointOptions"]

_WHAT = "the postgres checkpoint store"
_VERSION_QUERY = "SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1"
_NEEDED_VERSION = len(AsyncPostgresSaver.MIGRATIONS) - 1
_MAX_CONNECTION_SECONDS = 30 * 60.0

TokenSource = Callable[[], Awaitable[str]]
"""Returns the password for one new connection."""


class PostgresCheckpointOptions(OptionsModel):
    """Options of the ``postgres`` checkpoint store.

    Attributes:
        host: The database endpoint. The IAM token is signed for this exact
            name, so it must be the endpoint itself and not an alias of it.
        port: The database port.
        database: The database that holds the checkpoint tables.
        user: The database role the service signs in as. It must be allowed
            to sign in with IAM (``GRANT rds_iam TO ...``).
        ca_bundle: A file of the certificate authorities that sign the
            database's certificate. Amazon RDS needs its own bundle here. By
            default the system's authorities are used, which fits RDS Proxy.
        pool_min_size: Connections kept open while the service is idle.
        pool_max_size: The most connections the service opens.
        connect_timeout_seconds: How long to wait for a connection.
    """

    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    ca_bundle: Path | None = None
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _pool_sizes_agree(self) -> Self:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size cannot be larger than pool_max_size")
        return self


class PostgresCheckpointBackend:
    """Stores graph thread state in PostgreSQL.

    Args:
        options: Where the database is.
        sessions: Signs the IAM tokens.
        token_source: Returns the password for a new connection, in place of
            an IAM token. A test against a local database uses this.
        tls: Whether to require TLS and check the server's certificate. Only a
            test against a local database turns this off; configuration cannot.
        pool_factory: Builds the connection pool. A test replaces it.
    """

    def __init__(
        self,
        options: PostgresCheckpointOptions,
        sessions: AwsSessionFactory,
        *,
        token_source: TokenSource | None = None,
        tls: bool = True,
        pool_factory: Callable[..., Any] = AsyncConnectionPool,
    ) -> None:
        self._options = options
        self._sessions = sessions
        self._token_source = token_source if token_source is not None else self._iam_token
        self._tls = tls
        self._pool_factory = pool_factory
        self._pool: Any = None
        self._saver: AsyncPostgresSaver | None = None

    def __repr__(self) -> str:
        options = self._options
        return f"PostgresCheckpointBackend(host={options.host!r}, database={options.database!r})"

    async def start(self) -> None:
        """Open the connection pool and check that the tables are in place.

        Raises:
            ConfigurationError: If the database cannot be reached or signed in
                to, or its checkpoint tables are missing or out of date.
        """
        if self._pool is not None:
            raise RuntimeError("the checkpoint store is already started")
        options = self._options
        pool = self._pool_factory(
            kwargs=self.connection_arguments,
            min_size=options.pool_min_size,
            max_size=options.pool_max_size,
            timeout=options.connect_timeout_seconds,
            max_lifetime=_MAX_CONNECTION_SECONDS,
            name="eap-checkpoint",
            open=False,
        )
        try:
            await self._open(pool)
            await self._check_tables(pool)
        except BaseException:
            await pool.close()
            raise
        self._pool, self._saver = pool, AsyncPostgresSaver(pool)

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any]:
        """The PostgreSQL checkpointer.

        Raises:
            RuntimeError: If the store has not been started.
        """
        if self._saver is None:
            raise RuntimeError(f"{_WHAT} is not started")
        return self._saver

    async def validate(self) -> None:
        """Check that the database answers and its tables are in place.

        Raises:
            ConfigurationError: If it is not ready.
        """
        if self._pool is None:
            raise ConfigurationError(f"{_WHAT} is not started")
        await self._check_tables(self._pool)

    async def aclose(self) -> None:
        """Close every connection. Safe to call more than once."""
        pool, self._pool, self._saver = self._pool, None, None
        if pool is not None:
            await pool.close()

    async def create_tables(self) -> None:
        """Create or upgrade the checkpoint tables. An administrator's step.

        Run it with options that name a database role allowed to create tables
        and indexes. A service never calls this.

        Raises:
            ConfigurationError: If the database refuses.
        """
        arguments = await self.connection_arguments()
        try:
            async with await psycopg.AsyncConnection.connect(**arguments) as connection:
                await AsyncPostgresSaver(connection).setup()
        except psycopg.Error as exc:
            # The server's message can name things in the database: it is the detail.
            raise ConfigurationError(
                f"{_WHAT}: the tables could not be created ({type(exc).__name__})",
                fix="create them once as an administrator; the service never needs to",
                detail=describe(exc),
            ) from exc

    async def connection_arguments(self) -> dict[str, Any]:
        """Return what one new connection is opened with, a fresh token included."""
        options = self._options
        arguments: dict[str, Any] = {
            "host": options.host,
            "port": options.port,
            "dbname": options.database,
            "user": options.user,
            "password": await self._token_source(),
            "application_name": "ai-agent-lib",
            # The checkpointer needs these three.
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        }
        if self._tls:
            arguments["sslmode"] = "verify-full"
            arguments["sslrootcert"] = (
                str(options.ca_bundle) if options.ca_bundle is not None else "system"
            )
        return arguments

    async def _iam_token(self) -> str:
        options = self._options
        region = self._sessions.region
        if region is None:
            raise ConfigurationError(f"{_WHAT} needs an AWS region to sign in to the database")
        token = await self._sessions.invoke(
            _WHAT,
            self._sessions.client("rds").generate_db_auth_token,
            DBHostname=options.host,
            Port=options.port,
            DBUsername=options.user,
            Region=region,
        )
        return str(token)

    async def _open(self, pool: Any) -> None:
        try:
            await pool.open(wait=True, timeout=self._options.connect_timeout_seconds)
        except (psycopg.Error, PoolTimeout) as exc:
            raise self._unreachable(exc) from exc
        except AgentLibError as exc:
            raise ConfigurationError.from_error(exc) from exc

    def _unreachable(self, error: BaseException) -> ConfigurationError:
        # The server's message can name roles and databases: it is the detail.
        return ConfigurationError(
            f"{_WHAT}: the database at {self._options.host!r} could not be reached or "
            f"signed in to ({type(error).__name__})",
            fix=(
                "check the host and port, that this task's security group can reach the "
                "database, and that the role may sign in with IAM (rds-db:connect)"
            ),
            detail=describe(error),
        )

    async def _check_tables(self, pool: Any) -> None:
        try:
            async with pool.connection() as connection:
                cursor = await connection.execute(_VERSION_QUERY)
                row = await cursor.fetchone()
        except psycopg.errors.UndefinedTable:
            raise ConfigurationError(
                f"{_WHAT}: the checkpoint tables do not exist. An administrator creates them "
                "once; the service does not."
            ) from None
        except psycopg.errors.InsufficientPrivilege:
            raise ConfigurationError(
                f"{_WHAT}: database role {self._options.user!r} may not read the checkpoint tables"
            ) from None
        except (psycopg.Error, PoolTimeout) as exc:
            raise self._unreachable(exc) from exc
        except AgentLibError as exc:
            raise ConfigurationError.from_error(exc) from exc
        version = row["v"] if row is not None else -1
        if version < _NEEDED_VERSION:
            raise ConfigurationError(
                f"{_WHAT}: the checkpoint tables are at version {version} and this library "
                f"needs version {_NEEDED_VERSION}. An administrator upgrades them."
            )

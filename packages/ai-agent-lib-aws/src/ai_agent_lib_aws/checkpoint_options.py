"""PostgreSQL configuration schemas, available without the optional database driver."""

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from ai_agent_lib_core.contracts import OptionsModel

__all__ = ["PostgresCheckpointOptions"]


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

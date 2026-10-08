"""Which Redshift database a Data API call is for, and as whom.

The data source that runs queries and the reader of the catalogue name the
database the same way, so the rules and the request arguments are here once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_agent_lib_core.contracts import ConfigurationError

__all__ = ["RedshiftTarget", "target_problem"]


def target_problem(
    *, workgroup: str | None, cluster_id: str | None, db_user: str | None, secret_arn: str | None
) -> str | None:
    """Return what is wrong with how a database is named, or ``None`` if nothing is.

    Name either a serverless workgroup or a provisioned cluster. With a
    workgroup the caller's IAM role is the database user. A cluster also takes
    ``db_user`` or ``secret_arn``.
    """
    if (workgroup is None) == (cluster_id is None):
        return "set exactly one of workgroup and cluster_id"
    if db_user is not None and cluster_id is None:
        return "db_user applies to a provisioned cluster; set cluster_id"
    if db_user is not None and secret_arn is not None:
        return "set db_user or secret_arn, not both"
    return None


@dataclass(frozen=True, slots=True)
class RedshiftTarget:
    """A database, and the identity to use it with.

    Attributes:
        database: The database.
        workgroup: The name of a Redshift Serverless workgroup.
        cluster_id: The identifier of a provisioned cluster.
        db_user: The database user, on a provisioned cluster.
        secret_arn: A Secrets Manager secret that holds database credentials.

    Raises:
        ConfigurationError: If the database is not named, or the rest does not
            name exactly one place and one identity.
    """

    database: str
    workgroup: str | None = None
    cluster_id: str | None = None
    db_user: str | None = None
    secret_arn: str | None = None

    def __post_init__(self) -> None:
        problem = (
            "name the database"
            if not self.database
            else target_problem(
                workgroup=self.workgroup,
                cluster_id=self.cluster_id,
                db_user=self.db_user,
                secret_arn=self.secret_arn,
            )
        )
        if problem is not None:
            raise ConfigurationError(f"the Redshift database: {problem}")

    @property
    def place(self) -> str:
        """The workgroup or the cluster, for a message."""
        return self.workgroup or self.cluster_id or ""

    def arguments(self) -> dict[str, Any]:
        """Return the arguments that name the database in a Data API call."""
        optional = {
            "WorkgroupName": self.workgroup,
            "ClusterIdentifier": self.cluster_id,
            "DbUser": self.db_user,
            "SecretArn": self.secret_arn,
        }
        named = {key: value for key, value in optional.items() if value is not None}
        return {"Database": self.database, **named}

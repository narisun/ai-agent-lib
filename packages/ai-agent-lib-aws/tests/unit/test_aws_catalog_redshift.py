"""Reading tables and columns from the Redshift catalogue through the Data API."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.stub import Stubber

from ai_agent_lib_aws.catalog_redshift import CatalogColumn, CatalogTable, read_redshift_catalog
from ai_agent_lib_aws.redshift_target import RedshiftTarget
from ai_agent_lib_aws.testing import offline_sessions
from ai_agent_lib_core.contracts import ConfigurationError, CredentialsExpiredError

TARGET = RedshiftTarget(database="dev", workgroup="eap")
WHERE = {"Database": "dev", "WorkgroupName": "eap"}


def table(name: str, kind: str = "TABLE", schema: str = "sales") -> dict[str, str]:
    return {"name": name, "schema": schema, "type": kind}


def column(name: str, type_name: str) -> dict[str, Any]:
    return {"name": name, "typeName": type_name, "nullable": 1}


async def test_the_requests_and_the_replies_have_the_shapes_of_the_real_service() -> None:
    sessions = offline_sessions()
    listing = {**WHERE, "SchemaPattern": "sales", "MaxResults": 100}
    describing = {**WHERE, "Schema": "sales", "MaxResults": 100}
    with Stubber(sessions.client("redshift-data")) as service:
        # The list comes in pages. System tables and other schemas the pattern
        # happened to match are not part of the answer.
        service.add_response(
            "list_tables",
            {"Tables": [table("orders"), table("pg_thing", "SYSTEM TABLE")], "NextToken": "t1"},
            listing,
        )
        service.add_response(
            "list_tables",
            {"Tables": [table("customers", "VIEW"), table("orders", schema="sales_old")]},
            {**listing, "NextToken": "t1"},
        )
        service.add_response(
            "describe_table",
            {"TableName": "customers", "ColumnList": [column("customer_id", "int8")]},
            {**describing, "Table": "customers"},
        )
        service.add_response(
            "describe_table",
            {"ColumnList": [column("order_id", "int8")], "NextToken": "c1"},
            {**describing, "Table": "orders"},
        )
        service.add_response(
            "describe_table",
            {"ColumnList": [column("placed", "timestamptz"), {"name": "note"}]},
            {**describing, "Table": "orders", "NextToken": "c1"},
        )
        found = await read_redshift_catalog(sessions, TARGET, schema="sales")
        service.assert_no_pending_responses()

    assert found == (
        CatalogTable("sales", "customers", (CatalogColumn("customer_id", "int8"),)),
        CatalogTable(
            "sales",
            "orders",
            (
                CatalogColumn("order_id", "int8"),
                CatalogColumn("placed", "timestamptz"),
                CatalogColumn("note", ""),
            ),
        ),
    )


async def test_only_the_named_tables_are_described_and_a_missing_one_is_an_error() -> None:
    sessions = offline_sessions()
    target = RedshiftTarget(database="dev", cluster_id="c-1", db_user="reader")
    where = {"Database": "dev", "ClusterIdentifier": "c-1", "DbUser": "reader"}
    listed = {"Tables": [table("a"), table("b"), table("c")]}
    with Stubber(sessions.client("redshift-data")) as service:
        listing = {**where, "SchemaPattern": "sales", "MaxResults": 100}
        service.add_response("list_tables", listed, listing)
        service.add_response(
            "describe_table",
            {"ColumnList": [column("id", "int4")]},
            {**where, "Schema": "sales", "Table": "b", "MaxResults": 100},
        )
        # Nothing is described when the names or the number of tables are wrong.
        service.add_response("list_tables", listed, listing)
        service.add_response("list_tables", listed, listing)
        (only,) = await read_redshift_catalog(sessions, target, schema="sales", tables=["b"])
        assert only.name == "b"
        with pytest.raises(ConfigurationError, match="not in the schema 'sales': x, z"):
            await read_redshift_catalog(sessions, target, schema="sales", tables=["z", "b", "x"])
        with pytest.raises(ConfigurationError, match="has 3 tables; name the ones to read"):
            await read_redshift_catalog(sessions, target, schema="sales", max_tables=2)
        service.assert_no_pending_responses()


async def test_what_aws_refuses_is_reported_in_the_librarys_own_terms() -> None:
    sessions = offline_sessions(profile=None)
    with Stubber(sessions.client("redshift-data")) as service:
        service.add_client_error("list_tables", "AccessDeniedException", http_status_code=403)
        with pytest.raises(ConfigurationError, match="the Redshift catalogue"):
            await read_redshift_catalog(sessions, TARGET, schema="sales")
        service.add_client_error("list_tables", "ExpiredTokenException", http_status_code=403)
        with pytest.raises(CredentialsExpiredError):
            await read_redshift_catalog(sessions, TARGET, schema="sales")


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"database": ""}, "name the database"),
        ({"database": "dev"}, "set exactly one of workgroup and cluster_id"),
        ({"database": "dev", "workgroup": "w", "cluster_id": "c"}, "exactly one"),
        ({"database": "dev", "workgroup": "w", "db_user": "u"}, "applies to a provisioned cluster"),
        (
            {"database": "dev", "cluster_id": "c", "db_user": "u", "secret_arn": "arn:x"},
            "not both",
        ),
    ],
)
def test_a_target_that_makes_no_sense_is_refused(settings: dict[str, Any], reason: str) -> None:
    with pytest.raises(ConfigurationError, match=reason):
        RedshiftTarget(**settings)


def test_a_secret_can_name_the_database_identity() -> None:
    target = RedshiftTarget(database="dev", cluster_id="c", secret_arn="arn:aws:secret")
    assert target.arguments() == {
        "Database": "dev",
        "ClusterIdentifier": "c",
        "SecretArn": "arn:aws:secret",
    }

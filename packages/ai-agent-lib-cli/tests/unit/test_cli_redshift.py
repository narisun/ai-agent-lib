"""Servers over Redshift tables: from the catalogue to stand-in data and a plan."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.read_redshift import (
    CatalogTableLike,
    RedshiftRequest,
    fetch_catalog,
    stand_in_csv,
    tables_from_catalog,
)
from ai_agent_lib_cli.readers import from_redshift
from ai_agent_lib_cli.testing import toolbox_for_tests
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import load_answers
from ai_agent_lib_core.contracts import ParameterType


@dataclass(frozen=True)
class Column:
    name: str
    type_name: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]


ORDERS = Table(
    "orders",
    (
        Column("order_id", "int8"),
        Column("customer_id", "int4"),
        Column("state", "varchar"),
        Column("total", "numeric(12,2)"),
        Column("paid", "bool"),
        Column("placed", "timestamptz"),
        Column("ship_date", "date"),
        Column("contact_email", "character varying"),
        Column("payload", "super"),
        Column("Order Note", "varchar"),
        Column("user", "varchar"),
    ),
)
READINGS = Table("readings", (Column("taken", "timestamp"), Column("value", "float8")))
ODD = Table("Monthly Totals", (Column("id", "int4"),))
REQUEST = RedshiftRequest(schema="sales", database="dev", workgroup="eap")


# No formatter and no server processes: not what these tests are about.
TOOLS = toolbox_for_tests(pins={})


def run(*arguments: str | Path, tools: Toolbox = TOOLS) -> int:
    return main([str(argument) for argument in arguments], toolbox=tools)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert run("init", "demo", "--dir", tmp_path, "--owner", "demo-team") == 0
    return tmp_path / "demo"


def test_catalogue_types_become_parameter_types_and_every_column_gets_an_example() -> None:
    (orders, readings), notes = tables_from_catalog([ORDERS, READINGS, ODD])
    assert [(c.name, c.type) for c in orders.columns] == [
        ("order_id", ParameterType.INTEGER),
        ("customer_id", ParameterType.INTEGER),
        ("state", ParameterType.STRING),
        ("total", ParameterType.NUMBER),
        ("paid", ParameterType.BOOLEAN),
        ("placed", ParameterType.TIMESTAMP),
        ("ship_date", ParameterType.DATE),
        ("contact_email", ParameterType.STRING),
        ("payload", None),
    ]
    by_name = {c.name: c for c in orders.columns}
    assert by_name["order_id"].examples == (1,)
    assert by_name["state"].examples == ("state-1",)
    # Nothing was counted: only the catalogue was read.
    assert (orders.rows, by_name["state"].distinct) == (None, None)
    assert readings.name == "readings"
    assert notes == (
        "orders: left out 2 column(s) whose names a query cannot use as they are: Order Note, user",
        "left out the table Monthly Totals: a query cannot use its name as it is",
    )


def test_the_stand_in_file_has_the_columns_and_two_made_up_rows() -> None:
    (orders, _), _ = tables_from_catalog([ORDERS, READINGS])
    assert stand_in_csv(orders) == (
        "order_id,customer_id,state,total,paid,placed,ship_date,contact_email,payload\n"
        "1,1,state-1,1.5,true,2026-01-01 00:00:00,2026-01-01,person1@example.test,payload-1\n"
        "2,2,state-2,2.5,false,2026-01-02 00:00:00,2026-01-02,person2@example.test,payload-2\n"
    )


async def test_from_redshift_proposes_queries_that_ran_over_the_stand_in_data() -> None:
    proposal = await from_redshift([ORDERS, READINGS, ODD], REQUEST, source="sales")
    plan = proposal.plan
    assert (plan.origin, plan.kind, plan.options) == ("redshift", "duckdb_csv", {})
    # A lookup by the column whose name says it is the key; nothing was counted,
    # so no filters. A table without such a column is offered whole.
    assert [q.name for q in plan.queries] == ["orders_by_order_id", "readings_all"]
    lookup = plan.queries[0]
    assert lookup.parameters[0].example == 1
    assert lookup.returns_rows
    assert (lookup.masked, lookup.classification) == (("contact_email",), "confidential")
    assert "payload" in lookup.columns

    # What a deployed server needs is recorded with the plan.
    assert plan.deployed == {
        "kind": "redshift_data",
        "database": "dev",
        "workgroup": "eap",
        "queries_dir": "queries",
    }
    assert plan.deployed_setting == (
        '{"sales": {"kind": "redshift_data", "database": "dev", "workgroup": "eap", '
        '"queries_dir": "queries"}}'
    )
    assert "search path of the server's database user to `sales`" in plan.notes[0]
    assert sorted(path.as_posix() for path in proposal.extra_files) == [
        "data/orders.csv",
        "data/readings.csv",
    ]
    assert proposal.summary == (
        "Read 2 tables of dev.sales: orders (9 columns), readings (2 columns)"
    )
    assert proposal.description == "Read-only tools over the sales tables."
    assert len(proposal.notes) == 2

    with pytest.raises(CliError, match=r"nothing could be proposed from the tables of dev\.sales"):
        await from_redshift([ODD], REQUEST, source="sales")


def test_a_cluster_request_names_the_cluster_and_the_user_for_the_deployed_server() -> None:
    request = RedshiftRequest("sales", "dev", cluster_id="c-1", db_user="reader", profile="sso")
    assert request.deployed() == {
        "kind": "redshift_data",
        "database": "dev",
        "cluster_id": "c-1",
        "db_user": "reader",
        "queries_dir": "queries",
    }


async def test_a_request_that_cannot_be_made_is_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # Refused before anything is sent: no target is named.
    with pytest.raises(CliError, match="set exactly one of workgroup and cluster_id"):
        await fetch_catalog(RedshiftRequest(schema="sales", database="dev"))
    monkeypatch.setitem(sys.modules, "ai_agent_lib_aws.catalog_redshift", None)
    with pytest.raises(CliError, match="needs the AWS package; install ai-agent-lib-aws"):
        await fetch_catalog(REQUEST)


def test_new_mcp_from_redshift_writes_a_server_that_runs_locally_on_stand_ins(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[RedshiftRequest] = []

    async def catalogue(request: RedshiftRequest) -> Sequence[CatalogTableLike]:
        asked.append(request)
        return [ORDERS, READINGS]

    tools = toolbox_for_tests(pins={}, fetch_catalog=catalogue)
    command = (
        "new", "mcp", "sales-mcp", "--from-redshift", "sales", "--database", "dev",
        "--workgroup", "eap", "--table", "orders", "--table", "readings",
        "--aws-profile", "dev-sso", "--aws-region", "eu-west-1", "--workspace", workspace,
    )  # fmt: skip
    capsys.readouterr()
    assert run(*command, tools=tools) == 0
    out = capsys.readouterr().out
    assert "Read 2 tables of dev.sales" in out
    assert "sales.orders_by_order_id(order_id: int)   masks contact_email for analysts" in out
    assert asked == [
        RedshiftRequest(
            schema="sales",
            database="dev",
            workgroup="eap",
            profile="dev-sso",
            region="eu-west-1",
            tables=("orders", "readings"),
        )
    ]

    service = workspace / "mcp-servers/sales-mcp"
    assert (service / "data/orders.csv").read_text(encoding="utf-8").startswith("order_id,")
    assert (
        (service / "queries/orders_by_order_id.sql")
        .read_text(encoding="utf-8")
        .endswith("FROM orders\nWHERE order_id = :order_id\nORDER BY order_id\n")
    )
    # Locally it is a CSV server like any other.
    assert '"sales": {"kind": "duckdb_csv"' in (service / ".env.example").read_text()
    readme = (service / "README.md").read_text(encoding="utf-8")
    assert "proposed from the Redshift catalogue" in readme
    assert "## Before it is deployed" in readme
    assert "search path of the server's database user to `sales`" in readme
    assert '{"sales": {"kind": "redshift_data", "database": "dev", "workgroup": "eap"' in readme
    assert "EAP_DATA_SOURCES" in readme
    tests = (service / "tests/test_sales_mcp.py").read_text(encoding="utf-8")
    assert "{'order_id': 1}" in tests
    assert "rest_stub_providers" not in tests

    plan = load_answers(workspace).mcp_servers[0].plan
    assert (plan.origin, plan.deployed["workgroup"], len(plan.notes)) == ("redshift", "eap", 2)
    assert run("update", "--workspace", workspace) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_the_redshift_options_are_checked_before_anything_is_read(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert run("new", "mcp", "s-mcp", "--from-redshift", "sales", "--workspace", workspace) == 1
    assert "--from-redshift needs --database" in capsys.readouterr().err
    assert (
        run("new", "mcp", "s-mcp", "--workgroup", "eap", "--table", "t", "--workspace", workspace)
        == 1
    )
    assert "which was not given: --workgroup, --table" in capsys.readouterr().err
    assert not (workspace / "mcp-servers/s-mcp").exists()

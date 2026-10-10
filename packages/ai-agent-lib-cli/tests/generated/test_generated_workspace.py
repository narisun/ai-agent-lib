"""What the commands generate works: its tests pass, and it is clean code.

A workspace is generated once, with every kind of service and a long name,
and its own tests, linter, formatter check and type check are run on it the
way a developer would run them. This is what keeps the templates and the
library from drifting apart: a change to the library that breaks generated
code fails here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.read_redshift import CatalogTableLike, RedshiftRequest
from ai_agent_lib_cli.shared import read_servers
from ai_agent_lib_cli.toolbox import Toolbox

COMMANDS = (
    ("new", "mcp", "hello-mcp"),
    ("new", "agent", "hello-agent", "--mcp", "hello"),
    # A second pair, with long names, linked after the fact, and a real model provider.
    ("new", "agent", "customer-onboarding-agent", "--model", "anthropic"),
    ("new", "mcp", "claims-and-policy-lookup-mcp", "--server-id", "claims"),
    ("link", "customer-onboarding-agent", "claims"),
    ("link", "customer-onboarding-agent", "hello-mcp"),
)


# The developer's own data: a file name and column names that need care, a
# column that looks personal in each table, and one table with none.
EXPORTS = {
    "Orders 2026.csv": (
        "order_id,customer_id,state,total,placed,email,order,Sales Rep\n"
        "7001,501,new,120.50,2026-01-04,ana@example.test,1,Kim Lee\n"
        "7002,502,shipped,80.00,2026-01-09,bo@example.test,2,Kim Lee\n"
        "7003,501,new,45.10,2026-02-11,ana@example.test,3,Raj Rao\n"
        "7004,503,held,990.00,2026-03-02,cy@example.test,4,Raj Rao\n"
    ),
    "customers.csv": (
        "customer_id,name,segment,phone\n"
        "501,Ana Abel,retail,555-0101\n"
        "502,Bo Brook,retail,555-0102\n"
        "503,Cy Carr,business,555-0103\n"
    ),
    "readings.csv": "taken,value\n2026-01-01 10:00:00,1.5\n2026-01-01 11:00:00,2.5\n",
}


RATES = Path(__file__).resolve().parents[1] / "unit" / "data" / "rates.yaml"


@dataclass(frozen=True)
class Column:
    name: str
    type_name: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]


async def catalogue(request: RedshiftRequest) -> Sequence[CatalogTableLike]:
    """What the Redshift catalogue would say about the schema."""
    assert (request.schema, request.database, request.workgroup) == ("sales", "dev", "eap")
    return [
        Table(
            "invoices",
            (
                Column("invoice_id", "int8"),
                Column("amount", "numeric(12,2)"),
                Column("paid", "bool"),
                Column("issued", "timestamptz"),
                Column("due", "date"),
                Column("billing_email", "varchar"),
                Column("user", "varchar"),
            ),
        ),
        Table("rates_daily", (Column("day", "date"), Column("rate", "float8"))),
    ]


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    parent = tmp_path_factory.mktemp("generated")
    assert main(["init", "demo", "--dir", str(parent), "--owner", "demo-team"]) == 0
    root = parent / "demo"
    for command in COMMANDS:
        assert main([*command, "--workspace", str(root)]) == 0, command
    # A third server, over the developer's own CSV files, and an agent that calls it.
    exports = parent / "exports"
    exports.mkdir()
    for name, text in EXPORTS.items():
        (exports / name).write_text(text, encoding="utf-8")
    for command in (
        ("new", "mcp", "orders-mcp", "--from-csv", str(exports)),
        ("link", "hello-agent", "orders"),
        # A fourth, over a REST API that is described by an OpenAPI document.
        ("new", "mcp", "rates-mcp", "--from-openapi", str(RATES)),
        ("link", "hello-agent", "rates"),
    ):
        assert main([*command, "--workspace", str(root)]) == 0, command
    # A fifth, over Redshift tables. The catalogue is the one thing that is not real here.
    sales = ["new", "mcp", "sales-mcp", "--from-redshift", "sales", "--database", "dev"]
    with_catalogue = Toolbox(fetch_catalog=catalogue)
    assert main([*sales, "--workgroup", "eap", "--workspace", str(root)], with_catalogue) == 0
    return root


def run_in(workspace: Path, *command: str) -> subprocess.CompletedProcess[str]:
    """Run a tool in the workspace, with its services importable as if installed."""
    sources = sorted(str(path) for path in workspace.glob("*/*/src"))
    environment = dict(os.environ)  # the tool needs the caller's environment to start at all
    environment["PYTHONPATH"] = os.pathsep.join(sources)
    environment.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(  # noqa: S603 - this interpreter and fixed arguments
        [sys.executable, "-m", *command],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def test_every_tool_of_every_server_was_pinned_from_the_running_code(workspace: Path) -> None:
    servers = read_servers(workspace)
    assert [server.id for server in servers] == ["claims", "hello", "orders", "rates", "sales"]
    assert [tool.name for tool in servers[4].tools] == [
        "sales.invoices_by_invoice_id",
        "sales.rates_daily_all",
    ]
    assert [tool.name for tool in servers[3].tools] == [
        "rates.list_currencies",
        "rates.currencies_by_currency_code",
        "rates.traders_of_desk",
    ]
    assert sorted(tool.name for tool in servers[2].tools) == sorted(
        [
            "orders.orders_2026_by_order_id",
            "orders.orders_2026_by_customer_id",
            "orders.orders_2026_by_state",
            "orders.customers_by_customer_id",
            "orders.customers_by_segment",
            "orders.readings_all",
        ]
    )
    for server in servers:
        assert all(tool.schema_sha256 for tool in server.tools), server.id
    # The same arguments give the same pin, whatever the server is called.
    assert [t.schema_sha256 for t in servers[0].tools] == [
        t.schema_sha256 for t in servers[1].tools
    ]


def test_the_generated_tests_pass(workspace: Path) -> None:
    done = run_in(workspace, "pytest", "-q", "-p", "no:cacheprovider")
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
    # Success is what counts, not a total that changes whenever a template gains a test.
    # Every generated test file must have run, and nothing may fail, error or be skipped.
    summary = done.stdout.strip().splitlines()[-1]
    assert re.search(r"\d+ passed", summary), summary
    assert not re.search(r"failed|error|skipped", summary), summary
    collected = run_in(workspace, "pytest", "--collect-only", "-q", "-p", "no:cacheprovider")
    files = {line.split("::", 1)[0] for line in collected.stdout.splitlines() if "::" in line}
    generated = {
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("test_*.py")
        if ".venv" not in path.parts and not path.name.endswith("_eval.py")
    }
    assert generated <= files, sorted(generated - files)


def test_the_generated_code_is_lint_clean_and_formatted(workspace: Path) -> None:
    lint = run_in(workspace, "ruff", "check", "--no-cache", ".")
    assert lint.returncode == 0, lint.stdout[-4000:]
    formatted = run_in(workspace, "ruff", "format", "--check", "--no-cache", ".")
    assert formatted.returncode == 0, formatted.stdout[-4000:]


@pytest.mark.integration
def test_the_generated_code_passes_a_strict_type_check(workspace: Path) -> None:
    done = run_in(workspace, "mypy", "--no-incremental", "--cache-dir", os.devnull)
    assert done.returncode == 0, done.stdout[-4000:]
    assert "no issues found" in done.stdout


def agentlib(workspace: Path, *arguments: str) -> int:
    return main([*arguments, "--workspace", str(workspace)])


def test_doctor_finds_nothing_wrong_with_a_new_workspace(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert agentlib(workspace, "doctor") == 0
    out = capsys.readouterr().out
    for service in ("hello-agent", "hello-mcp", "customer-onboarding-agent"):
        assert service in out
    assert "FAIL" not in out


def test_the_generated_policy_samples_are_decided_as_expected(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert agentlib(workspace, "policy", "test") == 0
    out = capsys.readouterr().out
    # 5 servers x 4, 2 agents x 3, 5 links x 1.
    assert "31 samples" in out, out[-500:]
    assert "unexpected" not in out


def test_the_graph_of_a_generated_agent_can_be_drawn(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert agentlib(workspace, "graph", "customer-onboarding-agent") == 0
    out = capsys.readouterr().out
    assert "graph TD" in out
    assert "tools" in out


def test_update_leaves_a_workspace_made_by_this_version_alone(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    capsys.readouterr()
    assert agentlib(workspace, "update") == 0
    assert "nothing to do" in capsys.readouterr().out.lower()
    assert {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()} == before


@pytest.mark.integration
@pytest.mark.enable_socket
def test_the_rules_and_opa_agree_on_every_generated_sample(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert agentlib(workspace, "policy", "test", "--opa") == 0
    out = capsys.readouterr().out
    assert "\nopa\n" in out
    assert "31 samples came out as expected. The rules engine and OPA agree" in out

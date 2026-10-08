"""Servers over the developer's own data: the plan, the proposals and the CSV reader."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.dataplan import DataPlan, PlannedParameter, PlannedQuery, sample_plan
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.proposals import (
    ColumnInfo,
    TableInfo,
    is_plain_name,
    is_sensitive,
    propose_queries,
)
from ai_agent_lib_cli.read_csv import read_csv_folder, table_name_for
from ai_agent_lib_cli.readers import check_queries, csv_source, from_csv, selected
from ai_agent_lib_cli.render import TemplateRenderer
from ai_agent_lib_cli.shared import read_servers
from ai_agent_lib_cli.testing import toolbox_for_tests
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import (
    LibrarySource,
    McpAnswers,
    WorkspaceAnswers,
    load_answers,
)
from ai_agent_lib_core.contracts import Classification, ParameterType

RULES = Path("policies/agentlib/rules/data.yaml")
SAMPLES = Path("tests/policy-samples.yaml")

CLAIMS = """\
claim_id,customer_id,status,amount,opened,email,order,Adjuster Name
9001,501,open,1200.50,2026-01-04,ana@example.test,1,Kim Lee
9002,502,closed,80.00,2026-01-09,bo@example.test,2,Kim Lee
9003,501,open,455.10,2026-02-11,ana@example.test,3,Raj Rao
9004,503,review,9900.00,2026-03-02,cy@example.test,4,Raj Rao
9005,504,closed,15.25,2026-03-20,di@example.test,5,Kim Lee
"""
CUSTOMERS = """\
customer_id,name,segment,phone,joined
501,Ana Abel,retail,555-0101,2020-05-01
502,Bo Brook,retail,555-0102,2021-06-11
503,Cy Carr,business,555-0103,2019-02-21
504,Di Dalal,business,555-0104,2023-08-30
"""


# No formatter and no server processes: not what these tests are about.
TOOLS = toolbox_for_tests(pins={})


def run(*arguments: str | Path, tools: Toolbox = TOOLS) -> int:
    return main([str(argument) for argument in arguments], toolbox=tools)


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "Claims 2026.csv").write_text(CLAIMS, encoding="utf-8")
    (folder / "customers.CSV").write_text(CUSTOMERS, encoding="utf-8")
    (folder / "notes.txt").write_text("not a table", encoding="utf-8")
    return folder


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert run("init", "demo", "--dir", tmp_path, "--owner", "demo-team") == 0
    return tmp_path / "demo"


def column(
    name: str,
    kind: ParameterType | None = ParameterType.STRING,
    distinct: int | None = None,
    *examples: str | int,
) -> ColumnInfo:
    return ColumnInfo(name, kind, distinct, 0 if distinct is not None else None, examples)


# ------------------------------------------------------------------------ the plan


def a_plan() -> DataPlan:
    return DataPlan(
        origin="csv",
        source="orders",
        kind="duckdb_csv",
        options={"dialect": "redshift"},
        queries=(
            PlannedQuery(
                name="orders_by_state",
                description='Orders in one state, such as "new" or über.',
                parameters=(
                    PlannedParameter("state", "string", 'ne"w'),
                    PlannedParameter("priority", "integer", 2),
                    PlannedParameter("ratio", "number", 0.5),
                    PlannedParameter("open", "boolean", True),
                ),
                columns=("order_id", "state", "email"),
                masked=("email",),
                classification="confidential",
                max_rows=25,
                definition="-- description: x\nSELECT 'it''s' AS \"a\\b\"\n",
                returns_rows=False,
            ),
            PlannedQuery(
                "orders_all", "Every order.", (), ("order_id",), (), "internal", 100, "x\n"
            ),
        ),
    )


def test_a_plan_is_recorded_with_the_servers_answers_and_read_back_the_same() -> None:
    answers = WorkspaceAnswers(
        name="demo",
        owner="team",
        library=LibrarySource(kind="index", version=">=0.1"),
        mcp_servers=(
            McpAnswers("hello-mcp", "hello", "Sample."),
            McpAnswers("orders-mcp", "orders", "Orders.", 8101, a_plan()),
            McpAnswers("zebra-mcp", "zebra", "Sample again.", 8102),
        ),
    )
    read = WorkspaceAnswers.from_toml(answers.to_toml())
    assert read == answers
    hello, orders, _ = read.mcp_servers
    assert hello.data is None
    assert hello.plan == sample_plan()
    assert orders.plan.masked == ("email",)
    assert orders.plan.suffix == ".sql"


def test_a_planned_query_writes_its_own_python() -> None:
    query = a_plan().queries[0]
    assert query.signature == "state: str, priority: int, ratio: float, open: bool"
    assert query.arguments == '{"state": state, "priority": priority, "ratio": ratio, "open": open}'
    assert eval(query.example_arguments) == {  # noqa: S307 - text this test just built
        "state": 'ne"w',
        "priority": 2,
        "ratio": 0.5,
        "open": True,
    }
    assert a_plan().queries[1].signature == ""
    assert PlannedParameter("day", "date", "2026-01-04").annotation == "str"


@pytest.mark.parametrize(
    "change",
    [
        {"origin": "guess"},
        {"kind": "oracle"},
        {"queries": ()},
        {"queries": (a_plan().queries[1], a_plan().queries[1])},
    ],
)
def test_a_plan_that_makes_no_sense_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"origin|kind|query|queries"):
        replace(a_plan(), **change)  # type: ignore[arg-type]


def test_a_query_with_an_unknown_type_or_level_is_refused() -> None:
    with pytest.raises(ValueError, match="money"):
        PlannedParameter("amount", "money", 1)
    with pytest.raises(KeyError):
        replace(a_plan().queries[1], classification="secret-ish")


def test_a_workspace_file_with_a_broken_plan_is_not_a_workspace_file() -> None:
    answers = WorkspaceAnswers(
        name="demo",
        owner="team",
        library=LibrarySource(kind="index", version=">=0.1"),
        mcp_servers=(McpAnswers("orders-mcp", "orders", "Orders.", 8101, a_plan()),),
    )
    text = answers.to_toml().replace('kind = "duckdb_csv"', 'kind = "oracle"')
    with pytest.raises(CliError, match="not a workspace file"):
        WorkspaceAnswers.from_toml(text)


def test_the_sample_plan_is_the_query_file_the_sample_ships() -> None:
    files = TemplateRenderer().render("mcp-sample", {})
    (query,) = sample_plan().queries
    assert files[Path("queries/people_by_team.sql")] == query.definition  # type: ignore[index]
    assert f"-- description: {query.description}\n" in query.definition


# ------------------------------------------------------------------- the proposals


def test_names_that_need_quoting_and_names_that_look_personal_are_recognised() -> None:
    assert is_plain_name("claim_id")
    assert not any(is_plain_name(name) for name in ("order", "Claim", "2nd", "adjuster name", ""))
    for name in ("email", "work_email", "e_mail", "phone", "home_address", "date_of_birth", "ssn"):
        assert is_sensitive(name), name
    for name in ("name", "status", "zipper", "panel", "tokens_used_total", "emailed"):
        assert not is_sensitive(name), name


def test_a_table_gets_a_lookup_by_its_key_and_a_filter_per_grouping_column() -> None:
    claims = TableInfo(
        "claims",
        (
            column("claim_id", ParameterType.INTEGER, 5, 9001),
            column("customer_id", ParameterType.INTEGER, 4, 501),
            column("status", ParameterType.STRING, 3, "open", "closed", "review"),
            column("amount", ParameterType.NUMBER, 5),
            column("email", ParameterType.STRING, 4, "ana@example.test"),
            column("opened", ParameterType.DATE, 5, "2026-01-04"),
        ),
        rows=5,
    )
    by_id, by_status, by_customer = propose_queries([claims])

    assert (by_id.name, by_id.max_rows) == ("claims_by_claim_id", 10)
    assert by_id.parameters == (PlannedParameter("claim_id", "integer", 9001),)
    assert by_id.columns == ("claim_id", "customer_id", "status", "amount", "email", "opened")
    # The column that looks personal is masked, and makes the result confidential.
    assert (by_id.masked, by_id.classification) == (("email",), "confidential")
    assert by_id.definition == (
        "-- description: The row of claims with one claim_id.\n"
        "-- param claim_id: integer\n"
        "-- max_rows: 10\n"
        "-- classification: confidential\n"
        "SELECT claim_id, customer_id, status, amount, email, opened\n"
        "FROM claims\n"
        "WHERE claim_id = :claim_id\n"
        "ORDER BY claim_id\n"
    )
    # Fewest groups first; a personal column and a date are never a filter.
    assert [q.name for q in (by_status, by_customer)] == [
        "claims_by_status",
        "claims_by_customer_id",
    ]
    assert by_status.description == (
        "The rows of claims with one status, such as open, closed or review."
    )
    assert by_status.parameters[0].example == "open"
    assert all(query.returns_rows for query in (by_id, by_status, by_customer))
    assert [q.name for q in propose_queries([claims], per_table=1)] == ["claims_by_claim_id"]


def test_a_table_with_nothing_to_look_up_by_gets_one_query_for_all_of_it() -> None:
    readings = TableInfo(
        "readings",
        (column("taken", ParameterType.TIMESTAMP, 4), column("value", ParameterType.NUMBER, 4)),
        rows=4,
    )
    (everything,) = propose_queries([readings])
    assert (everything.name, everything.parameters) == ("readings_all", ())
    assert (everything.masked, everything.classification) == ((), "internal")
    assert "WHERE" not in everything.definition
    assert everything.definition.endswith("ORDER BY taken\n")


def test_tables_and_columns_a_query_cannot_name_are_left_out() -> None:
    tables = [
        TableInfo("order", (column("id", ParameterType.INTEGER, 2, 1),), rows=2),
        TableInfo("empty", (column("Odd Name"),), rows=2),
        TableInfo(
            "parts",
            (
                column("part_no", ParameterType.STRING, 2, "a-1"),
                column("group", ParameterType.STRING, 1, "x"),
            ),
            rows=2,
        ),
        TableInfo("x" * 45, (column("id", ParameterType.INTEGER, 2, 1),), rows=2),
    ]
    (parts,) = propose_queries(tables)
    assert parts.name == "parts_by_part_no"
    assert parts.columns == ("part_no",)


def test_without_counts_only_a_telling_name_makes_a_key_and_examples_are_stand_ins() -> None:
    accounts = TableInfo(
        "accounts",
        (column("account_id", ParameterType.INTEGER), column("region"), column("email")),
    )
    ledger = TableInfo("ledger", (column("entry"), column("amount", ParameterType.NUMBER)))
    by_id, everything = propose_queries([accounts, ledger])
    assert by_id.name == "accounts_by_account_id"
    assert by_id.parameters == (PlannedParameter("account_id", "integer", 1),)
    assert not by_id.returns_rows
    assert everything.name == "ledger_all"


def test_a_first_column_is_the_key_only_when_it_was_seen_to_be_unique() -> None:
    unique = TableInfo("tags", (column("label", ParameterType.STRING, 3, "a"), column("n")), rows=3)
    repeated = TableInfo("tags", (column("label", ParameterType.STRING, 2, "a"), column("n")), 3)
    assert propose_queries([unique])[0].max_rows == 10
    assert propose_queries([repeated])[0].max_rows == 100


def test_a_description_stays_one_short_line() -> None:
    long = "a-value-of-nearly-thirty-chars"
    table = TableInfo(
        "shipments_received",
        (
            column("shipment_id", ParameterType.INTEGER, 9, 1),
            column("destination_warehouse", ParameterType.STRING, 3, long, long + "2", long + "3"),
            column("carrier", ParameterType.STRING, 2, "a\nb", "ok"),
        ),
        rows=9,
    )
    _, carrier, warehouse = propose_queries([table])
    assert warehouse.description == "The rows of shipments_received with one destination_warehouse."
    # One usable example is not a list of examples.
    assert carrier.description == "The rows of shipments_received with one carrier."


# ------------------------------------------------------------------ the CSV reader


def test_the_reader_finds_tables_columns_types_counts_and_common_values(exports: Path) -> None:
    found = read_csv_folder(exports)
    claims, customers = found.tables
    assert (claims.name, claims.rows) == ("claims_2026", 5)
    assert (customers.name, customers.rows) == ("customers", 4)
    assert {name: path.name for name, path in found.files.items()} == {
        "claims_2026": "Claims 2026.csv",
        "customers": "customers.CSV",
    }
    by_name = {c.name: c for c in claims.columns}
    assert list(by_name) == ["claim_id", "customer_id", "status", "amount", "opened", "email"]
    assert [c.type for c in claims.columns] == [
        ParameterType.INTEGER,
        ParameterType.INTEGER,
        ParameterType.STRING,
        ParameterType.NUMBER,
        ParameterType.DATE,
        ParameterType.STRING,
    ]
    assert (by_name["status"].distinct, by_name["status"].nulls) == (3, 0)
    # Most common first, then in order.
    assert by_name["status"].examples == ("closed", "open", "review")
    assert by_name["customer_id"].examples[0] == 501
    assert by_name["opened"].examples[0] == "2026-01-04"
    assert found.notes == (
        "Claims 2026.csv: left out 2 column(s) whose names a query cannot use as they are: "
        "order, Adjuster Name",
    )


def test_a_file_name_becomes_a_plain_table_name() -> None:
    assert table_name_for(Path("Claims 2026.csv")) == "claims_2026"
    assert table_name_for(Path("2026-q1.csv")) == "t_2026_q1"
    assert table_name_for(Path("--.csv")) == "t_"


def test_a_folder_that_cannot_be_read_says_why(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="is not a folder"):
        read_csv_folder(tmp_path / "missing")
    with pytest.raises(CliError, match=r"holds no \.csv file"):
        read_csv_folder(tmp_path)
    (tmp_path / "a b.csv").write_text("x\n1\n", encoding="utf-8")
    (tmp_path / "a-b.csv").write_text("x\n1\n", encoding="utf-8")
    with pytest.raises(CliError, match="would both be the table 'a_b'"):
        read_csv_folder(tmp_path)


async def test_a_proposed_query_that_does_not_run_is_dropped_and_the_rest_kept(
    exports: Path, tmp_path: Path
) -> None:
    proposal = await from_csv(exports, source="claims")
    good = proposal.plan.queries[0]
    broken = replace(
        good, name="no_such_column", definition=good.definition.replace("email", "fax")
    )
    data = tmp_path / "staged"
    data.mkdir()
    for name, path in proposal.data_files.items():
        (data / name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    kept, notes = await check_queries(csv_source(data, "claims"), ".sql", [good, broken])
    assert kept == [good]
    assert notes == ["dropped no_such_column: ConfigurationError"]


async def test_from_csv_proposes_a_plan_that_ran_over_the_files(exports: Path) -> None:
    proposal = await from_csv(exports, source="claims")
    plan = proposal.plan
    assert (plan.origin, plan.source, plan.kind) == ("csv", "claims", "duckdb_csv")
    assert [query.name for query in plan.queries] == [
        "claims_2026_by_claim_id",
        "claims_2026_by_status",
        "claims_2026_by_customer_id",
        "customers_by_customer_id",
        "customers_by_segment",
    ]
    assert plan.masked == ("email", "phone")
    assert all(query.returns_rows for query in plan.queries)
    assert sorted(proposal.data_files) == ["claims_2026.csv", "customers.csv"]
    assert proposal.summary.endswith("claims_2026 (5 rows), customers (4 rows)")
    assert proposal.description == "Read-only tools over the claims data."
    assert len(proposal.notes) == 1

    one = await from_csv(exports, source="claims", only=["customers_by_segment"], per_table=5)
    assert [query.name for query in one.plan.queries] == ["customers_by_segment"]
    with pytest.raises(CliError, match="not among the proposed queries: nothing_like_it"):
        await from_csv(exports, source="claims", only=["nothing_like_it"])


async def test_files_nothing_can_be_proposed_from_are_an_error(tmp_path: Path) -> None:
    (tmp_path / "odd.csv").write_text("Odd Name,order\n1,2\n", encoding="utf-8")
    with pytest.raises(CliError, match="nothing could be proposed"):
        await from_csv(tmp_path, source="odd")
    assert selected([], []) == []


# -------------------------------------------------------------------- the command


def test_propose_shows_the_tools_and_writes_nothing(
    workspace: Path, exports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(path for path in workspace.rglob("*"))
    capsys.readouterr()
    assert (
        run(
            "new", "mcp", "claims-mcp", "--from-csv", exports, "--propose", "--workspace", workspace
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "claims.claims_2026_by_status(status: str)   masks email for analysts" in captured.out
    assert "such as closed, open or review." in captured.out
    assert "note: Claims 2026.csv: left out 2 column(s)" in captured.err
    assert sorted(path for path in workspace.rglob("*")) == before


def test_new_mcp_from_csv_writes_a_server_over_the_developers_data(
    workspace: Path, exports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = ("new", "mcp", "claims-mcp", "--from-csv", exports, "--workspace", workspace)
    assert run(*command, "--per-table", "2") == 0
    service = workspace / "mcp-servers/claims-mcp"

    # The data was copied under the table names, and each query has its file.
    assert (service / "data/claims_2026.csv").read_text(encoding="utf-8") == CLAIMS
    assert (service / "data/customers.csv").read_text(encoding="utf-8") == CUSTOMERS
    assert sorted(path.name for path in (service / "queries").iterdir()) == [
        "claims_2026_by_claim_id.sql",
        "claims_2026_by_status.sql",
        "customers_by_customer_id.sql",
        "customers_by_segment.sql",
    ]
    assert not (service / "data/people.csv").exists()

    # One tool per query, registered with the query's own classification; no sample tool.
    server = (service / "src/claims_mcp/server.py").read_text(encoding="utf-8")
    assert 'DATA_SOURCE = "claims"' in server
    assert "async def claims_2026_by_status(status: str) -> dict[str, Any]:" in server
    assert 'await source.query("customers_by_segment", {"segment": segment})' in server
    assert "greet" not in server
    (entry,) = read_servers(workspace)
    assert [tool.name for tool in entry.tools] == [
        "claims.claims_2026_by_claim_id",
        "claims.claims_2026_by_status",
        "claims.customers_by_customer_id",
        "claims.customers_by_segment",
    ]
    assert {tool.classification for tool in entry.tools} == {Classification.CONFIDENTIAL}

    # The rules mask every column that looked personal, for the analyst only.
    rules = {rule["id"]: rule for rule in yaml.safe_load((workspace / RULES).read_text())["rules"]}
    analyst = rules["claims-analysts-see-masked-columns"]
    assert analyst["resources"] == ["claims.*"]
    assert analyst["obligations"] == {"mask_columns": ["email", "phone"], "max_rows": 50}
    assert "obligations" not in rules["claims-managers-see-everything"]
    samples = (workspace / SAMPLES).read_text(encoding="utf-8")
    assert "an analyst sees claims with columns masked" in samples
    assert "claims/claims.claims_2026_by_claim_id" in samples

    # The settings name the source, and the tests call each tool with a value from the data.
    assert '"claims": {"kind": "duckdb_csv"' in (service / ".env.example").read_text()
    tests = (service / "tests/test_claims_mcp.py").read_text(encoding="utf-8")
    assert "async def test_customers_by_segment_masks_columns_for_an_analyst(" in tests
    assert "{'segment': 'business'}" in tests
    assert "test_greet_says_hello" not in tests
    readme = (service / "README.md").read_text(encoding="utf-8")
    assert "| `analyst` | allowed | `email`, `phone` are masked, at most 50 rows |" in readme
    assert "proposed from the CSV files" in readme

    # The plan is part of the answers, so a link or an update needs no data.
    assert load_answers(workspace).mcp_servers[0].plan.origin == "csv"
    capsys.readouterr()
    assert run(*command, "--per-table", "2") == 0
    assert "nothing to do" in capsys.readouterr().out
    assert run("update", "--workspace", workspace) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_an_agent_linked_to_such_a_server_is_tested_against_its_first_tool(
    workspace: Path, exports: Path
) -> None:
    assert run("new", "mcp", "claims-mcp", "--from-csv", exports, "--workspace", workspace) == 0
    assert run("new", "agent", "claims-agent", "--mcp", "claims", "--workspace", workspace) == 0
    test = (workspace / "tests/test_claims_agent_with_claims_mcp.py").read_text(encoding="utf-8")
    assert '"name": "claims_claims_2026_by_claim_id"' in test
    assert "'claim_id': 9001" in test
    assert "assert MASK in shown" in test
    assert "Cleo Carr" not in test
    samples = (workspace / SAMPLES).read_text(encoding="utf-8")
    assert "claims-agent calls a tool of claims" in samples


def test_data_the_developer_changed_is_not_overwritten(
    workspace: Path, exports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = ("new", "mcp", "claims-mcp", "--from-csv", exports, "--workspace", workspace)
    assert run(*command) == 0
    data = workspace / "mcp-servers/claims-mcp/data/customers.csv"
    data.write_text(CUSTOMERS + "505,Ed Eze,retail,555-0105,2024-01-01\n", encoding="utf-8")
    server = workspace / "mcp-servers/claims-mcp/src/claims_mcp/server.py"
    before = server.read_text(encoding="utf-8")
    capsys.readouterr()

    assert run(*command) == 1
    assert "data and differ from what would be copied: customers.csv" in capsys.readouterr().err
    assert server.read_text(encoding="utf-8") == before
    assert run(*command, "--force") == 0
    assert data.read_text(encoding="utf-8") == CUSTOMERS


def test_the_data_options_need_a_data_source_to_read(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert run("new", "mcp", "hello-mcp", "--propose", "--workspace", workspace) == 1
    assert "--propose needs a data option such as --from-csv" in capsys.readouterr().err
    assert run("new", "mcp", "hello-mcp", "--query", "x", "--workspace", workspace) == 1
    assert "which was not given: --query" in capsys.readouterr().err
    assert not (workspace / "mcp-servers/hello-mcp").exists()
    assert (
        run("new", "mcp", "x-mcp", "--from-csv", workspace / "none", "--workspace", workspace) == 2
    )


def test_a_server_without_personal_columns_has_a_rule_that_masks_nothing(
    workspace: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "parts.csv").write_text("part_no,bin\nA1,north\nA2,north\nA3,south\n", "utf-8")
    assert run("new", "mcp", "parts-mcp", "--from-csv", folder, "--workspace", workspace) == 0
    rules = {rule["id"]: rule for rule in yaml.safe_load((workspace / RULES).read_text())["rules"]}
    assert rules["parts-analysts-read-the-data"]["obligations"] == {"max_rows": 50}
    tests = (workspace / "mcp-servers/parts-mcp/tests/test_parts_mcp.py").read_text("utf-8")
    assert "shows_an_analyst_every_column_of_fewer_rows" in tests
    assert "import MASK" not in tests
    assert "an analyst reads parts" in (workspace / SAMPLES).read_text(encoding="utf-8")


def test_one_masked_column_names_the_rule(workspace: Path, tmp_path: Path) -> None:
    folder = tmp_path / "one"
    folder.mkdir()
    (folder / "staff.csv").write_text("staff_id,work_email\n1,a@x.test\n2,b@x.test\n", "utf-8")
    assert run("new", "mcp", "staff-mcp", "--from-csv", folder, "--workspace", workspace) == 0
    ids = [rule["id"] for rule in yaml.safe_load((workspace / RULES).read_text())["rules"]]
    assert "staff-analysts-see-no-work-email" in ids

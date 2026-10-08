"""Servers over a REST API: reading an OpenAPI document and what is generated from it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.read_openapi import OpenApiReading, read_openapi, snake
from ai_agent_lib_cli.readers import from_openapi
from ai_agent_lib_cli.shared import read_servers
from ai_agent_lib_cli.testing import toolbox_for_tests
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import load_answers
from ai_agent_lib_core.adapters.strict_yaml import load_strict_yaml

RATES = Path(__file__).parent / "data" / "rates.yaml"
RULES = Path("policies/agentlib/rules/data.yaml")

RECORD = {"type": "object", "properties": {"id": {"type": "integer"}, "label": {"type": "string"}}}


# No formatter and no server processes: not what these tests are about.
TOOLS = toolbox_for_tests(pins={})


def run(*arguments: str | Path, tools: Toolbox = TOOLS) -> int:
    return main([str(argument) for argument in arguments], toolbox=tools)


def answers_with(schema: object, media: str = "application/json") -> dict[str, Any]:
    return {"200": {"description": "ok", "content": {media: {"schema": schema}}}}


def document(tmp_path: Path, paths: dict[str, Any], **top: object) -> Path:
    raw = {"openapi": "3.1.0", "info": {"title": "Things"}, "paths": paths, **top}
    path = tmp_path / "api.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def reading_of(tmp_path: Path, paths: dict[str, Any], **top: object) -> OpenApiReading:
    return read_openapi(document(tmp_path, paths, **top))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert run("init", "demo", "--dir", tmp_path, "--owner", "demo-team") == 0
    return tmp_path / "demo"


# ----------------------------------------------------------------------- the reader


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("customerId", "customer_id"),
        ("HTTPStatus", "http_status"),
        ("already_plain", "already_plain"),
        ("X-Desk Name", "x_desk_name"),
        ("listAccountsV2", "list_accounts_v2"),
        ("--", ""),
    ],
)
def test_names_are_made_plain(name: str, expected: str) -> None:
    assert snake(name) == expected


def test_a_document_is_read_into_one_query_for_each_get_that_answers_with_records() -> None:
    reading = read_openapi(RATES)
    assert (reading.title, reading.base_url) == ("Rates", "https://rates.example.test/api/v2")
    assert reading.wants_credentials
    listing, lookup, traders = reading.queries

    # An operationId names the query; the summary describes it.
    assert (listing.name, listing.description) == (
        "list_currencies",
        "Every currency the desk quotes.",
    )
    # An optional parameter is kept only if the document gives it a default.
    assert [(p.name, p.type, p.example) for p in listing.parameters] == [
        ("page_size", "integer", 50)
    ]
    assert listing.columns == ("code", "display_name", "rate", "as_of")
    assert (listing.masked, listing.classification, listing.max_rows) == ((), "internal", 100)
    assert load_strict_yaml(listing.definition) == {
        "description": "Every currency the desk quotes.",
        "method": "GET",
        "path": "/currencies",
        "parameters": {
            "page_size": {"type": "integer", "in": "query", "default": 50, "name": "pageSize"}
        },
        "max_rows": 100,
        "classification": "internal",
        "rows": "data.items",
        "columns": {
            "code": "code",
            "display_name": "displayName",
            "rate": "rate",
            "as_of": "asOf",
        },
    }

    # Without an operationId the name comes from the path. One record, looked up by a
    # path value: at most one row, and nothing found is an empty table.
    assert lookup.name == "currencies_by_currency_code"
    assert lookup.parameters[0].example == "EUR"
    spec = load_strict_yaml(lookup.definition)
    assert isinstance(spec, dict)
    assert (spec["path"], spec["max_rows"], spec["empty_on_not_found"]) == (
        "/currencies/{currency_code}",
        1,
        True,
    )
    assert "rows" not in spec

    # A list at the top, records merged from allOf, one level of nesting, a personal column.
    assert traders.description == "The traders on one desk."
    assert [(p.name, p.type, p.example) for p in traders.parameters] == [
        ("desk_id", "integer", 1),
        ("active_only", "boolean", True),
    ]
    assert traders.columns == ("id", "full_name", "work_email", "joined", "desk_id", "desk_name")
    assert (traders.masked, traders.classification) == (("work_email",), "confidential")

    assert reading.notes == (
        "left out GET /reports/daily: it does not answer with JSON",
        "left out GET /quotes: it needs the header parameter 'X-Desk', "
        "which a definition cannot send",
        "left out 1 optional parameter(s) that have no default; "
        "add the ones you need to the definitions in queries/",
    )


def test_each_query_comes_with_the_call_it_makes_and_an_example_answer() -> None:
    calls = read_openapi(RATES).calls
    key, body = calls["list_currencies"]
    assert key == "GET /api/v2/currencies"
    first, second = body["data"]["items"]
    # The document's example where it gives one, a value of the right type where not.
    assert first == {
        "code": "EUR",
        "displayName": "display_name-1",
        "rate": 1.5,
        "asOf": "2026-01-01T00:00:00Z",
    }
    assert second["displayName"] == "display_name-2"
    assert calls["currencies_by_currency_code"][0] == "GET /api/v2/currencies/EUR"
    key, people = calls["traders_of_desk"]
    assert key == "GET /api/v2/desks/1/traders"
    assert people[0]["workEmail"] == "person1@example.test"
    assert people[1]["desk"] == {"id": 2, "name": "name-2"}
    assert people[0]["joined"] == "2026-01-01"

    # Another address moves the calls with it.
    elsewhere = read_openapi(RATES, base_url="https://internal.example.test")
    assert elsewhere.base_url == "https://internal.example.test"
    assert elsewhere.calls["list_currencies"][0] == "GET /currencies"


@pytest.mark.parametrize(
    ("schema", "rows"),
    [
        ({"type": "array", "items": RECORD}, None),
        (
            {"type": "object", "properties": {"results": {"type": "array", "items": RECORD}}},
            "results",
        ),
        (
            {
                "properties": {
                    "meta": {"type": "object", "properties": {"total": {"type": "integer"}}},
                    "page": {
                        "properties": {"things": {"type": ["array", "null"], "items": RECORD}}
                    },
                }
            },
            "page.things",
        ),
        (RECORD, None),
    ],
)
def test_the_records_are_found_wherever_the_response_keeps_them(
    tmp_path: Path, schema: dict[str, Any], rows: str | None
) -> None:
    (query,) = reading_of(
        tmp_path, {"/things": {"get": {"responses": answers_with(schema)}}}
    ).queries
    spec = load_strict_yaml(query.definition)
    assert isinstance(spec, dict)
    assert spec.get("rows") == rows
    assert query.name == "things"
    assert query.description == "GET /things"
    assert query.columns == ("id", "label")


@pytest.mark.parametrize(
    ("operation", "reason"),
    [
        (
            {"responses": answers_with({"type": "array", "items": {"type": "string"}})},
            "plain values",
        ),
        ({"responses": answers_with({"type": "string"})}, "does not answer with records"),
        ({"responses": answers_with({"oneOf": [RECORD, RECORD]})}, "no schema this reader"),
        ({"responses": answers_with({"$ref": "other.yaml#/Thing"})}, "no schema this reader"),
        (
            {"responses": answers_with({"$ref": "#/components/schemas/Loop"})},
            "no schema this reader",
        ),
        ({"responses": answers_with(RECORD, "text/csv")}, "does not answer with JSON"),
        ({"responses": {"204": {"description": "nothing"}}}, "does not answer with JSON"),
        (
            {
                "responses": answers_with(
                    {
                        "properties": {
                            "a": {"type": "array", "items": RECORD},
                            "b": {"type": "array", "items": RECORD},
                        }
                    }
                )
            },
            "more than one list",
        ),
        (
            {"responses": answers_with({"type": "object", "properties": {"x": {"type": "array"}}})},
            "no plain fields",
        ),
        (
            {
                "parameters": [
                    {"name": "ids", "in": "query", "required": True, "schema": {"type": "array"}}
                ],
                "responses": answers_with(RECORD),
            },
            "'ids', which is not a plain value",
        ),
        (
            {
                "parameters": [
                    {"name": "a-b", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "a_b", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "responses": answers_with(RECORD),
            },
            "same name",
        ),
        (
            {
                "parameters": [
                    {"name": "thing", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": answers_with(RECORD),
            },
            "placeholders and its path parameters do not match",
        ),
        ({"operationId": "x" * 60, "responses": answers_with(RECORD)}, "no usable name"),
    ],
)
def test_an_operation_that_cannot_be_offered_is_left_out_with_the_reason(
    tmp_path: Path, operation: dict[str, Any], reason: str
) -> None:
    reading = reading_of(
        tmp_path,
        {"/things": {"get": operation}},
        components={"schemas": {"Loop": {"$ref": "#/components/schemas/Loop"}}},
    )
    assert reading.queries == ()
    (note,) = reading.notes
    assert note.startswith("left out GET /things: ")
    assert reason in note


def test_parameters_take_their_types_examples_and_defaults_from_the_document(
    tmp_path: Path,
) -> None:
    parameters = [
        {"$ref": "#/components/parameters/Region"},
        {
            "name": "since",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "format": "date"},
        },
        {
            "name": "at",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "format": "date-time"},
        },
        {
            "name": "minimum",
            "in": "query",
            "required": True,
            "schema": {"type": "number", "example": "many"},
        },
        {
            "name": "kind",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "enum": ["a", "b"]},
        },
        {"name": "limit", "in": "query", "schema": {"type": ["integer", "null"], "default": 20}},
        {"name": "trace", "in": "header", "schema": {"type": "string"}},
        {"name": "flag", "in": "query", "schema": {"type": "boolean", "default": "yes"}},
    ]
    reading = reading_of(
        tmp_path,
        {
            "/api/v1/regions/{regionCode}/things": {
                "parameters": [
                    {
                        "name": "regionCode",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "get": {"parameters": parameters, "responses": {200: answers_with(RECORD)["200"]}},
            }
        },
        components={
            "parameters": {
                "Region": {
                    "name": "regionCode",
                    "in": "path",
                    "required": True,
                    "example": "emea",
                    "schema": {"type": "string"},
                }
            }
        },
    )
    (query,) = reading.queries
    # The version and the word "api" are not part of a name; the operation's own
    # declaration of a parameter wins over the path's.
    assert query.name == "regions_things_by_region_code"
    assert [(p.name, p.type, p.example) for p in query.parameters] == [
        ("region_code", "string", "emea"),
        ("since", "date", "2026-01-01"),
        ("at", "timestamp", "2026-01-01T00:00:00Z"),
        ("minimum", "number", 1.5),
        ("kind", "string", "a"),
        ("limit", "integer", 20),
    ]
    spec = load_strict_yaml(query.definition)
    assert isinstance(spec, dict)
    assert spec["parameters"]["limit"] == {"type": "integer", "in": "query", "default": 20}
    assert spec["path"] == "/api/v1/regions/{region_code}/things"
    assert reading.calls[query.name][0] == "GET /api/v1/regions/emea/things"
    # An optional header and an optional value with an unusable default are dropped.
    assert reading.notes[-1].startswith("left out 2 optional parameter(s)")
    assert not reading.wants_credentials


def test_two_operations_with_one_name_and_long_descriptions_are_handled(tmp_path: Path) -> None:
    long = "Lists every thing there is, " + "in great detail, " * 6 + "and then stops."
    reading = reading_of(
        tmp_path,
        {
            "/a": {
                "get": {"operationId": "things", "summary": long, "responses": answers_with(RECORD)}
            },
            "/b": {"get": {"operationId": "Things", "responses": answers_with(RECORD)}},
            "/c": {"post": {"responses": answers_with(RECORD)}},
        },
    )
    (query,) = reading.queries
    assert len(query.description) <= 84
    assert query.description.endswith("...")
    assert reading.notes == ("left out GET /b: another operation is already called things",)


@pytest.mark.parametrize(
    ("servers", "expected"),
    [
        ([{"url": "https://api.example.test/v1/"}], "https://api.example.test/v1"),
        ([{"url": "https://{tenant}.example.test"}], None),
        ([{"url": "/v1"}], None),
        ([], None),
        ("nonsense", None),
    ],
)
def test_the_address_is_the_first_server_if_it_is_a_whole_one(
    tmp_path: Path, servers: object, expected: str | None
) -> None:
    paths = {"/things": {"get": {"responses": answers_with(RECORD)}}}
    assert reading_of(tmp_path, paths, servers=servers).base_url == expected


def test_a_file_that_is_not_an_openapi_3_document_is_refused(tmp_path: Path) -> None:
    for name, text, reason in (
        ("a.json", "{not json", "cannot be read as JSON or YAML"),
        ("b.yaml", "a: [", "cannot be read as JSON or YAML"),
        ("c.yaml", "- 1\n", "it has no paths"),
        ("d.yaml", "openapi: 3.0.0\n", "it has no paths"),
        ("e.yaml", 'swagger: "2.0"\npaths: {}\n', "not an OpenAPI 3 document"),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8")
        with pytest.raises(CliError, match=reason):
            read_openapi(tmp_path / name)
    with pytest.raises(CliError, match="cannot be read"):
        read_openapi(tmp_path / "missing.yaml")


# -------------------------------------------------------------------- the proposal


async def test_from_openapi_proposes_definitions_that_the_real_data_source_ran() -> None:
    proposal = await from_openapi(RATES, source="rates")
    plan = proposal.plan
    assert (plan.origin, plan.kind, plan.suffix) == ("openapi", "rest", ".yaml")
    assert plan.options == {"base_url": "https://rates.example.test/api/v2"}
    assert [q.name for q in plan.queries] == [
        "list_currencies",
        "currencies_by_currency_code",
        "traders_of_desk",
    ]
    assert all(query.returns_rows for query in plan.queries)
    assert plan.masked == ("work_email",)
    (path,) = proposal.extra_files
    assert path.as_posix() == "tests/api_responses.json"
    assert sorted(json.loads(proposal.extra_files[path])) == [
        "GET /api/v2/currencies",
        "GET /api/v2/currencies/EUR",
        "GET /api/v2/desks/1/traders",
    ]
    assert proposal.summary == (
        "Read rates.yaml (Rates): 3 operations can be offered, at https://rates.example.test/api/v2"
    )
    assert proposal.description == "Read-only tools over the Rates API."
    assert proposal.notes[-1].startswith("the document says callers must authenticate")
    assert not proposal.data_files

    one = await from_openapi(RATES, source="rates", only=["traders_of_desk"])
    assert [q.name for q in one.plan.queries] == ["traders_of_desk"]
    assert list(json.loads(next(iter(one.extra_files.values())))) == ["GET /api/v2/desks/1/traders"]


async def test_an_address_that_cannot_be_used_is_refused(tmp_path: Path) -> None:
    paths = {"/things": {"get": {"responses": answers_with(RECORD)}}}
    with pytest.raises(CliError, match="names no server address"):
        await from_openapi(document(tmp_path, paths), source="things")
    with pytest.raises(CliError, match="must use https"):
        await from_openapi(
            document(tmp_path, paths), source="things", base_url="http://api.example.test"
        )
    local = await from_openapi(
        document(tmp_path, paths), source="things", base_url="http://localhost:9000/"
    )
    assert local.plan.options == {"base_url": "http://localhost:9000"}
    with pytest.raises(CliError, match=r"no operation of .* can be offered"):
        await from_openapi(document(tmp_path, {}), source="things", base_url="https://x.test")


async def test_a_large_api_is_cut_to_the_first_operations_unless_some_are_named(
    tmp_path: Path,
) -> None:
    paths = {f"/things{n}": {"get": {"responses": answers_with(RECORD)}} for n in range(15)}
    path = document(tmp_path, paths, servers=[{"url": "https://api.example.test"}])
    cut = await from_openapi(path, source="things")
    assert len(cut.plan.queries) == 12
    assert cut.notes[-1] == (
        "kept the first 12 of 15 operations; name the ones you want with --query. "
        "Not kept: things12, things13, things14"
    )
    named = await from_openapi(path, source="things", only=["things14", "things0"])
    assert [q.name for q in named.plan.queries] == ["things14", "things0"]
    assert named.notes == ()


# --------------------------------------------------------------------- the command


def test_new_mcp_from_openapi_writes_a_server_whose_tests_answer_for_the_api(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = ("new", "mcp", "rates-mcp", "--from-openapi", RATES, "--workspace", workspace)
    capsys.readouterr()
    assert run(*command) == 0
    captured = capsys.readouterr()
    assert (
        "rates.traders_of_desk(desk_id: int, active_only: bool)   masks work_email" in captured.out
    )
    assert "note: left out GET /quotes" in captured.err
    service = workspace / "mcp-servers/rates-mcp"

    assert sorted(path.name for path in (service / "queries").iterdir()) == [
        "currencies_by_currency_code.yaml",
        "list_currencies.yaml",
        "traders_of_desk.yaml",
    ]
    assert not (service / "data").exists()
    settings = (service / ".env.example").read_text(encoding="utf-8")
    assert (
        '{"rates": {"kind": "rest", "queries_dir": "queries", '
        '"base_url": "https://rates.example.test/api/v2"}}'
    ) in settings
    assert "The data the tools read: an HTTP API" in settings
    assert "auth_secret" not in settings

    server = (service / "src/rates_mcp/server.py").read_text(encoding="utf-8")
    assert "async def traders_of_desk(desk_id: int, active_only: bool) -> dict[str, Any]:" in server
    assert "never an address from a caller" in server
    tests = (service / "tests/test_rates_mcp.py").read_text(encoding="utf-8")
    assert "rest_stub_providers(API_RESPONSES)" in tests
    assert 'SERVICE / "tests/api_responses.json"' in tests
    assert "{'desk_id': 1, 'active_only': True}" in tests
    assert json.loads((service / "tests/api_responses.json").read_text(encoding="utf-8"))
    # A REST server needs no CSV engine.
    assert "ai-agent-lib-core[jwt,mcp,serve]" in (service / "pyproject.toml").read_text()

    readme = (service / "README.md").read_text(encoding="utf-8")
    assert "proposed from an OpenAPI document" in readme
    assert '"auth_secret": "rates_token"' in readme
    assert "EAP_SECRET_RATES_TOKEN" in readme
    assert "Adding a tool over your own data" not in readme

    (entry,) = read_servers(workspace)
    assert [tool.name for tool in entry.tools] == [
        "rates.list_currencies",
        "rates.currencies_by_currency_code",
        "rates.traders_of_desk",
    ]
    rules = {rule["id"]: rule for rule in yaml.safe_load((workspace / RULES).read_text())["rules"]}
    assert rules["rates-analysts-see-no-work-email"]["obligations"] == {
        "mask_columns": ["work_email"],
        "max_rows": 50,
    }
    plan = load_answers(workspace).mcp_servers[0].plan
    assert (plan.origin, plan.options["base_url"]) == (
        "openapi",
        "https://rates.example.test/api/v2",
    )

    assert run(*command) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert run("update", "--workspace", workspace) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_an_agent_linked_to_a_rest_server_is_tested_with_the_same_answers(
    workspace: Path,
) -> None:
    assert run("new", "mcp", "rates-mcp", "--from-openapi", RATES, "--workspace", workspace) == 0
    assert run("new", "agent", "fx-agent", "--mcp", "rates", "--workspace", workspace) == 0
    test = (workspace / "tests/test_fx_agent_with_rates_mcp.py").read_text(encoding="utf-8")
    assert 'ROOT / "mcp-servers" / "rates-mcp" / "tests/api_responses.json"' in test
    assert "server_config, rest_stub_providers(API_RESPONSES)" in test
    assert 'CALLS_THE_TOOL = calls_tool("rates.list_currencies"' in test
    # The first tool masks nothing, so there is nothing to say about masks.
    assert "MASK" not in test


def test_the_two_data_options_do_not_mix(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    arguments = ("--from-openapi", RATES, "--from-csv", tmp_path, "--workspace", workspace)
    assert run("new", "mcp", "x-mcp", *arguments) == 1
    assert "give only one of --from-csv, --from-openapi, --from-redshift" in capsys.readouterr().err
    assert run("new", "mcp", "x-mcp", "--base-url", "https://x.test", "--workspace", workspace) == 1
    assert "which was not given: --base-url" in capsys.readouterr().err
    assert (
        run(
            "new",
            "mcp",
            "x-mcp",
            "--from-openapi",
            RATES,
            "--base-url",
            "http://far.example.test",
            "--workspace",
            workspace,
        )
        == 1
    )
    assert "must use https" in capsys.readouterr().err
    assert not (workspace / "mcp-servers/x-mcp").exists()

"""What is specific to the REST data source; the shared rules are in the contract suite."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from ai_agent_lib_core.adapters import RestDataSource, RestOptions
from ai_agent_lib_core.contracts import (
    AgentLibError,
    ConfigurationError,
    CredentialsExpiredError,
    PolicyDenied,
    ProviderSelection,
    ServiceConfig,
    TransientError,
    ValidationFailed,
)
from ai_agent_lib_core.di import DATA_PORT, ServiceContainer
from ai_agent_lib_core.testing import (
    Fakes,
    FakeSecretsProvider,
    FrozenClock,
    accounts_api,
    write_accounts_endpoints,
)

Handler = Callable[[httpx.Request], httpx.Response]

LOOKUP = (
    "description: One account.\n"
    "path: /v1/accounts/{account}\n"
    "parameters:\n"
    "  account: {type: string, in: path}\n"
    "  as_at: {type: date, default: 2026-01-31}\n"
    "  detailed: {type: boolean, default: false}\n"
    "max_rows: 1\n"
    "columns: {account_id: id, tags: tags, owner: owner}\n"
    "empty_on_not_found: true\n"
)
SEARCH = (
    "description: Search accounts.\n"
    "method: POST\n"
    "path: /v1/accounts/search\n"
    "parameters:\n"
    "  region: {type: string, in: body}\n"
    "  min_balance: {type: number, in: body, default: 10}\n"
    "  opened_after: {type: date, in: body, default: 2020-01-01}\n"
    "  page: {type: integer, in: query, default: 1}\n"
    "max_rows: 10\n"
    "columns: {account_id: id}\n"
)


class Recording:
    """Wraps a handler and keeps every request it saw."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def replies(*responses: httpx.Response | Exception) -> Handler:
    """Return a handler that gives each reply in turn, repeating the last one."""
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        reply = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return handler


async def started(
    tmp_path: Path,
    handler: Handler,
    *,
    token: str | None = None,
    extra: dict[str, str] | None = None,
    **options: object,
) -> tuple[RestDataSource, Recording]:
    recording = handler if isinstance(handler, Recording) else Recording(handler)
    queries_dir = write_accounts_endpoints(tmp_path, lookup=LOOKUP, search=SEARCH, **(extra or {}))
    source = RestDataSource(
        "bank",
        RestOptions.model_validate(
            {"base_url": "https://api.example.test/core/", "queries_dir": queries_dir, **options}
        ),
        FrozenClock(),
        token=SecretStr(token) if token is not None else None,
        transport=httpx.MockTransport(recording),
        sleep=recording.sleep,
    )
    await source.start()
    return source, recording


def ok(document: object) -> httpx.Response:
    return httpx.Response(200, json=document)


# ------------------------------------------------------------------- requests


async def test_the_request_is_built_only_from_the_definition_and_typed_values(
    tmp_path: Path,
) -> None:
    source, seen = await started(tmp_path, replies(ok({"id": 1, "tags": [], "owner": None})))
    await source.query("lookup", {"account": "a/b?c=d#e %2e", "detailed": True})
    request = seen.requests[0]
    assert request.method == "GET"
    assert request.url.host == "api.example.test"
    assert request.url.raw_path == (
        b"/core/v1/accounts/a%2Fb%3Fc%3Dd%23e%20%252e?as_at=2026-01-31&detailed=true"
    )
    assert request.headers["accept"] == "application/json"
    assert "authorization" not in request.headers
    await source.aclose()


async def test_a_parameter_is_sent_under_the_name_the_api_uses(tmp_path: Path) -> None:
    renamed = (
        "description: Accounts of one customer.\n"
        "method: POST\n"
        "path: /v1/accounts/find\n"
        "parameters:\n"
        "  customer_id: {type: integer, in: query, name: customerId}\n"
        "  min_balance: {type: number, in: body, name: minBalance}\n"
        "  region: {type: string, in: body}\n"
        "max_rows: 10\n"
        "columns: {account_id: id}\n"
    )
    source, recording = await started(tmp_path, lambda request: ok([]), extra={"find": renamed})
    await source.query("find", {"customer_id": 7, "min_balance": 5, "region": "emea"})
    (request,) = recording.requests
    # A caller uses the plain name; the API gets its own.
    assert dict(request.url.params) == {"customerId": "7"}
    assert json.loads(request.content) == {"minBalance": 5, "region": "emea"}
    assert [p.name for p in source.describe()["find"].parameters] == [
        "customer_id",
        "min_balance",
        "region",
    ]
    await source.aclose()


async def test_a_path_parameter_cannot_be_renamed(tmp_path: Path) -> None:
    renamed = (
        "description: One account.\n"
        "path: /v1/accounts/{account}\n"
        "parameters:\n"
        "  account: {type: string, in: path, name: accountId}\n"
        "max_rows: 1\n"
        "columns: {account_id: id}\n"
    )
    with pytest.raises(ConfigurationError, match="named by its placeholder"):
        await started(tmp_path, lambda request: ok([]), extra={"renamed": renamed})


@pytest.mark.parametrize("value", ["", ".", ".."])
async def test_a_path_value_that_would_change_the_path_is_refused(
    tmp_path: Path, value: str
) -> None:
    source, seen = await started(tmp_path, replies(ok({})))
    with pytest.raises(ValidationFailed, match="cannot be used in a path"):
        await source.query("lookup", {"account": value})
    assert seen.requests == []
    await source.aclose()


async def test_a_post_sends_body_parameters_as_typed_json(tmp_path: Path) -> None:
    source, seen = await started(tmp_path, replies(ok([{"id": 7}])))
    result = await source.query("search", {"region": "east", "min_balance": "12.5", "page": 2})
    request = seen.requests[0]
    assert (request.method, request.url.path, request.url.query) == (
        "POST",
        "/core/v1/accounts/search",
        b"page=2",
    )
    assert json.loads(request.content) == {
        "region": "east",
        "min_balance": 12.5,
        "opened_after": "2020-01-01",
    }
    assert result.rows == ((7,),)
    await source.aclose()


async def test_the_token_is_sent_and_never_shown(tmp_path: Path) -> None:
    source, seen = await started(tmp_path, replies(httpx.Response(500)), token="s3cr3t-token")
    with pytest.raises(TransientError) as caught:
        await source.query("all_accounts")
    assert seen.requests[0].headers["authorization"] == "Bearer s3cr3t-token"
    assert "s3cr3t" not in str(caught.value)
    assert "s3cr3t" not in repr(source)
    await source.aclose()

    keyed, seen = await started(
        tmp_path / "keyed",
        replies(ok({"data": {"items": []}})),
        token="k3y",
        auth_header="X-API-Key",
        auth_scheme="",
    )
    await keyed.query("all_accounts")
    assert seen.requests[0].headers["x-api-key"] == "k3y"
    await keyed.aclose()


# ------------------------------------------------------------------ responses


async def test_only_declared_fields_are_returned_and_nested_values_become_text(
    tmp_path: Path,
) -> None:
    document = {"id": 9, "tags": ["vip", "new"], "owner": {"name": "Ann"}, "ssn": "000-00-0000"}
    source, _ = await started(tmp_path, replies(ok(document)))
    result = await source.query("lookup", {"account": "9"})
    assert result.as_dicts() == [
        {"account_id": 9, "tags": '["vip","new"]', "owner": '{"name":"Ann"}'}
    ]
    assert "000-00-0000" not in repr(result)
    await source.aclose()


async def test_the_api_can_say_how_fresh_the_data_is(tmp_path: Path) -> None:
    source, _ = await started(tmp_path, accounts_api)
    result = await source.query("accounts_by_region", {"region": "west"})
    assert result.source is not None
    assert result.source.as_of == datetime(2026, 3, 1, 6, tzinfo=UTC)
    assert result.source.uri == "datasource://bank/accounts_by_region"
    undated = await source.query("all_accounts")
    assert undated.source is not None
    assert undated.source.as_of is None
    await source.aclose()


async def test_not_found_is_an_empty_result_only_where_the_definition_says(tmp_path: Path) -> None:
    source, _ = await started(tmp_path, replies(httpx.Response(404)))
    assert (await source.query("lookup", {"account": "1"})).rows == ()
    with pytest.raises(ValidationFailed, match="HTTP 404"):
        await source.query("all_accounts")
    await source.aclose()


@pytest.mark.parametrize(
    ("reply", "error", "text"),
    [
        (httpx.Response(401, text="token abc expired"), CredentialsExpiredError, "credentials"),
        (httpx.Response(403), PolicyDenied, "forbade"),
        (httpx.Response(400, text="bad region 'x'"), ValidationFailed, "HTTP 400"),
        (
            httpx.Response(302, headers={"location": "https://evil.test/"}),
            AgentLibError,
            "redirect",
        ),
        (httpx.Response(200, text="<html>"), AgentLibError, "not JSON"),
        (ok({"data": "nothing here"}), AgentLibError, "does not hold records"),
        (ok({"data": {"items": [1, 2]}}), AgentLibError, "does not hold records"),
    ],
    ids=["401", "403", "400", "redirect", "not-json", "wrong-shape", "not-records"],
)
async def test_other_replies_map_to_the_error_taxonomy_without_repeating_the_body(
    tmp_path: Path, reply: httpx.Response, error: type[AgentLibError], text: str
) -> None:
    source, seen = await started(tmp_path, replies(reply))
    with pytest.raises(error, match=text) as caught:
        await source.query("all_accounts")
    assert type(caught.value) is error
    assert not caught.value.retryable
    assert "abc" not in str(caught.value)
    assert "'x'" not in str(caught.value)
    assert len(seen.requests) == 1
    await source.aclose()


async def test_a_forbidden_reply_carries_a_reason_code(tmp_path: Path) -> None:
    source, _ = await started(tmp_path, replies(httpx.Response(403)))
    with pytest.raises(PolicyDenied) as caught:
        await source.query("all_accounts")
    assert caught.value.reason_code == "upstream_forbidden"
    await source.aclose()


async def test_a_response_larger_than_the_limit_is_refused(tmp_path: Path) -> None:
    source, _ = await started(tmp_path, accounts_api, max_response_bytes=64)
    with pytest.raises(AgentLibError, match="larger than 64 bytes"):
        await source.query("all_accounts")
    await source.aclose()


# -------------------------------------------------------------------- retries


async def test_a_get_is_retried_after_a_passing_failure(tmp_path: Path) -> None:
    handler = replies(
        httpx.Response(503), httpx.ConnectError("refused"), ok({"data": {"items": [{"id": 1}]}})
    )
    source, seen = await started(tmp_path, handler)
    result = await source.query("all_accounts")
    assert result.rows[0][0] == 1
    assert len(seen.requests) == 3
    assert seen.sleeps == [0.2, 0.4]
    await source.aclose()


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(429), httpx.Response(502), httpx.ReadTimeout("slow"), httpx.ConnectError("x")],
    ids=["429", "502", "timeout", "unreachable"],
)
async def test_a_failure_that_persists_is_transient_after_the_last_attempt(
    tmp_path: Path, failure: httpx.Response | Exception
) -> None:
    source, seen = await started(tmp_path, replies(failure), max_retries=1)
    with pytest.raises(TransientError) as caught:
        await source.query("all_accounts")
    assert caught.value.retryable
    assert len(seen.requests) == 2
    await source.aclose()


async def test_a_post_is_never_repeated(tmp_path: Path) -> None:
    source, seen = await started(tmp_path, replies(httpx.Response(503)))
    with pytest.raises(TransientError):
        await source.query("search", {"region": "east"})
    assert len(seen.requests) == 1
    assert seen.sleeps == []
    await source.aclose()


async def test_an_unenforceable_obligation_is_refused_before_any_request(tmp_path: Path) -> None:
    from ai_agent_lib_core.contracts import Obligations, RowFilter

    source, seen = await started(tmp_path, accounts_api)
    with pytest.raises(PolicyDenied):
        await source.query(
            "all_accounts", obligations=Obligations(row_filters=(RowFilter("branch", ("n",)),))
        )
    assert seen.requests == []
    await source.aclose()


# ---------------------------------------------------------------- definitions


@pytest.mark.parametrize(
    ("definition", "problem"),
    [
        ("- a list\n", "<root>"),
        ("description: x\npath: /a\nmax_rows: 1\n", "columns"),
        ("description: x\npath: /a\nmax_rows: 0\ncolumns: {a: a}\n", "max_rows"),
        ("description: x\npath: /a\nmax_rows: 1\ncolumns: {a: a}\nverb: GET\n", "verb"),
        ("description: x\nmethod: DELETE\npath: /a\nmax_rows: 1\ncolumns: {a: a}\n", "method"),
        ("description: x\npath: a\nmax_rows: 1\ncolumns: {a: a}\n", "must start with '/'"),
        ("description: x\npath: /a?b=1\nmax_rows: 1\ncolumns: {a: a}\n", "no query string"),
        ("description: x\npath: /a/{id}\nmax_rows: 1\ncolumns: {a: a}\n", "placeholders"),
        (
            "description: x\npath: /a\nparameters: {id: {type: string, in: path}}\n"
            "max_rows: 1\ncolumns: {a: a}\n",
            "placeholders",
        ),
        (
            "description: x\npath: /a/{id}\n"
            "parameters: {id: {type: string, in: path, default: z}}\n"
            "max_rows: 1\ncolumns: {a: a}\n",
            "cannot have a default",
        ),
        (
            "description: x\npath: /a\nparameters: {q: {type: string, in: body}}\n"
            "max_rows: 1\ncolumns: {a: a}\n",
            "GET request cannot have body",
        ),
        (
            "description: x\npath: /a\nparameters: {n: {type: integer, default: many}}\n"
            "max_rows: 1\ncolumns: {a: a}\n",
            "default of parameter 'n' is not a integer",
        ),
        (
            "description: x\npath: /a\nparameters: {N: {type: integer}}\n"
            "max_rows: 1\ncolumns: {a: a}\n",
            "parameter name",
        ),
        ("description: x\npath: /a\nmax_rows: 1\ncolumns: {Bad Name: a}\n", "column name"),
        (
            "description: x\npath: /a\nmax_rows: 1\ncolumns: {a: a}\nclassification: top\n",
            "classification",
        ),
        ("description: [unclosed\n", "not valid YAML"),
        (
            "description: x\npath: /a\nparameters: {on: {type: date}}\n"
            "max_rows: 1\ncolumns: {a: a}\n",
            "put the name in quotes",
        ),
    ],
)
async def test_an_invalid_definition_stops_startup(
    tmp_path: Path, definition: str, problem: str
) -> None:
    with pytest.raises(ConfigurationError, match=problem) as caught:
        await started(tmp_path, accounts_api, extra={"broken": definition})
    assert "query 'broken'" in str(caught.value) or "broken.yaml" in str(caught.value)


async def test_yaml_cannot_build_python_objects(tmp_path: Path) -> None:
    hostile = "description: !!python/object/apply:os.system ['true']\npath: /a\n"
    with pytest.raises(ConfigurationError, match="not valid YAML"):
        await started(tmp_path, accounts_api, extra={"hostile": hostile})


async def test_a_definition_file_name_must_be_a_plain_identifier(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="file name is the query name"):
        await started(tmp_path, accounts_api, extra={"Get-Accounts": LOOKUP})


async def test_typed_defaults_come_from_the_definition(tmp_path: Path) -> None:
    source, _ = await started(tmp_path, accounts_api)
    defaults = {p.name: p.default for p in source.describe()["lookup"].parameters}
    assert defaults == {"account": None, "as_at": date(2026, 1, 31), "detailed": False}
    await source.aclose()


# -------------------------------------------------------------------- options


def options(tmp_path: Path, **values: object) -> RestOptions:
    return RestOptions.model_validate(
        {"base_url": "https://api.example.test", "queries_dir": tmp_path, **values}
    )


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"base_url": "ftp://files.example.test"}, "http\\(s\\) URL"),
        ({"base_url": "https://user:pw@api.example.test"}, "must not hold credentials"),
        ({"base_url": "https://api.example.test/?debug=1"}, "must not hold credentials"),
        ({"base_url": "http://api.internal.test"}, "must use https"),
        ({"auth_secret": "bank_token"}, "secret for its token is missing"),
        ({"use_proxy": True}, "no outbound proxy is configured"),
    ],
)
def test_unsafe_or_inconsistent_options_are_refused(
    tmp_path: Path, values: dict[str, object], problem: str
) -> None:
    with pytest.raises(ConfigurationError, match=problem) as caught:
        RestDataSource("bank", options(tmp_path, **values), FrozenClock())
    assert "pw" not in str(caught.value)


def test_plain_http_is_allowed_on_this_machine_or_when_asked_for(tmp_path: Path) -> None:
    RestDataSource("bank", options(tmp_path, base_url="http://localhost:8080"), FrozenClock())
    RestDataSource(
        "bank",
        options(tmp_path, base_url="http://api.internal.test", allow_http=True),
        FrozenClock(),
    )


async def test_a_ca_file_that_cannot_be_read_stops_startup(tmp_path: Path) -> None:
    not_a_ca = tmp_path / "ca.pem"
    not_a_ca.write_text("not a certificate", encoding="utf-8")
    for ca_file in (not_a_ca, tmp_path / "absent.pem"):
        source = RestDataSource("bank", options(tmp_path, ca_file=ca_file), FrozenClock())
        with pytest.raises(ConfigurationError, match="ca_file could not be read"):
            await source.start()


async def test_lifecycle_is_checked(tmp_path: Path) -> None:
    source = RestDataSource("bank", options(tmp_path / "absent"), FrozenClock())
    with pytest.raises(RuntimeError, match="not started"):
        source.describe()
    with pytest.raises(ConfigurationError, match="not started"):
        await source.validate()
    with pytest.raises(ConfigurationError, match="the queries folder does not exist"):
        await source.start()

    empty = RestDataSource("bank", options(tmp_path), FrozenClock())
    await empty.start()
    with pytest.raises(ConfigurationError, match="has no queries"):
        await empty.validate()
    with pytest.raises(RuntimeError, match="already started"):
        await empty.start()
    await empty.aclose()
    await empty.aclose()


# ------------------------------------------------------------------ container


async def test_the_container_gives_the_source_its_token_from_the_secrets_port(
    tmp_path: Path,
) -> None:
    fakes = Fakes(secrets=FakeSecretsProvider({"bank_token": "t0p"}))
    default = ServiceContainer(ServiceConfig.for_testing())._providers.lookup(DATA_PORT, "rest")
    assert not default.local_only
    registry = fakes.providers().register(DATA_PORT, "rest", default.factory)
    selection = ProviderSelection(
        provider="rest",
        options={
            "base_url": "http://localhost:9",
            "queries_dir": str(write_accounts_endpoints(tmp_path)),
            "auth_secret": "bank_token",
            "max_retries": 0,
        },
    )
    config = ServiceConfig.for_testing(data_sources={"bank": selection})
    async with ServiceContainer(config, registry) as services:
        await services.validate()
        source = services.data_source("bank")._source
        assert isinstance(source, RestDataSource)
        assert source._token is not None
        assert source._token.get_secret_value() == "t0p"
        assert sorted(source.describe()) == ["accounts_by_region", "all_accounts"]

    missing = ServiceConfig.for_testing(
        data_sources={
            "bank": ProviderSelection(
                provider="rest",
                options={**selection.options, "auth_secret": "absent"},
            )
        }
    )
    with pytest.raises(ConfigurationError, match="secret 'absent' is not configured"):
        await ServiceContainer(missing, registry).start()

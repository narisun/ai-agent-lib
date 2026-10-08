"""The stand-in for a REST API that a service's tests use."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ai_agent_lib_core.contracts import (
    Obligations,
    ProviderSelection,
    ServiceConfig,
    ValidationFailed,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.testing import (
    Fakes,
    accounts_api,
    rest_stub_providers,
    rest_stub_transport,
    write_accounts_endpoints,
)

RECORDS = [
    {"id": 1, "owner": {"name": "Ann"}, "region": "east", "balance": 10.0},
    {"id": 2, "owner": {"name": "Bo"}, "region": "east", "balance": 20.0},
]


def config(tmp_path: Path, **options: object) -> ServiceConfig:
    selection = ProviderSelection(
        provider="rest",
        options={
            "base_url": "https://api.example.test/core",
            "queries_dir": str(write_accounts_endpoints(tmp_path)),
            **options,
        },
    )
    return ServiceConfig.for_testing(data_sources={"bank": selection})


async def test_a_rest_source_answers_from_the_table_with_every_check_still_running(
    tmp_path: Path,
) -> None:
    responses = {"GET /core/v1/regions/east/accounts": {"data": {"items": RECORDS}}}
    # A token and a proxy are configured; the test supplies neither.
    settings = config(tmp_path, auth_secret="bank_token", use_proxy=True)
    providers = rest_stub_providers(responses, Fakes().providers())
    async with ServiceContainer(settings, providers) as services:
        await services.validate()
        source = services.data_source("bank")._source
        result = await source.query(
            "accounts_by_region",
            {"region": "east"},
            obligations=Obligations(mask_columns=frozenset({"holder"})),
        )
        assert result.columns == ("account_id", "holder", "region", "balance")
        assert [row[0] for row in result.rows] == [1, 2]
        assert result.masked_columns == frozenset({"holder"})
        # A path with no entry is a 404, as it would be from the API.
        with pytest.raises(ValidationFailed, match="HTTP 404"):
            await source.query("accounts_by_region", {"region": "west"})


def request(method: str, path: str) -> httpx.Request:
    return httpx.Request(method, f"https://api.example.test{path}")


def test_an_entry_is_a_body_a_status_with_a_body_or_a_function() -> None:
    transport = rest_stub_transport(
        {
            "GET /a": [1, 2],
            "GET /b": (503, {"error": "busy"}),
            "POST /c": accounts_api,
        }
    )
    plain = transport.handle_request(request("GET", "/a?page=2"))
    assert (plain.status_code, plain.json()) == (200, [1, 2])
    assert plain.headers["content-type"] == "application/json"
    busy = transport.handle_request(request("GET", "/b"))
    assert (busy.status_code, busy.json()) == (503, {"error": "busy"})
    assert transport.handle_request(request("POST", "/c")).status_code == 404  # the function's own
    assert transport.handle_request(request("POST", "/a")).status_code == 404

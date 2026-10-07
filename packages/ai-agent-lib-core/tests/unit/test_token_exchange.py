"""Token exchange: the caller's token is traded for one bound to the service being called."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from pydantic import SecretStr

from ai_agent_lib_core.adapters import OAuthTokenExchanger
from ai_agent_lib_core.contracts import (
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
    TransientError,
)
from ai_agent_lib_core.testing import FrozenClock
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

AGENT_CLIENT = "22222222-2222-2222-2222-222222222222"
MCP = "api://accounts-mcp"


@pytest.fixture(scope="module")
def idp_keys() -> FakeIdentityProvider:
    provider = FakeIdentityProvider()
    provider.register_client(AGENT_CLIENT, "s3cret")
    return provider


@pytest.fixture
def idp(idp_keys: FakeIdentityProvider) -> FakeIdentityProvider:
    idp_keys.requests.clear()
    idp_keys.available = True
    return idp_keys


def exchanger(
    idp: FakeIdentityProvider, clock: FrozenClock | None = None, **overrides: Any
) -> OAuthTokenExchanger:
    settings: dict[str, Any] = {
        "kind": "on_behalf_of",
        "token_url": idp.token_url,
        "client_id": AGENT_CLIENT,
        "client_secret": SecretStr("s3cret"),
        "scope": "{audience}/.default",
        "clock": clock or FrozenClock(),
        "transport": idp.transport,
        **overrides,
    }
    return OAuthTokenExchanger(**settings)


def caller(idp: FakeIdentityProvider, subject: str = "u-7", **overrides: Any) -> RequestContext:
    token = idp.user_token(audience=AGENT_CLIENT, subject=subject, roles=["analyst"])
    values: dict[str, Any] = {
        "principal": Principal(subject=subject, tenant=idp.tenant_id, roles=frozenset({"analyst"})),
        "application": "accounts-agent",
        "request_id": "r-1",
        "thread_id": "th-1",
        "credential": SecretStr(token),
        **overrides,
    }
    return RequestContext(**values)


def form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def unverified(token: SecretStr) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token.get_secret_value(), options={"verify_signature": False}
    )
    return claims


async def test_on_behalf_of_sends_the_request_entra_documents(idp: FakeIdentityProvider) -> None:
    context = caller(idp)
    oauth = exchanger(idp)
    token = await oauth.exchange(context, MCP)
    await oauth.aclose()

    (request,) = idp.requests
    assert (request.method, str(request.url)) == ("POST", idp.token_url)
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert context.credential is not None
    assert form(request) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": AGENT_CLIENT,
        "client_secret": "s3cret",
        "assertion": context.credential.get_secret_value(),
        "scope": "api://accounts-mcp/.default",
        "requested_token_use": "on_behalf_of",
    }
    # The new token is for the MCP server, for the same user, and names the agent.
    claims = unverified(token)
    assert (claims["aud"], claims["oid"], claims["azp"]) == (MCP, "u-7", AGENT_CLIENT)
    assert token.get_secret_value() != context.credential.get_secret_value()
    assert "s3cret" not in repr(oauth)


async def test_rfc_8693_sends_the_standard_request_with_client_authentication(
    idp: FakeIdentityProvider,
) -> None:
    context = caller(idp)
    oauth = exchanger(idp, kind="token_exchange", scope=None)
    token = await oauth.exchange(context, "accounts-mcp")
    await oauth.aclose()

    (request,) = idp.requests
    assert context.credential is not None
    assert form(request) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": context.credential.get_secret_value(),
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "accounts-mcp",
    }
    basic = base64.b64decode(request.headers["authorization"].removeprefix("Basic ")).decode()
    assert basic == f"{AGENT_CLIENT}:s3cret"
    assert unverified(token)["aud"] == "accounts-mcp"


async def test_a_token_is_reused_until_shortly_before_it_expires(idp: FakeIdentityProvider) -> None:
    clock = FrozenClock()
    oauth = exchanger(idp, clock)
    context = caller(idp)
    first = await oauth.exchange(context, MCP)
    assert await oauth.exchange(context, MCP) == first
    assert len(idp.requests) == 1

    # Another audience and another caller each get their own token.
    await oauth.exchange(context, "api://rates-mcp")
    await oauth.exchange(caller(idp, "u-8"), MCP)
    assert len(idp.requests) == 3

    clock.advance(3600 - 61)
    assert await oauth.exchange(context, MCP) == first
    clock.advance(2)
    await oauth.exchange(context, MCP)
    assert len(idp.requests) == 4
    await oauth.aclose()


async def test_a_refusal_is_a_deny_that_names_only_the_error_code(
    idp: FakeIdentityProvider,
) -> None:
    wrong_secret = exchanger(idp, client_secret=SecretStr("wrong"))
    with pytest.raises(PolicyDenied) as caught:
        await wrong_secret.exchange(caller(idp), MCP)
    assert caught.value.reason_code == "token_exchange_refused"
    assert "invalid_client" in str(caught.value)
    assert "Trace ID" not in str(caught.value)
    assert "AADSTS" not in str(caught.value)
    await wrong_secret.aclose()

    # A token that was not issued to this agent cannot be exchanged by it.
    oauth = exchanger(idp)
    foreign = caller(
        idp, credential=SecretStr(idp.user_token(audience="someone-else", subject="u"))
    )
    with pytest.raises(PolicyDenied, match="invalid_grant"):
        await oauth.exchange(foreign, MCP)
    with pytest.raises(PolicyDenied) as missing:
        await oauth.exchange(caller(idp, credential=None), MCP)
    assert missing.value.reason_code == "credential_missing"
    await oauth.aclose()


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"token_type": "Bearer"}),
        httpx.Response(200, json={"access_token": ""}),
        httpx.Response(200, text="<html>"),
        httpx.Response(302, headers={"location": "https://elsewhere.test"}),
        httpx.Response(400, json={"error": "has spaces and <tags>"}),
    ],
)
async def test_an_answer_that_is_not_a_token_is_a_deny(reply: httpx.Response) -> None:
    idp = FakeIdentityProvider()
    oauth = exchanger(idp, transport=httpx.MockTransport(lambda request: reply))
    with pytest.raises(PolicyDenied) as caught:
        await oauth.exchange(caller(idp), MCP)
    assert caught.value.reason_code == "token_exchange_refused"
    assert "<tags>" not in str(caught.value)
    await oauth.aclose()


async def test_an_outage_is_transient_and_nothing_is_cached(idp: FakeIdentityProvider) -> None:
    oauth = exchanger(idp)
    idp.available = False
    with pytest.raises(TransientError):
        await oauth.exchange(caller(idp), MCP)
    idp.available = True
    assert (await oauth.exchange(caller(idp), MCP)).get_secret_value()
    await oauth.aclose()

    for status in (500, 503, 429):
        busy = exchanger(
            idp, transport=httpx.MockTransport(lambda request, s=status: httpx.Response(s))
        )
        with pytest.raises(TransientError, match=f"HTTP {status}"):
            await busy.exchange(caller(idp), MCP)
        await busy.aclose()


async def test_an_application_acting_for_itself_asks_in_its_own_name(
    idp: FakeIdentityProvider,
) -> None:
    service = caller(
        idp,
        principal=Principal(subject="sp-1", tenant=idp.tenant_id, kind=PrincipalKind.SERVICE),
        credential=None,
    )
    oauth = exchanger(idp)
    token = await oauth.exchange(service, MCP)
    await oauth.aclose()
    (request,) = idp.requests
    assert form(request)["grant_type"] == "client_credentials"
    assert form(request)["scope"] == "api://accounts-mcp/.default"
    claims = unverified(token)
    assert (claims["aud"], claims["idtyp"], claims["azp"]) == (MCP, "app", AGENT_CLIENT)


async def test_a_service_gets_a_token_of_its_own_with_its_client_credentials(
    idp: FakeIdentityProvider,
) -> None:
    clock = FrozenClock()
    agent = exchanger(idp, clock)

    own = await agent.service_token(MCP)
    again = await agent.service_token(MCP)
    for_the_user = await agent.exchange(caller(idp), MCP)

    (first, second) = idp.requests
    assert form(first)["grant_type"] == "client_credentials"
    assert form(first)["scope"] == f"{MCP}/.default"
    assert "assertion" not in form(first)
    assert again == own  # kept until shortly before it expires
    claims = jwt.decode(own.get_secret_value(), options={"verify_signature": False})
    assert (claims["aud"], claims["azp"], claims["idtyp"]) == (MCP, AGENT_CLIENT, "app")
    # The service's own token and a user's are never confused in the cache.
    assert form(second)["requested_token_use"] == "on_behalf_of"
    assert for_the_user != own

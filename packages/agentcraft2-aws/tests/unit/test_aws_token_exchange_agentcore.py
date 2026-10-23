"""Tokens for other services through AgentCore Identity, against a stubbed client."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.stub import Stubber
from pydantic import SecretStr, ValidationError

from agentcraft2_aws.testing import offline_sessions
from agentcraft2_aws.token_exchange_agentcore import (
    AgentCoreExchangeOptions,
    AgentCoreTokenExchanger,
)
from agentcraft2_core.contracts import (
    ConfigurationError,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
    TokenExchanger,
    TransientError,
    UpstreamError,
)
from agentcraft2_core.testing import FrozenClock

PROVIDER = "entra-provider"
WORKLOAD = "accounts-agent"
AUDIENCE = "api://accounts-mcp"
CONSENT_URL = "https://consent.example.test/authorize?state=secret-state"


def options(**changes: Any) -> AgentCoreExchangeOptions:
    settings: dict[str, Any] = {
        "credential_provider": PROVIDER,
        "scopes": ["{audience}/.default", "openid"],
        "workload_name": WORKLOAD,
    }
    settings.update(changes)
    return AgentCoreExchangeOptions.model_validate(settings)


class Exchange:
    """An exchanger over one stubbed ``bedrock-agentcore`` client."""

    def __init__(self, clock: FrozenClock | None = None, **changes: Any) -> None:
        sessions = offline_sessions()
        self.client = sessions.client("bedrock-agentcore")
        self.stub = Stubber(self.client)
        self.clock = clock if clock is not None else FrozenClock()
        self.exchanger = AgentCoreTokenExchanger(
            options(**changes), sessions, self.clock, client=self.client
        )

    def expect_resource_token(
        self,
        workload_token: str,
        flow: str,
        token: str = "downstream",  # noqa: S107 - a placeholder, not a credential
        **more: Any,
    ) -> None:
        self.stub.add_response(
            "get_resource_oauth2_token",
            {"accessToken": token},
            {
                "workloadIdentityToken": workload_token,
                "resourceCredentialProviderName": PROVIDER,
                "scopes": [f"{AUDIENCE}/.default", "openid"],
                "oauth2Flow": flow,
                **more,
            },
        )


@pytest.fixture
def exchange() -> Iterator[Exchange]:
    found = Exchange()
    with found.stub:
        yield found
    found.stub.assert_no_pending_responses()


def context(
    kind: PrincipalKind = PrincipalKind.USER, credential: str | None = None
) -> RequestContext:
    return RequestContext(
        principal=Principal(
            subject="user-42", tenant="acme", kind=kind, authenticated_by="agentcore-runtime"
        ),
        application="accounts-agent",
        request_id="r-1",
        thread_id="t-1",
        credential=SecretStr(credential) if credential is not None else None,
    )


async def test_a_users_workload_token_is_exchanged_with_user_federation(
    exchange: Exchange,
) -> None:
    exchange.expect_resource_token("wat-user", "USER_FEDERATION")

    token = await exchange.exchanger.exchange(context(credential="wat-user"), AUDIENCE)

    assert token.get_secret_value() == "downstream"
    assert isinstance(exchange.exchanger, TokenExchanger)


async def test_the_return_url_goes_with_a_users_exchange_only() -> None:
    found = Exchange(return_url="https://agent.example.test/consented")
    with found.stub:
        found.expect_resource_token(
            "wat-user",
            "USER_FEDERATION",
            resourceOauth2ReturnUrl="https://agent.example.test/consented",
        )
        found.expect_resource_token("wat-service", "M2M")
        await found.exchanger.exchange(context(credential="wat-user"), AUDIENCE)
        await found.exchanger.exchange(
            context(PrincipalKind.SERVICE, credential="wat-service"), AUDIENCE
        )
    found.stub.assert_no_pending_responses()


async def test_a_service_with_a_workload_token_uses_it_machine_to_machine(
    exchange: Exchange,
) -> None:
    exchange.expect_resource_token("wat-service", "M2M", token="own")

    token = await exchange.exchanger.exchange(
        context(PrincipalKind.SERVICE, credential="wat-service"), AUDIENCE
    )

    assert token.get_secret_value() == "own"


async def test_a_service_without_a_token_mints_its_own_from_its_workload_name(
    exchange: Exchange,
) -> None:
    exchange.stub.add_response(
        "get_workload_access_token",
        {"workloadAccessToken": "wat-minted"},
        {"workloadName": WORKLOAD},
    )
    exchange.expect_resource_token("wat-minted", "M2M", token="own")

    assert (await exchange.exchanger.service_token(AUDIENCE)).get_secret_value() == "own"
    # A service principal with nothing to exchange asks in the service's own name: cached.
    assert (
        await exchange.exchanger.exchange(context(PrincipalKind.SERVICE), AUDIENCE)
    ).get_secret_value() == "own"


async def test_a_user_whose_token_the_runtime_kept_gets_one_minted_for_them(
    exchange: Exchange,
) -> None:
    exchange.stub.add_response(
        "get_workload_access_token_for_user_id",
        {"workloadAccessToken": "wat-for-user"},
        {"workloadName": WORKLOAD, "userId": "user-42"},
    )
    exchange.expect_resource_token("wat-for-user", "USER_FEDERATION")

    token = await exchange.exchanger.exchange(context(), AUDIENCE)

    assert token.get_secret_value() == "downstream"


async def test_without_a_workload_name_there_is_nothing_to_exchange() -> None:
    found = Exchange(workload_name=None)
    with found.stub:
        with pytest.raises(PolicyDenied) as user:
            await found.exchanger.exchange(context(), AUDIENCE)
        with pytest.raises(PolicyDenied) as service:
            await found.exchanger.service_token(AUDIENCE)
        # A token the runtime passed on is still exchanged.
        found.expect_resource_token("wat-user", "USER_FEDERATION")
        await found.exchanger.exchange(context(credential="wat-user"), AUDIENCE)
    assert user.value.reason_code == "credential_missing"
    assert service.value.reason_code == "credential_missing"
    assert "workload_name" in str(service.value)


async def test_a_user_who_has_not_consented_is_refused_and_the_url_stays_private(
    exchange: Exchange, caplog: pytest.LogCaptureFixture
) -> None:
    exchange.stub.add_response(
        "get_resource_oauth2_token",
        {
            "authorizationUrl": CONSENT_URL,
            "sessionUri": "urn:ietf:params:oauth:request_uri:abc123",
            "sessionStatus": "IN_PROGRESS",
        },
    )

    with (
        caplog.at_level(logging.DEBUG, logger="agentcraft2_aws"),
        pytest.raises(PolicyDenied) as caught,
    ):
        await exchange.exchanger.exchange(context(credential="wat-user"), AUDIENCE)

    assert caught.value.reason_code == "user_consent_required"
    assert "has not authorised this service" in caught.value.message
    assert AUDIENCE in caught.value.message
    assert "consent.example.test" not in str(caught.value)
    assert "secret-state" not in str(caught.value)
    assert "secret-state" not in caplog.text
    assert "consent.example.test" not in caplog.text


async def test_tokens_are_reused_per_caller_and_audience_until_they_age(
    exchange: Exchange,
) -> None:
    exchange.expect_resource_token("wat-a", "USER_FEDERATION", token="a-1")
    exchange.expect_resource_token("wat-b", "USER_FEDERATION", token="b-1")
    exchange.expect_resource_token("wat-a", "USER_FEDERATION", token="a-2")
    exchanger = exchange.exchanger

    first = await exchanger.exchange(context(credential="wat-a"), AUDIENCE)
    again = await exchanger.exchange(context(credential="wat-a"), AUDIENCE)
    other_caller = await exchanger.exchange(context(credential="wat-b"), AUDIENCE)
    exchange.clock.advance(299)
    still = await exchanger.exchange(context(credential="wat-a"), AUDIENCE)
    exchange.clock.advance(1)
    renewed = await exchanger.exchange(context(credential="wat-a"), AUDIENCE)

    assert first is again is still
    assert first.get_secret_value() == "a-1"
    assert other_caller.get_secret_value() == "b-1"
    assert renewed.get_secret_value() == "a-2"


async def test_another_audience_is_another_token(exchange: Exchange) -> None:
    exchange.expect_resource_token("wat-a", "USER_FEDERATION", token="for-mcp")
    exchange.stub.add_response(
        "get_resource_oauth2_token",
        {"accessToken": "for-other"},
        {
            "workloadIdentityToken": "wat-a",
            "resourceCredentialProviderName": PROVIDER,
            "scopes": ["api://other/.default", "openid"],
            "oauth2Flow": "USER_FEDERATION",
        },
    )

    mcp = await exchange.exchanger.exchange(context(credential="wat-a"), AUDIENCE)
    other = await exchange.exchanger.exchange(context(credential="wat-a"), "api://other")

    assert (mcp.get_secret_value(), other.get_secret_value()) == ("for-mcp", "for-other")


async def test_the_cache_is_configurable() -> None:
    found = Exchange(cache_seconds=10)
    with found.stub:
        found.expect_resource_token("wat-a", "USER_FEDERATION", token="a-1")
        found.expect_resource_token("wat-a", "USER_FEDERATION", token="a-2")
        await found.exchanger.exchange(context(credential="wat-a"), AUDIENCE)
        found.clock.advance(10)
        renewed = await found.exchanger.exchange(context(credential="wat-a"), AUDIENCE)
    assert renewed.get_secret_value() == "a-2"


@pytest.mark.parametrize(
    ("code", "status", "kind", "reason"),
    [
        ("AccessDeniedException", 403, ConfigurationError, None),
        ("ThrottlingException", 429, TransientError, None),
        ("InternalServerException", 500, TransientError, None),
        ("UnauthorizedException", 401, PolicyDenied, "credential_invalid"),
        ("ValidationException", 400, UpstreamError, None),
        ("ResourceNotFoundException", 404, UpstreamError, None),
    ],
)
async def test_a_refusal_by_aws_keeps_only_its_code(
    exchange: Exchange, code: str, status: int, kind: type[Exception], reason: str | None
) -> None:
    exchange.stub.add_client_error(
        "get_resource_oauth2_token",
        service_error_code=code,
        service_message="the service's own message",
        http_status_code=status,
    )

    with pytest.raises(kind) as caught:
        await exchange.exchanger.exchange(context(credential="wat-a"), AUDIENCE)

    assert code in str(caught.value)
    assert "the service's own message" not in str(caught.value)
    if reason is not None:
        assert isinstance(caught.value, PolicyDenied)
        assert caught.value.reason_code == reason


async def test_a_reply_without_a_token_is_an_upstream_error(exchange: Exchange) -> None:
    exchange.stub.add_response("get_resource_oauth2_token", {})

    with pytest.raises(UpstreamError, match="no token") as caught:
        await exchange.exchanger.exchange(context(credential="wat-a"), AUDIENCE)

    assert caught.value.expected == "a reply with 'accessToken'"


def test_the_exchanger_names_its_provider_but_no_token() -> None:
    found = Exchange()

    assert repr(found.exchanger) == (
        "AgentCoreTokenExchanger(credential_provider='entra-provider', "
        "workload_name='accounts-agent')"
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"credential_provider": "has space"}, "credential_provider"),
        ({"credential_provider": ""}, "credential_provider"),
        ({"workload_name": "ab"}, "workload_name"),
        ({"workload_name": "has/slash"}, "workload_name"),
        ({"return_url": "not a url"}, "return_url"),
        ({"cache_seconds": 0}, "cache_seconds"),
        ({"scopes": None}, "scopes"),
        ({"client_secret": "nope"}, "client_secret"),
    ],
)
def test_options_are_checked(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        options(**changes)


def test_scopes_are_required_but_may_be_empty() -> None:
    assert options(scopes=[]).scopes == ()
    with pytest.raises(ValidationError, match="scopes"):
        AgentCoreExchangeOptions.model_validate({"credential_provider": PROVIDER})

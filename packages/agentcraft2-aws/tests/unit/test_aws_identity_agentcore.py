"""Identity from the AgentCore runtime: the user header, the configured tenant, nothing else."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from agentcraft2_aws.identity_agentcore import (
    AUTHENTICATED_BY,
    AgentCoreRuntimeIdentity,
    AgentCoreRuntimeIdentityOptions,
    ExchangingAgentCoreRuntimeIdentity,
)
from agentcraft2_aws.pack import register_aws_adapters
from agentcraft2_aws.testing import offline_sessions
from agentcraft2_core.contracts import (
    ConfigurationError,
    DeploymentEnv,
    PolicyDenied,
    PresentedCredentials,
    Principal,
    PrincipalKind,
    ProviderSelection,
    RequestContext,
    RequestVerifier,
    Section,
    ServiceConfig,
    TokenExchanger,
)
from agentcraft2_core.di import ServiceContainer, ServiceProviders
from agentcraft2_core.testing import Fakes

USER_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"
TOKEN_HEADER = "x-amz-bedrock-agentcore-identity-wat"
OLD_TOKEN_HEADER = "workloadaccesstoken"
EXCHANGE = {
    "credential_provider": "entra-provider",
    "scopes": ["{audience}/.default"],
    "workload_name": "accounts-agent",
}


def options(**changes: Any) -> AgentCoreRuntimeIdentityOptions:
    settings: dict[str, Any] = {"tenant": "acme", "roles": ["analyst"]}
    settings.update(changes)
    return AgentCoreRuntimeIdentityOptions.model_validate(settings)


def identity(**changes: Any) -> AgentCoreRuntimeIdentity:
    return AgentCoreRuntimeIdentity(options(**changes))


def presented(**headers: str) -> PresentedCredentials:
    return PresentedCredentials(bearer=None, headers=headers)


def from_runtime(user: str = "user-42", **more: str) -> PresentedCredentials:
    return presented(**{USER_HEADER: user, **more})


async def test_the_user_header_becomes_a_principal_in_the_configured_tenant() -> None:
    verified = await identity().verify_request(from_runtime())

    assert verified.principal == Principal(
        subject="user-42",
        tenant="acme",
        roles=frozenset({"analyst"}),
        authenticated_by=AUTHENTICATED_BY,
        kind=PrincipalKind.USER,
    )
    assert verified.principal.authenticated_by == "agentcore-runtime"
    assert verified.principal.delegation_chain == ()
    assert verified.credential is None


async def test_the_header_names_are_matched_whatever_their_case() -> None:
    verifier = identity(user_header="X-Amzn-Bedrock-AgentCore-Runtime-User-Id")

    assert verifier.required_headers == (USER_HEADER,)
    verified = await verifier.verify_request(from_runtime("user-7"))
    assert verified.principal.subject == "user-7"


@pytest.mark.parametrize(
    "credentials",
    [
        presented(),
        presented(**{USER_HEADER: ""}),
        presented(**{TOKEN_HEADER: "wat-only"}),
        PresentedCredentials(bearer="a-token", headers={}),
    ],
)
async def test_a_request_without_the_user_header_is_refused(
    credentials: PresentedCredentials,
) -> None:
    with pytest.raises(PolicyDenied) as caught:
        await identity().verify_request(credentials)

    assert caught.value.reason_code == "perimeter_identity_missing"
    assert USER_HEADER in str(caught.value)


@pytest.mark.parametrize("user", [" user-42", "user-42 ", "user\x0042", "user\t42", "\x1b[31m"])
async def test_a_user_id_that_is_not_an_identifier_is_refused_without_repeating_it(
    user: str,
) -> None:
    with pytest.raises(PolicyDenied) as caught:
        await identity().verify_request(from_runtime(user))

    assert caught.value.reason_code == "credential_invalid"
    assert user not in str(caught.value)


async def test_the_same_user_id_in_two_tenants_gives_two_principals() -> None:
    acme = (await identity(tenant="acme").verify_request(from_runtime())).principal
    globex = (await identity(tenant="globex").verify_request(from_runtime())).principal

    assert acme != globex
    assert (acme.tenant, globex.tenant) == ("acme", "globex")
    assert acme.subject == globex.subject == "user-42"
    with pytest.raises(dataclasses.FrozenInstanceError):
        acme.tenant = "globex"  # type: ignore[misc]


@pytest.mark.parametrize("credential", [None, "", "a-bearer-token"])
async def test_a_bare_bearer_token_proves_nothing(credential: str | None) -> None:
    with pytest.raises(PolicyDenied) as caught:
        await identity().verify(credential)

    assert caught.value.reason_code == "perimeter_identity_missing"


async def test_the_workload_token_is_kept_as_the_credential_and_never_shown() -> None:
    verified = await identity().verify_request(from_runtime(**{TOKEN_HEADER: "wat-secret"}))

    assert verified.credential is not None
    assert verified.credential.get_secret_value() == "wat-secret"
    assert "wat-secret" not in repr(verified)
    assert "wat-secret" not in repr(verified.credential)


async def test_the_older_runtime_header_is_read_too_and_the_current_one_wins() -> None:
    older = await identity().verify_request(from_runtime(**{OLD_TOKEN_HEADER: "wat-old"}))
    both = await identity().verify_request(
        from_runtime(**{OLD_TOKEN_HEADER: "wat-old", TOKEN_HEADER: "wat-new"})
    )

    assert older.credential is not None
    assert older.credential.get_secret_value() == "wat-old"
    assert both.credential is not None
    assert both.credential.get_secret_value() == "wat-new"


def test_it_trusts_the_perimeter_and_names_the_header_it_needs() -> None:
    verifier = identity()

    assert isinstance(verifier, RequestVerifier)
    assert verifier.trusts_perimeter is True
    assert verifier.required_headers == (USER_HEADER,)
    assert not isinstance(verifier, TokenExchanger)
    assert repr(verifier) == "AgentCoreRuntimeIdentity(tenant='acme')"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tenant": ""}, "tenant"),
        ({"tenant": " acme"}, "tenant"),
        ({"roles": [""]}, "role"),
        ({"user_header": ""}, "header name"),
        ({"user_header": "two words"}, "header name"),
        ({"workload_token_headers": ["ok", "not ok"]}, "header name"),
        ({"exchange": {"credential_provider": "p", "scopes": [], "nope": 1}}, "nope"),
        ({"password": "hunter2"}, "password"),
    ],
)
def test_options_are_checked(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        options(**changes)


class FakeExchanger:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.closed = 0

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:
        self.asked.append((context.principal.subject, audience))
        return SecretStr(f"token-for-{audience}")

    async def service_token(self, audience: str) -> SecretStr:
        self.asked.append(("", audience))
        return SecretStr(f"own-token-for-{audience}")

    async def aclose(self) -> None:
        self.closed += 1


async def test_with_an_exchanger_the_identity_also_exchanges_and_closes_it() -> None:
    exchanger = FakeExchanger()
    verifier = ExchangingAgentCoreRuntimeIdentity(options(), exchanger)
    verified = await verifier.verify_request(from_runtime(**{TOKEN_HEADER: "wat"}))
    context = RequestContext(
        principal=verified.principal,
        application="accounts-agent",
        request_id="r-1",
        thread_id="t-1",
        credential=verified.credential,
    )

    assert isinstance(verifier, TokenExchanger)
    assert isinstance(verifier, RequestVerifier)
    assert (
        await verifier.exchange(context, "api://mcp")
    ).get_secret_value() == "token-for-api://mcp"
    assert (
        await verifier.service_token("api://mcp")
    ).get_secret_value() == "own-token-for-api://mcp"
    assert exchanger.asked == [("user-42", "api://mcp"), ("", "api://mcp")]
    await verifier.aclose()
    assert exchanger.closed == 1


# ----------------------------------------------------------------- the pack


def providers() -> tuple[Fakes, ServiceProviders]:
    fakes = Fakes()
    return fakes, register_aws_adapters(fakes.providers(), sessions=offline_sessions())


def config(env: DeploymentEnv, **more: Any) -> ServiceConfig:
    return ServiceConfig.for_testing(
        deployment_env=env,
        sections={
            Section.IDENTITY: ProviderSelection("agentcore_runtime", {"tenant": "acme", **more})
        },
    )


async def test_the_factory_refuses_to_build_in_local_development() -> None:
    fakes, registry = providers()
    assert registry.lookup(Section.IDENTITY, "agentcore_runtime").local_only is False

    with pytest.raises(ConfigurationError, match="local development") as caught:
        async with ServiceContainer(config(DeploymentEnv.LOCAL), registry, clock=fakes.clock):
            pass

    assert caught.value.fix is not None
    assert "identity=static" in caught.value.fix


@pytest.mark.parametrize("env", [DeploymentEnv.DEV, DeploymentEnv.PROD])
async def test_the_factory_builds_a_verifying_identity_where_a_runtime_exists(
    env: DeploymentEnv,
) -> None:
    fakes, registry = providers()

    async with ServiceContainer(config(env), registry, clock=fakes.clock) as services:
        verifier = services.identity
        assert isinstance(verifier, AgentCoreRuntimeIdentity)
        assert not isinstance(verifier, TokenExchanger)
        verified = await verifier.verify_request(from_runtime())
        assert verified.principal.tenant == "acme"


async def test_with_exchange_options_the_factory_builds_an_exchanging_identity() -> None:
    fakes, registry = providers()

    async with ServiceContainer(
        config(DeploymentEnv.PROD, exchange=EXCHANGE), registry, clock=fakes.clock
    ) as services:
        assert isinstance(services.identity, ExchangingAgentCoreRuntimeIdentity)
        assert isinstance(services.identity, TokenExchanger)

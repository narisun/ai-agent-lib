"""The jwt identity provider: signed access tokens, with Microsoft Entra ID as a preset."""

from __future__ import annotations

import sys
from typing import Any

import jwt
import pytest
from pydantic import SecretStr

from ai_agent_lib_core.adapters import (
    ExchangingJwtIdentity,
    JwtIdentityOptions,
    JwtIdentityVerifier,
    resolve_jwt_options,
)
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AuditOutcome,
    Classification,
    ConfigurationError,
    DeploymentEnv,
    PolicyDenied,
    PrincipalKind,
    ProviderSelection,
    Section,
    ServiceConfig,
    TokenAuthenticator,
    TokenExchanger,
)
from ai_agent_lib_core.di import ServiceContainer, ServiceProviders
from ai_agent_lib_core.testing import FakeRegistry, Fakes, FakeSecretsProvider, FrozenClock
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

TENANT = "11111111-1111-1111-1111-111111111111"
API = "api://accounts-mcp"
AGENT_CLIENT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(scope="module")
def idp_keys() -> FakeIdentityProvider:
    """One provider for the module: generating a signing key is the slow part."""
    return FakeIdentityProvider()


@pytest.fixture
def idp(idp_keys: FakeIdentityProvider) -> FakeIdentityProvider:
    idp_keys.requests.clear()
    idp_keys.available = True
    return idp_keys


def verifier(
    idp: FakeIdentityProvider, clock: FrozenClock | None = None, **options: Any
) -> JwtIdentityVerifier:
    settings = resolve_jwt_options(JwtIdentityOptions.model_validate(idp.options(API, **options)))
    return JwtIdentityVerifier(settings, clock or FrozenClock(), transport=idp.transport)


async def refusal(verifier: JwtIdentityVerifier, token: str | None) -> str:
    with pytest.raises(PolicyDenied) as caught:
        await verifier.verify(token)
    if token:
        assert token not in str(caught.value)
    return caught.value.reason_code


# ------------------------------------------------------------------- settings


def test_the_entra_preset_fills_in_the_standard_settings() -> None:
    settings = resolve_jwt_options(
        JwtIdentityOptions(preset="entra", tenant_id=TENANT.upper(), audience="client-guid")
    )
    assert settings.issuer == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert settings.jwks_url == f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    assert settings.audiences == ("client-guid",)
    assert settings.algorithms == ("RS256",)
    assert dict(settings.claims) == {
        "subject": "oid",
        "tenant": "tid",
        "roles": "roles",
        "actor": "azp",
        "scopes": "scp",
        "kind": "idtyp",
    }
    assert settings.expected_tenant == TENANT
    assert settings.exchange_kind is None


def test_the_preset_covers_version_one_tokens_national_clouds_and_overrides() -> None:
    v1 = resolve_jwt_options(
        JwtIdentityOptions(preset="entra", tenant_id=TENANT, token_version=1, audience=API)
    )
    assert v1.issuer == f"https://sts.windows.net/{TENANT}/"
    assert v1.jwks_url == f"https://login.microsoftonline.com/{TENANT}/discovery/keys"
    assert v1.claims["actor"] == "appid"

    cloud = resolve_jwt_options(
        JwtIdentityOptions(
            preset="entra",
            tenant_id=TENANT,
            authority="https://login.microsoftonline.us",
            audience=[API, "client-guid"],
            claims={"roles": "groups"},
            exchange={"client_id": AGENT_CLIENT},
        )
    )
    assert cloud.issuer == f"https://login.microsoftonline.us/{TENANT}/v2.0"
    assert cloud.audiences == (API, "client-guid")
    assert cloud.claims["roles"] == "groups"
    assert cloud.claims["subject"] == "oid"
    assert cloud.exchange_kind == "on_behalf_of"
    assert cloud.token_url == f"https://login.microsoftonline.us/{TENANT}/oauth2/v2.0/token"
    assert cloud.exchange_scope == "{audience}/.default"


def test_another_identity_provider_is_a_matter_of_options() -> None:
    settings = resolve_jwt_options(
        JwtIdentityOptions(
            issuer="https://idp.example.test/realms/bank",
            jwks_url="https://idp.example.test/realms/bank/certs",
            audience="accounts-mcp",
            tenant="bank",
            algorithms=["ES256", "RS256"],
            claims={"roles": "realm_roles", "actor": "client_id"},
            service_tokens_are="without_scopes",
            exchange={
                "client_id": "accounts-agent",
                "token_url": "https://idp.example.test/realms/bank/token",
            },
        )
    )
    assert settings.claims["subject"] == "sub"
    assert settings.fixed_tenant == "bank"
    assert settings.expected_tenant is None
    # Without a preset the exchange is the standard one, RFC 8693.
    assert settings.exchange_kind == "token_exchange"
    assert settings.exchange_scope is None


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        ({"preset": "entra", "audience": API}, "needs tenant_id"),
        ({"preset": "entra", "tenant_id": "common", "audience": API}, "needs tenant_id"),
        ({"preset": "entra", "tenant_id": "contoso.onmicrosoft.com", "audience": API}, "GUID"),
        ({"audience": API}, "set issuer and jwks_url"),
        (
            {"issuer": "https://idp.test", "jwks_url": "https://idp.test/keys", "audience": API},
            "name the tenant claim",
        ),
        (
            {
                "issuer": "https://i.test",
                "jwks_url": "http://i.test/k",
                "audience": API,
                "tenant": "t",
            },
            "must use https",
        ),
        (
            {
                "issuer": "https://i.test",
                "jwks_url": "https://i.test/k",
                "audience": API,
                "tenant": "t",
                "exchange": {"client_id": "c"},
            },
            "exchange needs token_url",
        ),
        ({"preset": "entra", "tenant_id": TENANT, "audience": []}, "audience must name"),
    ],
)
def test_incomplete_or_unsafe_options_are_refused(options: dict[str, Any], problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem):
        resolve_jwt_options(JwtIdentityOptions.model_validate(options))


def test_symmetric_and_unsigned_algorithms_cannot_even_be_configured() -> None:
    for algorithm in ("HS256", "none"):
        with pytest.raises(ConfigurationError, match="algorithms"):
            ProviderSelection(
                "jwt",
                {
                    "preset": "entra",
                    "tenant_id": TENANT,
                    "audience": API,
                    "algorithms": [algorithm],
                },
            ).parse_options(JwtIdentityOptions)


# --------------------------------------------------------------- verification


async def test_a_users_token_becomes_a_principal_with_the_agent_that_presented_it(
    idp: FakeIdentityProvider,
) -> None:
    token = idp.user_token(
        audience=API, subject="user-oid-7", roles=["analyst", "manager"], client_id=AGENT_CLIENT
    )
    identity = verifier(idp)
    principal = await identity.verify(token)
    await identity.aclose()
    assert principal.subject == "user-oid-7"
    assert principal.tenant == TENANT
    assert principal.roles == frozenset({"analyst", "manager"})
    assert principal.kind is PrincipalKind.USER
    # The application the token was issued to is the agent acting for the user.
    assert principal.delegation_chain == (AGENT_CLIENT,)
    assert principal.actor == AGENT_CLIENT
    assert principal.authenticated_by == idp.issuer
    assert not isinstance(identity, TokenExchanger)


async def test_each_way_a_token_can_be_wrong_has_its_own_reason(idp: FakeIdentityProvider) -> None:
    clock = FrozenClock()
    identity = verifier(idp, clock)
    good = idp.claims(audience=API, subject="u-1") | {"azp": "x", "scp": "access_as_user"}
    other = FakeIdentityProvider()
    cases: dict[str, list[str | None]] = {
        "credential_missing": [None, ""],
        "credential_invalid": [
            "not-a-token",
            "a.b.c",
            "x" * 20_000,
            # Signed by someone else, under a key ID this issuer does publish.
            other.sign(good, kid="key-1"),
            idp.sign(good, kid=None, typ="JWT").rsplit(".", 1)[0] + ".AAAA",
            idp.sign({key: value for key, value in good.items() if key != "exp"}),
            idp.sign(good | {"exp": "tomorrow"}),
            idp.sign(good | {"nbf": good["nbf"] + 3600}),
            idp.sign({key: value for key, value in good.items() if key != "oid"}),
            idp.sign(good | {"roles": [1, 2]}),
            jwt.encode(
                good,
                "shared-secret-shared-secret-shared-secret",
                algorithm="HS256",
                headers={"kid": "key-1"},
            ),
            jwt.encode(good, "", algorithm="none", headers={"kid": "key-1"}),
        ],
        "credential_audience": [idp.sign(good | {"aud": "api://payments-mcp"})],
        "credential_issuer": [idp.sign(good | {"iss": "https://login.example.test/other/v2.0"})],
        "credential_tenant": [idp.sign(good | {"tid": "99999999-9999-9999-9999-999999999999"})],
        "credential_expired": [idp.sign(good | {"exp": good["iat"] - 61})],
    }
    for reason, tokens in cases.items():
        for token in tokens:
            assert await refusal(identity, token) == reason, (reason, token and token[:40])
    assert (await identity.verify(idp.sign(good))).subject == "u-1"
    await identity.aclose()


async def test_the_validity_period_follows_the_injected_clock_with_leeway(
    idp: FakeIdentityProvider,
) -> None:
    clock = FrozenClock()
    identity = verifier(idp, clock, leeway_seconds=30)
    token = idp.user_token(audience=API, subject="u-1", expires_in=600)
    clock.advance(600 + 29)
    assert (await identity.verify(token)).subject == "u-1"
    clock.advance(1)
    assert await refusal(identity, token) == "credential_expired"
    await identity.aclose()


async def test_an_applications_own_token_is_refused_unless_they_are_accepted(
    idp: FakeIdentityProvider,
) -> None:
    app = idp.app_token(audience=API, client_id=AGENT_CLIENT, roles=["Ledger.Read.All"])
    # Without the optional idtyp claim, a token with no delegated scope is still an app's own.
    unmarked = idp.sign(idp.claims(audience=API, subject="sp-1") | {"azp": AGENT_CLIENT})
    strict = verifier(idp)
    assert await refusal(strict, app) == "service_token_refused"
    assert await refusal(strict, unmarked) == "service_token_refused"
    await strict.aclose()

    accepting = verifier(idp, accept_service_tokens=True)
    principal = await accepting.verify(app)
    await accepting.aclose()
    assert principal.kind is PrincipalKind.SERVICE
    assert principal.roles == frozenset({"Ledger.Read.All"})
    assert principal.actor == AGENT_CLIENT


async def test_a_users_token_must_carry_every_required_scope(idp: FakeIdentityProvider) -> None:
    identity = verifier(idp, required_scopes=["access_as_user", "Accounts.Read"])
    narrow = idp.user_token(audience=API, subject="u-1", scopes="access_as_user")
    wide = idp.user_token(audience=API, subject="u-1", scopes="Accounts.Read access_as_user x")
    assert await refusal(identity, narrow) == "scope_missing"
    assert (await identity.verify(wide)).subject == "u-1"
    await identity.aclose()


async def test_claim_names_are_configuration(idp: FakeIdentityProvider) -> None:
    identity = verifier(idp, claims={"roles": "groups", "subject": "sub", "actor": None})
    token = idp.user_token(
        audience=API, subject="u-1", roles=["ignored"], groups="g-analysts g-ops"
    )
    principal = await identity.verify(token)
    await identity.aclose()
    assert principal.subject == "pairwise-u-1"
    assert principal.roles == frozenset({"g-analysts", "g-ops"})
    assert principal.delegation_chain == ()


# ---------------------------------------------------------------- signing keys


async def test_signing_keys_are_read_once_and_kept(idp: FakeIdentityProvider) -> None:
    clock = FrozenClock()
    identity = verifier(idp, clock, jwks_cache_seconds=3600)
    token = idp.user_token(audience=API, subject="u-1")
    for _ in range(5):
        await identity.verify(token)
    assert len(idp.requests) == 1
    assert str(idp.requests[0].url) == idp.jwks_url

    clock.advance(3600)
    await identity.verify(idp.user_token(audience=API, subject="u-1"))
    assert len(idp.requests) == 2
    await identity.aclose()


async def test_a_rotated_key_is_picked_up_but_made_up_key_ids_cannot_flood_the_issuer() -> None:
    idp = FakeIdentityProvider()
    clock = FrozenClock()
    identity = verifier(idp, clock)
    await identity.verify(idp.user_token(audience=API, subject="u-1"))
    forged = jwt.encode({"aud": API}, "k" * 40, algorithm="HS256", headers={"kid": "made-up"})
    for _ in range(10):
        assert await refusal(identity, forged) == "credential_invalid"
    assert len(idp.requests) == 1

    idp.rotate_keys()
    rotated = idp.user_token(audience=API, subject="u-2")
    # Within a minute of the last read the keys are not read again.
    assert await refusal(identity, rotated) == "credential_invalid"
    clock.advance(60)
    assert (await identity.verify(rotated)).subject == "u-2"
    assert len(idp.requests) == 2
    await identity.aclose()


async def test_an_outage_at_the_issuer_fails_closed_unless_keys_are_already_held(
    idp: FakeIdentityProvider,
) -> None:
    clock = FrozenClock()
    token = idp.user_token(audience=API, subject="u-1", expires_in=200_000)
    cold = verifier(idp, clock)
    idp.available = False
    assert await refusal(cold, token) == "identity_unavailable"
    with pytest.raises(ConfigurationError, match="signing keys could not be read"):
        await cold.validate()
    await cold.aclose()

    idp.available = True
    warm = verifier(idp, clock, jwks_cache_seconds=60)
    await warm.validate()
    idp.available = False
    clock.advance(120)
    assert (await warm.verify(token)).subject == "u-1"
    await warm.aclose()


async def test_keys_that_cannot_sign_are_ignored() -> None:
    idp = FakeIdentityProvider()
    published = idp.jwks()["keys"]
    idp.jwks = lambda: {  # type: ignore[method-assign]
        "keys": [{**published[0], "use": "enc"}, {"kty": "unheard-of", "kid": "x"}, "junk"]
    }
    identity = verifier(idp)
    with pytest.raises(ConfigurationError, match="no usable signing keys"):
        await identity.validate()
    await identity.aclose()


# ------------------------------------------------------------------ container


def providers(fakes: Fakes) -> ServiceProviders:
    spec = ServiceProviders.default().lookup(Section.IDENTITY, "jwt")
    assert not spec.local_only
    return fakes.providers().register(Section.IDENTITY, "jwt", spec.factory, replace=True)


async def test_the_container_builds_a_verifier_and_adds_exchange_when_configured() -> None:
    options = {"preset": "entra", "tenant_id": TENANT, "audience": API}
    plain = ServiceConfig.for_testing(
        deployment_env=DeploymentEnv.PROD,
        sections={Section.IDENTITY: ProviderSelection("jwt", options)},
    )
    async with ServiceContainer(plain, providers(Fakes())) as services:
        assert type(services.identity) is JwtIdentityVerifier
        assert "microsoftonline" in repr(services.identity)

    exchanging = ServiceConfig.for_testing(
        deployment_env=DeploymentEnv.PROD,
        sections={
            Section.IDENTITY: ProviderSelection(
                "jwt",
                {
                    **options,
                    "exchange": {"client_id": AGENT_CLIENT, "client_secret": "agent_secret"},
                },
            )
        },
    )
    fakes = Fakes(secrets=FakeSecretsProvider({"agent_secret": "s3cret"}))
    async with ServiceContainer(exchanging, providers(fakes)) as services:
        assert isinstance(services.identity, ExchangingJwtIdentity)
        assert isinstance(services.identity, TokenExchanger)
        assert "s3cret" not in repr(services.identity)

    with pytest.raises(ConfigurationError, match="secret 'agent_secret' is not configured"):
        await ServiceContainer(exchanging, providers(Fakes())).start()


async def test_a_missing_library_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jwt", None)
    settings = resolve_jwt_options(
        JwtIdentityOptions(preset="entra", tenant_id=TENANT, audience=API)
    )
    with pytest.raises(ConfigurationError, match=r"ai-agent-lib-core\[jwt\]"):
        JwtIdentityVerifier(settings, FrozenClock())


async def test_authenticate_builds_the_request_context_from_a_bearer_token(
    idp: FakeIdentityProvider,
) -> None:
    agent = AgentEntry(
        id="accounts-agent",
        owner="treasury",
        version="1",
        classification_ceiling=Classification.CONFIDENTIAL,
        client_id=AGENT_CLIENT,
    )
    fakes = Fakes(identity=verifier(idp), registry=FakeRegistry([agent], []))
    token = idp.user_token(audience=API, subject="u-7", roles=["analyst"], client_id="chat-ui")
    async with fakes.container() as services:
        context = await services.authenticate(token, application="accounts-agent", thread_id="th-1")
        other = await services.authenticate(
            token,
            application="unregistered-agent",
            thread_id="th-2",
            request_id="r-9",
            classification_ceiling=Classification.PUBLIC,
        )
        with pytest.raises(PolicyDenied):
            await services.authenticate("garbage", application="accounts-agent", thread_id="t")
        with pytest.raises(PolicyDenied):
            await services.authenticate(None, application="accounts-agent", thread_id="t")

    # A credential refused at the door leaves a record that names no caller.
    refused = [r for r in fakes.audit.records if r.event == "request.authenticate"]
    assert [(r.outcome, r.attributes["reason_code"]) for r in refused] == [
        (AuditOutcome.DENIED, "credential_invalid"),
        (AuditOutcome.DENIED, "credential_missing"),
    ]
    assert {(r.subject, r.tenant, r.application, r.thread_id) for r in refused} == {
        ("unknown", "unknown", "accounts-agent", "t")
    }
    assert "garbage" not in str([r.to_dict() for r in refused])
    assert ("request.authenticate", {"outcome": "denied"}) in fakes.telemetry.events

    assert context.principal.subject == "u-7"
    assert context.principal.actor == "chat-ui"
    assert (context.application, context.thread_id, context.request_id) == (
        "accounts-agent",
        "th-1",
        "id-1",
    )
    # The agent registry says how sensitive the data this agent handles may be.
    assert context.classification_ceiling is Classification.CONFIDENTIAL
    assert context.credential == SecretStr(token)
    assert token not in repr(context)
    assert (other.request_id, other.classification_ceiling) == ("r-9", Classification.PUBLIC)


# ------------------------------------------------------- a valid token, whoever it names


async def test_a_valid_token_can_be_told_from_an_entitled_caller(idp: FakeIdentityProvider) -> None:
    checks = verifier(idp)
    own = idp.app_token(audience=API, client_id=AGENT_CLIENT)

    # At a server's door the question is only whether the token is valid for this service.
    application = await checks.authenticate(own)
    assert application.kind is PrincipalKind.SERVICE
    assert application.actor == AGENT_CLIENT
    # Whether such a caller is accepted is a separate question, with its own answer.
    assert await refusal(checks, own) == "service_token_refused"

    person = await checks.authenticate(idp.user_token(audience=API, subject="u-7"))
    assert (person.kind, person.subject) == (PrincipalKind.USER, "u-7")
    assert isinstance(checks, TokenAuthenticator)
    assert checks.issuer == idp.issuer


async def test_authenticating_is_as_strict_about_the_token_itself(
    idp: FakeIdentityProvider,
) -> None:
    checks = verifier(idp, required_scopes=["access_as_user"])
    for token, reason in (
        (None, "credential_missing"),
        ("garbage", "credential_invalid"),
        (idp.user_token(audience="another-api", subject="u-7"), "credential_audience"),
        (idp.user_token(audience=API, subject="u-7", expires_in=-3600), "credential_expired"),
    ):
        with pytest.raises(PolicyDenied) as caught:
            await checks.authenticate(token)
        assert caught.value.reason_code == reason
    # A missing scope is about what the caller may do, not about the token being valid.
    other_scope = idp.user_token(audience=API, subject="u-7", scopes="something.else")
    assert (await checks.authenticate(other_scope)).subject == "u-7"
    assert await refusal(checks, other_scope) == "scope_missing"


# ------------------------------------------- R3: application tokens are never users


def generic(idp: FakeIdentityProvider, **options: Any) -> JwtIdentityVerifier:
    """A verifier for the same issuer, configured as a generic one, with no preset."""
    settings = resolve_jwt_options(
        JwtIdentityOptions(
            issuer=idp.issuer,
            jwks_url=idp.jwks_url,
            audience=API,
            tenant="bank",
            **options,
        )
    )
    return JwtIdentityVerifier(settings, FrozenClock(), transport=idp.transport)


def client_credentials_token(idp: FakeIdentityProvider) -> str:
    """An application's own token as some issuers shape it: sub and azp differ, no scope."""
    claims = idp.claims(audience=API, subject="unused")
    claims.update({"sub": "reports-job@clients", "azp": "reports-job"})
    return idp.sign(claims)


def test_r3_a_generic_issuer_must_say_how_application_tokens_look() -> None:
    with pytest.raises(ConfigurationError) as caught:
        resolve_jwt_options(
            JwtIdentityOptions(
                issuer="https://idp.test",
                jwks_url="https://idp.test/keys",
                audience=API,
                tenant="t",
            )
        )
    assert "cannot tell an application's own token from a user's" in str(caught.value)
    assert "service_tokens_are" in str(caught.value.expected)


async def test_r3_an_application_token_is_refused_where_only_users_are_accepted(
    idp: FakeIdentityProvider,
) -> None:
    by_scopes = generic(idp, service_tokens_are="without_scopes")
    assert await refusal(by_scopes, client_credentials_token(idp)) == "service_token_refused"

    by_kind = generic(idp, service_tokens_are="kind_claim", claims={"kind": "token_use"})
    # A token that does not say what it is is refused, never taken for a user's.
    assert await refusal(by_kind, client_credentials_token(idp)) == "credential_invalid"


async def test_r3_a_user_token_still_passes_the_explicit_rule(idp: FakeIdentityProvider) -> None:
    claims = idp.claims(audience=API, subject="unused")
    claims.update({"sub": "ann", "azp": "chat-ui", "scope": "accounts.read"})
    principal = await generic(idp, service_tokens_are="without_scopes").verify(idp.sign(claims))
    assert (principal.subject, principal.kind) == ("ann", PrincipalKind.USER)


# ------------------------------- F1: actor_is_subject never guesses a user


def test_f1_actor_is_subject_needs_an_actor_claim() -> None:
    with pytest.raises(ConfigurationError) as caught:
        resolve_jwt_options(
            JwtIdentityOptions(
                issuer="https://idp.test",
                jwks_url="https://idp.test/keys",
                audience=API,
                tenant="t",
                service_tokens_are="actor_is_subject",
                claims={"actor": None},
            )
        )
    assert "needs the claim that names the client" in str(caught.value)


@pytest.mark.parametrize(
    "actor",
    [pytest.param(None, id="missing"), "", ["reports-job"], 7, {"id": "x"}],
)
async def test_f1_a_token_whose_actor_cannot_be_read_is_refused_not_taken_for_a_user(
    idp: FakeIdentityProvider, actor: object
) -> None:
    claims = idp.claims(audience=API, subject="unused")
    claims.update({"sub": "reports-job", "roles": ["admin"]})
    claims.pop("azp", None)
    if actor is not None:
        claims["azp"] = actor
    by_actor = generic(idp, service_tokens_are="actor_is_subject")
    assert await refusal(by_actor, idp.sign(claims)) == "credential_invalid"


async def test_f1_matching_actor_and_subject_is_an_application_and_distinct_is_a_user(
    idp: FakeIdentityProvider,
) -> None:
    by_actor = generic(idp, service_tokens_are="actor_is_subject")
    claims = idp.claims(audience=API, subject="unused")
    claims.update({"sub": "reports-job", "azp": "reports-job"})
    assert await refusal(by_actor, idp.sign(claims)) == "service_token_refused"
    accepting = generic(idp, service_tokens_are="actor_is_subject", accept_service_tokens=True)
    assert (await accepting.verify(idp.sign(claims))).kind is PrincipalKind.SERVICE

    claims.update({"sub": "ann", "azp": "chat-ui"})
    principal = await by_actor.verify(idp.sign(claims))
    assert (principal.subject, principal.kind) == ("ann", PrincipalKind.USER)

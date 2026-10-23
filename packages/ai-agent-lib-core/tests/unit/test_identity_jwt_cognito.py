"""The jwt identity provider's cognito preset: a user pool whose access tokens carry no aud."""

from __future__ import annotations

import jwt
import pytest

from ai_agent_lib_core.adapters import JwtIdentityOptions, JwtIdentityVerifier, resolve_jwt_options
from ai_agent_lib_core.contracts import ConfigurationError, PolicyDenied, PrincipalKind
from ai_agent_lib_core.testing import FrozenClock
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

USER_POOL = "eu-west-1_AbCdEfGhI"
COGNITO_ISSUER = f"https://cognito-idp.eu-west-1.amazonaws.com/{USER_POOL}"
APP_CLIENT = "7h3app1cl13nt1d"


@pytest.fixture(scope="module")
def idp() -> FakeIdentityProvider:
    """One provider for the module: generating a signing key is the slow part."""
    return FakeIdentityProvider()


# ------------------------------------------------------------------- settings


def test_the_cognito_preset_fills_in_the_pools_issuer_keys_and_claims() -> None:
    settings = resolve_jwt_options(
        JwtIdentityOptions(
            preset="cognito", user_pool_id=USER_POOL, audience=APP_CLIENT, tenant="acme"
        )
    )
    assert settings.issuer == COGNITO_ISSUER
    assert settings.jwks_url == f"{COGNITO_ISSUER}/.well-known/jwks.json"
    assert settings.audiences == (APP_CLIENT,)
    assert settings.audience_claim == "client_id"
    assert dict(settings.claims) == {
        "subject": "sub",
        "tenant": None,
        "roles": "cognito:groups",
        "actor": "client_id",
        "scopes": "scope",
        "kind": None,
    }
    assert settings.fixed_tenant == "acme"
    assert settings.expected_tenant is None
    assert settings.service_tokens_are == "actor_is_subject"
    assert (settings.exchange_kind, settings.token_url) == (None, None)


def test_the_other_presets_keep_the_standard_audience_claim() -> None:
    generic = resolve_jwt_options(
        JwtIdentityOptions(
            issuer="https://issuer.example",
            jwks_url="https://issuer.example/keys",
            audience="api",
            tenant="t",
            service_tokens_are="without_scopes",
        )
    )
    assert generic.audience_claim == "aud"
    assert JwtIdentityOptions(audience="api").audience_claim == "aud"


def test_the_cognito_preset_refuses_incomplete_options() -> None:
    with pytest.raises(ConfigurationError, match="needs user_pool_id"):
        resolve_jwt_options(JwtIdentityOptions(preset="cognito", audience=APP_CLIENT, tenant="t"))
    with pytest.raises(ValueError, match="user_pool_id"):
        JwtIdentityOptions(preset="cognito", user_pool_id="nonsense", audience="x", tenant="t")
    with pytest.raises(ConfigurationError, match="tenant"):
        resolve_jwt_options(
            JwtIdentityOptions(preset="cognito", user_pool_id=USER_POOL, audience=APP_CLIENT)
        )


def test_cognito_has_no_delegation_grant_so_exchange_is_spelled_out() -> None:
    with pytest.raises(ConfigurationError, match="exchange needs token_url"):
        resolve_jwt_options(
            JwtIdentityOptions(
                preset="cognito",
                user_pool_id=USER_POOL,
                audience=APP_CLIENT,
                tenant="t",
                exchange={"client_id": APP_CLIENT},
            )
        )
    with pytest.raises(ConfigurationError, match="exchange needs kind"):
        resolve_jwt_options(
            JwtIdentityOptions(
                preset="cognito",
                user_pool_id=USER_POOL,
                audience=APP_CLIENT,
                tenant="t",
                exchange={"client_id": APP_CLIENT, "token_url": "https://auth.example/token"},
            )
        )
    explicit = resolve_jwt_options(
        JwtIdentityOptions(
            preset="cognito",
            user_pool_id=USER_POOL,
            audience=APP_CLIENT,
            tenant="t",
            audience_claim="aud",
            exchange={
                "client_id": APP_CLIENT,
                "kind": "token_exchange",
                "token_url": "https://auth.example/token",
            },
        )
    )
    assert explicit.audience_claim == "aud"
    assert explicit.exchange_kind == "token_exchange"
    assert explicit.token_url == "https://auth.example/token"


# ------------------------------------------------------------------- tokens


def cognito_verifier(idp: FakeIdentityProvider) -> JwtIdentityVerifier:
    """A verifier for a Cognito pool whose keys the fake provider publishes."""
    settings = resolve_jwt_options(
        JwtIdentityOptions(
            preset="cognito",
            user_pool_id=USER_POOL,
            audience=APP_CLIENT,
            tenant="acme",
            jwks_url=idp.jwks_url,
            accept_service_tokens=True,
        )
    )
    return JwtIdentityVerifier(settings, FrozenClock(), transport=idp.transport)


def cognito_token(idp: FakeIdentityProvider, *, client_id: str, subject: str = "u-7") -> str:
    """A Cognito access token: the app client is in client_id, and there is no aud."""
    claims = idp.claims(audience=client_id, subject=subject, audience_claim="client_id")
    claims.update(
        {"iss": COGNITO_ISSUER, "sub": subject, "scope": "openid profile"},
        **{"cognito:groups": ["analysts"]},
    )
    return idp.sign(claims)


async def refusal(verifier: JwtIdentityVerifier, token: str) -> str:
    with pytest.raises(PolicyDenied) as caught:
        await verifier.verify(token)
    assert token not in str(caught.value)
    return caught.value.reason_code


async def test_a_cognito_access_token_names_its_audience_in_client_id(
    idp: FakeIdentityProvider,
) -> None:
    token = cognito_token(idp, client_id=APP_CLIENT)
    assert "aud" not in jwt.decode(token, options={"verify_signature": False})
    principal = await cognito_verifier(idp).verify(token)
    assert (principal.subject, principal.tenant) == ("u-7", "acme")
    assert principal.roles == {"analysts"}
    assert principal.delegation_chain == (APP_CLIENT,)
    assert principal.kind is PrincipalKind.USER
    assert principal.authenticated_by == COGNITO_ISSUER


async def test_a_cognito_token_for_another_app_client_is_refused(
    idp: FakeIdentityProvider,
) -> None:
    verifier = cognito_verifier(idp)
    code = await refusal(verifier, cognito_token(idp, client_id="another-client"))
    assert code == "credential_audience"
    # A token with the standard aud but no client_id is not for this service either.
    without_client = idp.user_token(audience=APP_CLIENT, subject="u-7", iss=COGNITO_ISSUER)
    assert await refusal(verifier, without_client) == "credential_audience"


async def test_a_cognito_client_credentials_token_is_the_applications_own(
    idp: FakeIdentityProvider,
) -> None:
    # Cognito issues sub == client_id for the client-credentials grant.
    token = cognito_token(idp, client_id=APP_CLIENT, subject=APP_CLIENT)
    principal = await cognito_verifier(idp).verify(token)
    assert principal.kind is PrincipalKind.SERVICE
    assert principal.subject == APP_CLIENT

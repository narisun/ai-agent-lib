"""Verifying OAuth 2.0 access tokens that are signed JWTs.

The adapter follows the open standards, not one vendor: it checks the
signature against the issuer's published keys (a JWKS), then the issuer, the
audience and the validity period, and builds the principal from claims whose
names are configuration. A preset fills those settings in for Microsoft Entra
ID, so moving to another identity provider is a change of options, not code.

The options and their resolution live in ``identity_jwt_options`` and the key
cache in ``jwks``; both are re-exported here, so every name this module ever
offered still imports from it.

PyJWT is an optional dependency; install the ``jwt`` extra of
``ai-agent-lib-core`` to use this adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from ai_agent_lib_core.adapters.http_support import tls_verification
from ai_agent_lib_core.adapters.identity_jwt_options import (
    Algorithm,
    ClaimRole,
    ExchangeKind,
    JwtIdentityOptions,
    JwtSettings,
    ServiceTokenRule,
    TokenExchangeOptions,
    resolve_jwt_options,
)
from ai_agent_lib_core.adapters.jwks import SigningKeys
from ai_agent_lib_core.contracts import (
    Clock,
    ConfigurationError,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
    SupportsAsyncClose,
    TokenExchanger,
)

__all__ = [
    "Algorithm",
    "ClaimRole",
    "ExchangeKind",
    "ExchangingJwtIdentity",
    "JwtIdentityOptions",
    "JwtIdentityVerifier",
    "JwtSettings",
    "ServiceTokenRule",
    "SigningKeys",
    "TokenExchangeOptions",
    "resolve_jwt_options",
]

_INSTALL_HINT = "install it with: pip install 'ai-agent-lib-core[jwt]'"
_AUDIENCE = "aud"
_WHAT = "the jwt identity provider"
_MAX_TOKEN_CHARS = 16_384


def _denied(
    message: str,
    reason_code: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    fix: str | None = None,
) -> PolicyDenied:
    return PolicyDenied(message, reason_code=reason_code, expected=expected, actual=actual, fix=fix)


def _words(value: object) -> frozenset[str]:
    """Read a claim that lists names, as an array or as space-separated text."""
    if isinstance(value, str):
        return frozenset(value.split())
    if isinstance(value, list | tuple) and all(isinstance(item, str) for item in value):
        return frozenset(value)
    if value is None:
        return frozenset()
    raise ValueError("the claim is neither text nor a list of text")


class JwtIdentityVerifier:
    """Turns a signed access token into a verified principal.

    Args:
        settings: The resolved options.
        clock: Decides whether a token is within its validity period, and ages
            the cached signing keys.
        proxy: The outbound proxy, when the identity provider is reached through it.
        transport: Replaces the network, for tests.

    Raises:
        ConfigurationError: If PyJWT is not installed.
    """

    def __init__(
        self,
        settings: JwtSettings,
        clock: Clock,
        *,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            import jwt  # noqa: F401 - checked here so a missing extra stops startup
        except ImportError:
            raise ConfigurationError(
                f"{_WHAT} needs PyJWT with cryptography; {_INSTALL_HINT}"
            ) from None
        self._settings = settings
        self._clock = clock
        self._client = httpx.AsyncClient(
            verify=tls_verification(_WHAT, settings.ca_file),
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            # The environment is read by the configuration layer only.
            trust_env=False,
            proxy=proxy if transport is None else None,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._keys = SigningKeys(
            settings.jwks_url, self._client, clock, settings.jwks_cache_seconds
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(issuer={self._settings.issuer!r})"

    @property
    def issuer(self) -> str:
        """The issuer whose tokens are accepted."""
        return self._settings.issuer

    async def verify(self, credential: str | None) -> Principal:
        """Return the principal the token names, if this service accepts such a caller.

        Raises:
            PolicyDenied: If there is no token, or it is malformed, not signed
                by the issuer, expired, for another audience or issuer, lacks
                a required scope, or is an application's own token where
                those are not accepted. The reason code says which.
        """
        principal, scopes = await self._authenticated(credential)
        if principal.kind is PrincipalKind.SERVICE:
            if not self._settings.accept_service_tokens:
                raise _denied(
                    "the token belongs to an application, not a user", "service_token_refused"
                )
        elif not self._settings.required_scopes <= scopes:
            raise _denied("the token lacks a required scope", "scope_missing")
        return principal

    async def authenticate(self, credential: str | None) -> Principal:
        """Return the principal a valid token names, a person or an application.

        This checks the token and nothing else: its signature, issuer,
        audience, validity period and tenant. Whether such a caller may do
        anything is decided afterwards, by ``verify`` and by policy.

        Raises:
            PolicyDenied: If the token is missing or not valid for this service.
        """
        principal, _ = await self._authenticated(credential)
        return principal

    async def _authenticated(self, credential: str | None) -> tuple[Principal, frozenset[str]]:
        import jwt

        settings = self._settings
        if not credential:
            raise _denied("no token was presented", "credential_missing")
        if len(credential) > _MAX_TOKEN_CHARS:
            raise _denied("the token is malformed", "credential_invalid")
        try:
            header = jwt.get_unverified_header(credential)
        except jwt.PyJWTError:
            raise _denied("the token is malformed", "credential_invalid") from None
        kid = header.get("kid")
        # The allowlist holds asymmetric algorithms only, so a token cannot choose
        # "none" or have a public key treated as a shared secret.
        if header.get("alg") not in settings.algorithms or not isinstance(kid, str) or not kid:
            raise _denied("the token is not signed in an accepted way", "credential_invalid")
        key = await self._keys.get(kid)
        if key is None:
            raise _denied("the token is not signed by the issuer", "credential_invalid")
        # An issuer whose access tokens carry no aud names the audience in another
        # claim; PyJWT checks aud only, so that claim is checked here instead.
        standard_audience = settings.audience_claim == _AUDIENCE
        try:
            claims = jwt.decode(
                credential,
                key=key,
                algorithms=list(settings.algorithms),
                audience=list(settings.audiences) if standard_audience else None,
                issuer=settings.issuer,
                # The validity period is checked below against the injected clock.
                options={
                    "require": ["exp", "iss", *([_AUDIENCE] if standard_audience else [])],
                    "verify_aud": standard_audience,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.InvalidAudienceError:
            raise self._wrong_audience() from None
        except jwt.InvalidIssuerError:
            raise _denied(
                "the token is from another issuer",
                "credential_issuer",
                expected=f"a token whose iss is {settings.issuer!r}",
                fix="check tenant_id and token_version, or issuer, in the identity options",
            ) from None
        except jwt.PyJWTError:
            raise _denied("the token is not valid", "credential_invalid") from None
        if not standard_audience:
            self._check_audience(claims)
        self._check_validity_period(claims)
        return self._principal(claims)

    def _wrong_audience(self) -> PolicyDenied:
        settings = self._settings
        return _denied(
            "the token is for another service",
            "credential_audience",
            expected=(
                f"a token whose {settings.audience_claim} is "
                + " or ".join(map(repr, settings.audiences))
            ),
            fix=(
                "the caller must ask the identity provider for a token for this service "
                "(its scope or resource); or correct audience in the identity options"
            ),
        )

    def _check_audience(self, claims: Mapping[str, Any]) -> None:
        """Check the configured audience claim, a string or a list, as PyJWT checks ``aud``."""
        value = claims.get(self._settings.audience_claim)
        named = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        if not any(isinstance(name, str) and name in self._settings.audiences for name in named):
            raise self._wrong_audience()

    def _check_validity_period(self, claims: Mapping[str, Any]) -> None:
        now = self._clock.now().timestamp()
        leeway = self._settings.leeway_seconds
        expires, not_before = claims.get("exp"), claims.get("nbf")
        if isinstance(expires, bool) or not isinstance(expires, int | float):
            raise _denied("the token is not valid", "credential_invalid")
        if now >= expires + leeway:
            raise _denied(
                "the token has expired",
                "credential_expired",
                actual=f"it expired {round(now - expires)} seconds ago",
                fix="the caller must get a new token",
            )
        if not_before is not None and (
            isinstance(not_before, bool)
            or not isinstance(not_before, int | float)
            or now < not_before - leeway
        ):
            raise _denied("the token is not valid yet", "credential_invalid")

    def _principal(self, claims: Mapping[str, Any]) -> tuple[Principal, frozenset[str]]:
        settings = self._settings
        names = settings.claims

        def claim(role: ClaimRole) -> object:
            name = names.get(role)
            return claims.get(name) if name is not None else None

        tenant = claim("tenant") if names.get("tenant") is not None else settings.fixed_tenant
        if settings.expected_tenant is not None and (
            not isinstance(tenant, str) or tenant.lower() != settings.expected_tenant
        ):
            raise _denied("the token is from another tenant", "credential_tenant")
        actor = claim("actor")
        try:
            scopes = _words(claim("scopes"))
            is_service = self._is_service_token(claims, claim)
            principal = Principal(
                subject=claim("subject"),  # type: ignore[arg-type]
                tenant=tenant,  # type: ignore[arg-type]
                roles=_words(claim("roles")),
                authenticated_by=settings.issuer,
                delegation_chain=(actor,) if isinstance(actor, str) and actor else (),
                kind=PrincipalKind.SERVICE if is_service else PrincipalKind.USER,
            )
        except (TypeError, ValueError):
            raise _denied(
                "the token does not carry the claims this service needs", "credential_invalid"
            ) from None
        return principal, scopes

    def _is_service_token(
        self, claims: Mapping[str, object], claim: Callable[[ClaimRole], object]
    ) -> bool:
        """Apply the configured rule; a token it cannot classify is refused."""
        settings = self._settings
        rule = settings.service_tokens_are
        kind_value = claim("kind")
        if rule == "entra":
            if kind_value is not None:
                if not isinstance(kind_value, str):
                    raise _unclassifiable()
                return kind_value in settings.service_kinds
            return settings.claims.get("scopes") not in claims
        if rule == "kind_claim":
            if not isinstance(kind_value, str):
                raise _unclassifiable()
            return kind_value in settings.service_kinds
        if rule == "without_scopes":
            return settings.claims.get("scopes") not in claims
        # actor_is_subject: only a token naming both, as text, can be classified.
        actor, subject = claim("actor"), claim("subject")
        if not (isinstance(actor, str) and actor and isinstance(subject, str) and subject):
            raise _unclassifiable()
        return actor == subject

    async def validate(self) -> None:
        """Check that the issuer's signing keys can be read.

        Raises:
            ConfigurationError: If they cannot.
        """
        await self._keys.refresh()

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


def _unclassifiable() -> PolicyDenied:
    return _denied(
        "the token does not say whether it is a user's or an application's",
        "credential_invalid",
    )


class ExchangingJwtIdentity(JwtIdentityVerifier):
    """A verifier for a service that also calls other services for its callers.

    Args:
        settings: The resolved options.
        clock: Ages the cached signing keys.
        exchanger: Obtains the tokens presented to the services this one calls.
        proxy: The outbound proxy, when the identity provider is reached through it.
        transport: Replaces the network, for tests.
    """

    def __init__(
        self,
        settings: JwtSettings,
        clock: Clock,
        exchanger: TokenExchanger,
        *,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(settings, clock, proxy=proxy, transport=transport)
        self._exchanger = exchanger

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:
        """Return a token for the caller that only ``audience`` accepts."""
        return await self._exchanger.exchange(context, audience)

    async def service_token(self, audience: str) -> SecretStr:
        """Return a token for this service itself that only ``audience`` accepts."""
        return await self._exchanger.service_token(audience)

    async def aclose(self) -> None:
        """Close the HTTP clients."""
        await super().aclose()
        if isinstance(self._exchanger, SupportsAsyncClose):
            await self._exchanger.aclose()

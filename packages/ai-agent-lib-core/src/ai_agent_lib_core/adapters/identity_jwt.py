"""Verifying OAuth 2.0 access tokens that are signed JWTs.

The adapter follows the open standards, not one vendor: it checks the
signature against the issuer's published keys (a JWKS), then the issuer, the
audience and the validity period, and builds the principal from claims whose
names are configuration. A preset fills those settings in for Microsoft Entra
ID, so moving to another identity provider is a change of options, not code.

PyJWT is an optional dependency; install the ``jwt`` extra of
``ai-agent-lib-core`` to use this adapter.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import Field, SecretStr

from ai_agent_lib_core.adapters.http_support import checked_base_url, tls_verification
from ai_agent_lib_core.contracts import (
    Clock,
    ConfigurationError,
    OptionsModel,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
    TokenExchanger,
)

if TYPE_CHECKING:
    from jwt import PyJWK

__all__ = [
    "ExchangingJwtIdentity",
    "JwtIdentityOptions",
    "JwtIdentityVerifier",
    "JwtSettings",
    "TokenExchangeOptions",
    "resolve_jwt_options",
]

Algorithm = Literal[
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"
]
ClaimRole = Literal["subject", "tenant", "roles", "actor", "scopes", "kind"]
ExchangeKind = Literal["on_behalf_of", "token_exchange"]

_INSTALL_HINT = "install it with: pip install 'ai-agent-lib-core[jwt]'"
_WHAT = "the jwt identity provider"
_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_MAX_TOKEN_CHARS = 16_384
_MIN_REFRESH_SECONDS = 60.0
_APP_TOKEN = "app"  # noqa: S105 - the value of a claim, not a credential
_ENTRA_V2 = 2

_GENERIC_CLAIMS: Mapping[ClaimRole, str | None] = {
    "subject": "sub",
    "tenant": None,
    "roles": "roles",
    "actor": "azp",
    "scopes": "scope",
    "kind": None,
}
_ENTRA_CLAIMS: Mapping[ClaimRole, str | None] = {
    # The object ID is the same for a user in every application of the tenant.
    "subject": "oid",
    "tenant": "tid",
    # App roles assigned to the user, or to the application for an app-only token.
    "roles": "roles",
    "actor": "azp",
    "scopes": "scp",
    "kind": "idtyp",
}


class TokenExchangeOptions(OptionsModel):
    """How this service obtains tokens for the services it calls.

    Attributes:
        kind: ``on_behalf_of`` is the JWT bearer grant as Microsoft Entra ID
            implements delegation. ``token_exchange`` is RFC 8693. The default
            follows the preset.
        client_id: This service's own identifier at the identity provider.
        client_secret: The name of the secret that holds this service's client secret.
        token_url: The token endpoint. The preset supplies it.
        scope: The scope to ask for, where ``{audience}`` stands for the
            audience of the service being called.
        timeout_seconds: How long one exchange may take.
    """

    kind: ExchangeKind | None = None
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = "oauth_client_secret"  # noqa: S105 - the name of a secret
    token_url: str | None = None
    scope: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0)


class JwtIdentityOptions(OptionsModel):
    """Options of the ``jwt`` identity provider.

    Attributes:
        preset: ``entra`` fills in the issuer, key location, token endpoint and
            claim names for Microsoft Entra ID from ``tenant_id``.
        tenant_id: The Entra tenant (directory) ID.
        token_version: The version of access token this application is
            registered to receive from Entra.
        authority: The Entra sign-in host, for national clouds.
        issuer: The exact ``iss`` value to accept. Required without a preset.
        jwks_url: Where the issuer publishes its signing keys. Required without a preset.
        audience: The ``aud`` value or values that identify this service.
        algorithms: The signature algorithms to accept. Only asymmetric ones exist here.
        claims: Claim names that differ from the preset, by what they supply:
            ``subject``, ``tenant``, ``roles``, ``actor``, ``scopes``, ``kind``.
        tenant: The tenant every caller belongs to, when tokens carry no tenant claim.
        required_scopes: Delegated scopes a user's token must all carry.
        accept_service_tokens: Accept tokens an application obtained for
            itself, with no user. Refused unless this is set.
        leeway_seconds: Clock difference tolerated when checking validity.
        jwks_cache_seconds: How long the signing keys are kept before they are read again.
        timeout_seconds: How long reading the signing keys may take.
        ca_file: A CA file for the identity provider, when it is signed by a private authority.
        use_proxy: Reach the identity provider through the platform's outbound proxy.
        allow_http: Allow URLs without TLS on a host other than this machine.
        exchange: How to obtain tokens for the services this one calls. An MCP
            server that calls nothing else leaves it out.
    """

    preset: Literal["entra"] | None = None
    tenant_id: str | None = None
    token_version: Literal[1, 2] = 2
    authority: str = "https://login.microsoftonline.com"
    issuer: str | None = None
    jwks_url: str | None = None
    audience: str | tuple[str, ...]
    algorithms: tuple[Algorithm, ...] = Field(default=("RS256",), min_length=1)
    claims: dict[ClaimRole, str | None] = Field(default_factory=dict)
    tenant: str | None = None
    required_scopes: tuple[str, ...] = ()
    accept_service_tokens: bool = False
    leeway_seconds: int = Field(default=60, ge=0, le=300)
    jwks_cache_seconds: int = Field(default=86_400, ge=60)
    timeout_seconds: float = Field(default=5.0, gt=0)
    ca_file: Path | None = None
    use_proxy: bool = False
    allow_http: bool = False
    exchange: TokenExchangeOptions | None = None


@dataclass(frozen=True, slots=True)
class JwtSettings:
    """The options with the preset applied: everything the verifier acts on."""

    issuer: str
    jwks_url: str
    audiences: tuple[str, ...]
    algorithms: tuple[str, ...]
    claims: Mapping[ClaimRole, str | None]
    fixed_tenant: str | None
    expected_tenant: str | None
    required_scopes: frozenset[str]
    accept_service_tokens: bool
    scopes_mark_users: bool
    leeway_seconds: int
    jwks_cache_seconds: int
    timeout_seconds: float
    ca_file: Path | None
    exchange_kind: ExchangeKind | None
    token_url: str | None
    exchange_scope: str | None


@dataclass(frozen=True, slots=True)
class _Defaults:
    """What a preset, or the lack of one, supplies before the options override it."""

    issuer: str
    jwks_url: str
    claims: Mapping[ClaimRole, str | None]
    token_url: str | None
    exchange_kind: ExchangeKind
    exchange_scope: str | None
    expected_tenant: str | None


def _entra_defaults(options: JwtIdentityOptions) -> _Defaults:
    tenant_id = options.tenant_id or ""
    if not _GUID.match(tenant_id):
        raise ConfigurationError(
            f"{_WHAT}: the entra preset needs tenant_id, the directory (tenant) ID as a GUID; "
            "shared endpoints such as 'common' are not accepted"
        )
    tenant_id = tenant_id.lower()
    authority = checked_base_url(_WHAT, options.authority, allow_http=options.allow_http)
    if options.token_version == _ENTRA_V2:
        issuer = f"{authority}/{tenant_id}/v2.0"
        jwks_url = f"{authority}/{tenant_id}/discovery/v2.0/keys"
        claims: Mapping[ClaimRole, str | None] = _ENTRA_CLAIMS
    else:
        issuer = f"https://sts.windows.net/{tenant_id}/"
        jwks_url = f"{authority}/{tenant_id}/discovery/keys"
        claims = {**_ENTRA_CLAIMS, "actor": "appid"}
    return _Defaults(
        issuer=options.issuer or issuer,
        jwks_url=options.jwks_url or jwks_url,
        claims=claims,
        token_url=f"{authority}/{tenant_id}/oauth2/v2.0/token",
        exchange_kind="on_behalf_of",
        exchange_scope="{audience}/.default",
        expected_tenant=tenant_id,
    )


def _generic_defaults(options: JwtIdentityOptions) -> _Defaults:
    if not options.issuer or not options.jwks_url:
        raise ConfigurationError(f"{_WHAT}: set issuer and jwks_url, or choose a preset")
    return _Defaults(
        issuer=options.issuer,
        jwks_url=options.jwks_url,
        claims=_GENERIC_CLAIMS,
        token_url=None,
        exchange_kind="token_exchange",
        exchange_scope=None,
        expected_tenant=None,
    )


def _claim_names(options: JwtIdentityOptions, defaults: _Defaults) -> dict[ClaimRole, str | None]:
    claims: dict[ClaimRole, str | None] = {**defaults.claims, **options.claims}
    if not claims.get("subject"):
        raise ConfigurationError(f"{_WHAT}: a subject claim is required")
    if claims.get("tenant") is None and not options.tenant:
        raise ConfigurationError(
            f"{_WHAT}: name the tenant claim in claims, or set tenant to the tenant every "
            "caller belongs to"
        )
    return claims


def _exchange_settings(
    options: JwtIdentityOptions, defaults: _Defaults
) -> tuple[ExchangeKind | None, str | None, str | None]:
    """Return the kind, token endpoint and scope of token exchange, if it is configured."""
    exchange = options.exchange
    if exchange is None:
        return None, None, None
    token_url = exchange.token_url or defaults.token_url
    if token_url is None:
        raise ConfigurationError(f"{_WHAT}: exchange needs token_url")
    checked_base_url(f"{_WHAT} (token_url)", token_url, allow_http=options.allow_http)
    scope = exchange.scope if exchange.scope is not None else defaults.exchange_scope
    return exchange.kind or defaults.exchange_kind, token_url, scope


def resolve_jwt_options(options: JwtIdentityOptions) -> JwtSettings:
    """Apply the preset and check that the options are complete and safe.

    Raises:
        ConfigurationError: If a required setting is missing or a URL is unsafe.
    """
    audiences = (options.audience,) if isinstance(options.audience, str) else options.audience
    if not audiences or not all(audiences):
        raise ConfigurationError(f"{_WHAT}: audience must name this service")
    entra = options.preset == "entra"
    defaults = _entra_defaults(options) if entra else _generic_defaults(options)
    claims = _claim_names(options, defaults)
    checked_base_url(f"{_WHAT} (jwks_url)", defaults.jwks_url, allow_http=options.allow_http)
    exchange_kind, token_url, exchange_scope = _exchange_settings(options, defaults)
    return JwtSettings(
        issuer=defaults.issuer,
        jwks_url=defaults.jwks_url,
        audiences=tuple(audiences),
        algorithms=tuple(options.algorithms),
        claims=claims,
        fixed_tenant=options.tenant,
        expected_tenant=defaults.expected_tenant,
        required_scopes=frozenset(options.required_scopes),
        accept_service_tokens=options.accept_service_tokens,
        scopes_mark_users=entra,
        leeway_seconds=options.leeway_seconds,
        jwks_cache_seconds=options.jwks_cache_seconds,
        timeout_seconds=options.timeout_seconds,
        ca_file=options.ca_file,
        exchange_kind=exchange_kind,
        token_url=token_url,
        exchange_scope=exchange_scope,
    )


def _denied(message: str, reason_code: str) -> PolicyDenied:
    return PolicyDenied(message, reason_code=reason_code)


def _words(value: object) -> frozenset[str]:
    """Read a claim that lists names, as an array or as space-separated text."""
    if isinstance(value, str):
        return frozenset(value.split())
    if isinstance(value, list | tuple) and all(isinstance(item, str) for item in value):
        return frozenset(value)
    if value is None:
        return frozenset()
    raise ValueError("the claim is neither text nor a list of text")


class _SigningKeys:
    """The issuer's published keys, read when needed and kept for a while."""

    def __init__(
        self, url: str, client: httpx.AsyncClient, clock: Clock, keep_seconds: int
    ) -> None:
        self._url = url
        self._client = client
        self._clock = clock
        self._keep_seconds = keep_seconds
        self._keys: dict[str, PyJWK] = {}
        self._read_at: float | None = None
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        """Read the keys again.

        Raises:
            ConfigurationError: If they cannot be read or none of them is usable.
        """
        import jwt

        try:
            response = await self._client.get(self._url)
            document = response.json() if response.status_code == httpx.codes.OK else None
        except (httpx.HTTPError, ValueError) as exc:
            raise ConfigurationError(
                f"{_WHAT}: the signing keys could not be read ({type(exc).__name__})"
            ) from None
        listed = document.get("keys") if isinstance(document, Mapping) else None
        keys: dict[str, PyJWK] = {}
        for entry in listed if isinstance(listed, list) else ():
            if not isinstance(entry, Mapping) or entry.get("use", "sig") != "sig":
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK.from_dict(dict(entry))
            except jwt.PyJWTError:
                continue  # a key type this library cannot use; tokens signed with it are refused
        if not keys:
            raise ConfigurationError(f"{_WHAT}: the issuer published no usable signing keys")
        self._keys = keys
        self._read_at = self._clock.monotonic()

    async def get(self, kid: str) -> PyJWK | None:
        """Return the key called ``kid``, reading the keys again if that may help.

        An unknown key ID triggers one more read, because issuers rotate keys,
        but not more often than once a minute, so a stream of made-up key IDs
        cannot be turned into a stream of requests to the issuer.
        """
        async with self._lock:
            age = None if self._read_at is None else self._clock.monotonic() - self._read_at
            stale = age is None or age >= self._keep_seconds
            may_retry = age is None or age >= _MIN_REFRESH_SECONDS
            if stale or (kid not in self._keys and may_retry):
                try:
                    await self.refresh()
                except ConfigurationError:
                    if not self._keys:
                        raise _denied(
                            "the caller's token cannot be checked: the issuer's signing keys "
                            "are not available",
                            "identity_unavailable",
                        ) from None
                    # Keys rotate rarely: the ones already held stay in use until a read works.
            return self._keys.get(kid)


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
        self._keys = _SigningKeys(
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
        try:
            claims = jwt.decode(
                credential,
                key=key,
                algorithms=list(settings.algorithms),
                audience=list(settings.audiences),
                issuer=settings.issuer,
                # The validity period is checked below against the injected clock.
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.InvalidAudienceError:
            raise _denied("the token is for another service", "credential_audience") from None
        except jwt.InvalidIssuerError:
            raise _denied("the token is from another issuer", "credential_issuer") from None
        except jwt.PyJWTError:
            raise _denied("the token is not valid", "credential_invalid") from None
        self._check_validity_period(claims)
        return self._principal(claims)

    def _check_validity_period(self, claims: Mapping[str, Any]) -> None:
        now = self._clock.now().timestamp()
        leeway = self._settings.leeway_seconds
        expires, not_before = claims.get("exp"), claims.get("nbf")
        if isinstance(expires, bool) or not isinstance(expires, int | float):
            raise _denied("the token is not valid", "credential_invalid")
        if now >= expires + leeway:
            raise _denied("the token has expired", "credential_expired")
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
            kind_value = claim("kind")
            if kind_value is not None:
                is_service = kind_value == _APP_TOKEN
            elif settings.scopes_mark_users:
                # Without the optional claim, a token with no delegated scope is an app's own.
                is_service = names.get("scopes") not in claims
            else:
                is_service = isinstance(actor, str) and actor == claim("subject")
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

    async def validate(self) -> None:
        """Check that the issuer's signing keys can be read.

        Raises:
            ConfigurationError: If they cannot.
        """
        await self._keys.refresh()

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


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
        close = getattr(self._exchanger, "aclose", None)
        if close is not None:
            await close()

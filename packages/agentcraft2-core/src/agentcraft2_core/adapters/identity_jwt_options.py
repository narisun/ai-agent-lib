"""The options of the ``jwt`` identity provider, and what they resolve to.

They live apart from the verifier, which needs PyJWT, so that the provider
registry can declare and check them without loading it. ``resolve_jwt_options``
applies the preset (Microsoft Entra ID, or Amazon Cognito) and refuses options
that are incomplete or unsafe before any token is looked at.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from agentcraft2_core.adapters.http_support import checked_base_url
from agentcraft2_core.contracts import ConfigurationError, OptionsModel, shown_value

__all__ = [
    "Algorithm",
    "ClaimRole",
    "ExchangeKind",
    "JwtIdentityOptions",
    "JwtSettings",
    "Preset",
    "ServiceTokenRule",
    "TokenExchangeOptions",
    "resolve_jwt_options",
]

Algorithm = Literal[
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"
]
ClaimRole = Literal["subject", "tenant", "roles", "actor", "scopes", "kind"]
ServiceTokenRule = Literal["kind_claim", "without_scopes", "actor_is_subject"]
"""How a token an application obtained for itself is told from a user's.

``kind_claim``: the ``kind`` claim names it (``service_kinds`` lists the values);
a token without the claim is refused. ``without_scopes``: a token that carries no
delegated scope claim is an application's own. ``actor_is_subject``: the
application (``actor``) and the subject are the same, true of some issuers only.
"""
ExchangeKind = Literal["on_behalf_of", "token_exchange"]

Preset = Literal["entra", "cognito"]
"""An identity provider whose issuer, keys and claim names the options can fill in."""

_WHAT = "the jwt identity provider"
_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_USER_POOL = re.compile(r"^(?P<region>[a-z]{2}-[a-z]+-\d)_[A-Za-z0-9]+$")
_APP_TOKEN = "app"  # noqa: S105 - the value of a claim, not a credential
_ENTRA_V2 = 2
_AUDIENCE = "aud"

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
_COGNITO_CLAIMS: Mapping[ClaimRole, str | None] = {
    "subject": "sub",
    # A user pool is one tenant; the options name it.
    "tenant": None,
    "roles": "cognito:groups",
    # The app client the token was issued to; a client-credentials token has sub == client_id.
    "actor": "client_id",
    "scopes": "scope",
    "kind": None,
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
            claim names for Microsoft Entra ID from ``tenant_id``. ``cognito``
            fills in the issuer, key location and claim names for an Amazon
            Cognito user pool from ``user_pool_id``; its access tokens name the
            app client in ``client_id`` rather than ``aud``, so that is the
            audience claim, and ``tenant`` must be set because a pool carries
            no tenant claim. Cognito has no delegation grant, so ``exchange``
            needs its ``kind`` and ``token_url`` spelled out.
        tenant_id: The Entra tenant (directory) ID.
        user_pool_id: The Cognito user pool, ``{region}_{id}``.
        token_version: The version of access token this application is
            registered to receive from Entra.
        authority: The Entra sign-in host, for national clouds.
        issuer: The exact ``iss`` value to accept. Required without a preset.
        jwks_url: Where the issuer publishes its signing keys. Required without a preset.
        audience: The audience value or values that identify this service.
        audience_claim: The claim that carries the audience. ``aud`` by the
            standard; an issuer whose access tokens have none names it elsewhere.
        algorithms: The signature algorithms to accept. Only asymmetric ones exist here.
        claims: Claim names that differ from the preset, by what they supply:
            ``subject``, ``tenant``, ``roles``, ``actor``, ``scopes``, ``kind``.
        tenant: The tenant every caller belongs to, when tokens carry no tenant claim.
        required_scopes: Delegated scopes a user's token must all carry.
        accept_service_tokens: Accept tokens an application obtained for
            itself, with no user. Refused unless this is set.
        service_tokens_are: How an application's own token is told from a
            user's. Required without a preset, because issuers differ and a
            wrong guess would let an application act as a user.
        service_kinds: The values of the ``kind`` claim that mark an
            application's own token, for ``kind_claim``.
        leeway_seconds: Clock difference tolerated when checking validity.
        jwks_cache_seconds: How long the signing keys are kept before they are read again.
        timeout_seconds: How long reading the signing keys may take.
        ca_file: A CA file for the identity provider, when it is signed by a private authority.
        use_proxy: Reach the identity provider through the platform's outbound proxy.
        allow_http: Allow URLs without TLS on a host other than this machine.
        exchange: How to obtain tokens for the services this one calls. An MCP
            server that calls nothing else leaves it out.
    """

    preset: Preset | None = None
    tenant_id: str | None = None
    user_pool_id: str | None = Field(default=None, pattern=r"^[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]+$")
    token_version: Literal[1, 2] = 2
    authority: str = "https://login.microsoftonline.com"
    issuer: str | None = None
    jwks_url: str | None = None
    audience: str | tuple[str, ...]
    audience_claim: str = Field(default=_AUDIENCE, min_length=1, max_length=200)
    algorithms: tuple[Algorithm, ...] = Field(default=("RS256",), min_length=1)
    claims: dict[ClaimRole, str | None] = Field(default_factory=dict)
    tenant: str | None = None
    required_scopes: tuple[str, ...] = ()
    accept_service_tokens: bool = False
    service_tokens_are: ServiceTokenRule | None = None
    service_kinds: tuple[str, ...] = Field(default=(_APP_TOKEN,), min_length=1)
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
    service_tokens_are: ServiceTokenRule | Literal["entra"]
    service_kinds: frozenset[str]
    leeway_seconds: int
    jwks_cache_seconds: int
    timeout_seconds: float
    ca_file: Path | None
    exchange_kind: ExchangeKind | None
    token_url: str | None
    exchange_scope: str | None
    audience_claim: str = _AUDIENCE


@dataclass(frozen=True, slots=True)
class _Defaults:
    """What a preset, or the lack of one, supplies before the options override it."""

    issuer: str
    jwks_url: str
    claims: Mapping[ClaimRole, str | None]
    token_url: str | None
    exchange_kind: ExchangeKind | None
    exchange_scope: str | None
    expected_tenant: str | None
    audience_claim: str = _AUDIENCE
    service_tokens_are: ServiceTokenRule | None = None


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


def _cognito_defaults(options: JwtIdentityOptions) -> _Defaults:
    pool = _USER_POOL.match(options.user_pool_id or "")
    if pool is None:
        raise ConfigurationError(
            f"{_WHAT}: the cognito preset needs user_pool_id",
            expected="the user pool's ID, {region}_{id}, such as eu-west-1_AbCdEfGhI",
            actual=shown_value(options.user_pool_id),
            fix="copy the pool ID from the Cognito console or `aws cognito-idp list-user-pools`",
        )
    issuer = f"https://cognito-idp.{pool['region']}.amazonaws.com/{options.user_pool_id}"
    issuer = options.issuer or issuer
    return _Defaults(
        issuer=issuer,
        jwks_url=options.jwks_url or f"{issuer}/.well-known/jwks.json",
        claims=_COGNITO_CLAIMS,
        # Cognito has no delegation grant; exchange, if any, is spelled out in the options.
        token_url=None,
        exchange_kind=None,
        exchange_scope=None,
        expected_tenant=None,
        # Access tokens carry no aud; the app client they were issued to is in client_id.
        audience_claim="client_id",
        service_tokens_are="actor_is_subject",
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


def _service_token_rule(
    options: JwtIdentityOptions,
    claims: Mapping[ClaimRole, str | None],
    *,
    entra: bool,
    defaults: _Defaults,
) -> ServiceTokenRule | Literal["entra"]:
    """The rule that tells an application's own token from a user's, which must be explicit."""
    rule = options.service_tokens_are
    if rule is None and entra:
        # Entra marks app-only tokens with idtyp when the optional claim is
        # configured, and they never carry the delegated scp claim.
        return "entra"
    if rule is None:
        rule = defaults.service_tokens_are
    if rule is None:
        raise ConfigurationError(
            f"{_WHAT}: it cannot tell an application's own token from a user's",
            expected='"service_tokens_are": "kind_claim", "without_scopes" or "actor_is_subject"',
            actual="no rule, and no preset that brings one",
            fix=(
                "read how your issuer marks client-credentials tokens and choose the rule "
                "that matches; a wrong rule lets an application act as a user"
            ),
        )
    if rule == "kind_claim" and not claims.get("kind"):
        raise ConfigurationError(
            f"{_WHAT}: the kind_claim rule needs the claim that names the kind of token",
            expected='claims.kind, such as {"kind": "idtyp"}',
            actual="no kind claim",
        )
    if rule == "without_scopes" and not claims.get("scopes"):
        raise ConfigurationError(
            f"{_WHAT}: the without_scopes rule needs the delegated scopes claim",
            expected='claims.scopes, such as {"scopes": "scope"}',
            actual="no scopes claim",
        )
    if rule == "actor_is_subject" and not claims.get("actor"):
        raise ConfigurationError(
            f"{_WHAT}: the actor_is_subject rule needs the claim that names the client",
            expected='claims.actor, such as {"actor": "azp"}',
            actual="no actor claim",
        )
    return rule


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
    kind = exchange.kind or defaults.exchange_kind
    if kind is None:
        raise ConfigurationError(
            f"{_WHAT}: exchange needs kind",
            expected='"kind": "on_behalf_of" or "token_exchange" under exchange',
            actual="no kind, and a preset whose issuer has no delegation grant of its own",
        )
    return kind, token_url, scope


def resolve_jwt_options(options: JwtIdentityOptions) -> JwtSettings:
    """Apply the preset and check that the options are complete and safe.

    Raises:
        ConfigurationError: If a required setting is missing or a URL is unsafe.
    """
    audiences = (options.audience,) if isinstance(options.audience, str) else options.audience
    if not audiences or not all(audiences):
        raise ConfigurationError(f"{_WHAT}: audience must name this service")
    entra = options.preset == "entra"
    if entra:
        defaults = _entra_defaults(options)
    elif options.preset == "cognito":
        defaults = _cognito_defaults(options)
    else:
        defaults = _generic_defaults(options)
    claims = _claim_names(options, defaults)
    checked_base_url(f"{_WHAT} (jwks_url)", defaults.jwks_url, allow_http=options.allow_http)
    exchange_kind, token_url, exchange_scope = _exchange_settings(options, defaults)
    audience_claim = (
        options.audience_claim
        if "audience_claim" in options.model_fields_set
        else defaults.audience_claim
    )
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
        service_tokens_are=_service_token_rule(options, claims, entra=entra, defaults=defaults),
        service_kinds=frozenset(options.service_kinds),
        leeway_seconds=options.leeway_seconds,
        jwks_cache_seconds=options.jwks_cache_seconds,
        timeout_seconds=options.timeout_seconds,
        ca_file=options.ca_file,
        exchange_kind=exchange_kind,
        token_url=token_url,
        exchange_scope=exchange_scope,
        audience_claim=audience_claim,
    )

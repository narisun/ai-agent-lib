"""A fixed development identity, and development tokens that carry it between services."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json

from pydantic import Field, SecretStr

from ai_agent_lib_core.contracts import (
    Clock,
    OptionsModel,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
)

__all__ = ["StaticIdentityOptions", "StaticIdentityVerifier"]

_PREFIX = "agentlib-dev"
_ISSUER = "static-dev-identity"
_SERVICE_SUBJECT = "dev-service"
# Not a secret: this adapter is local-only and cannot start in a deployed environment.
_DEVELOPMENT_KEY = SecretStr("agentlib-local-development-only")


class StaticIdentityOptions(OptionsModel):
    """Options for the static development identity.

    Attributes:
        subject: The subject a caller without a development token is treated as.
        tenant: The tenant such a caller is treated as belonging to.
        roles: The roles such a caller is given.
        audience: The name this service accepts development tokens for. Without
            it a token for any audience is accepted.
        token_ttl_seconds: How long a development token is valid.
    """

    subject: str = "dev-user"
    tenant: str = "dev-tenant"
    roles: tuple[str, ...] = ()
    audience: str | None = None
    token_ttl_seconds: int = Field(default=300, gt=0, le=3600)


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class StaticIdentityVerifier:
    """Stands in for the identity provider on a developer's machine.

    A caller that shows no development token is the configured principal,
    whatever else it shows. So that an agent and an MCP server on the same
    machine agree on who the caller is, the adapter also issues short-lived
    development tokens: the agent exchanges the caller's identity for a token
    bound to the MCP server's audience, and the server rebuilds the principal
    from it. The tokens are signed with a development key, which is why the
    adapter is registered as local-only and cannot start anywhere else.

    Args:
        options: The development principal and the token settings.
        clock: Dates the tokens.
        key: The signing key. Defaults to a fixed development key.
    """

    def __init__(
        self, options: StaticIdentityOptions, clock: Clock, *, key: SecretStr | None = None
    ) -> None:
        self._options = options
        self._clock = clock
        self._key = (key if key is not None else _DEVELOPMENT_KEY).get_secret_value().encode()
        self._principal = Principal(
            subject=options.subject,
            tenant=options.tenant,
            roles=frozenset(options.roles),
            authenticated_by=_ISSUER,
        )

    def _signature(self, body: str) -> str:
        return _encode(hmac.new(self._key, f"{_PREFIX}.{body}".encode(), hashlib.sha256).digest())

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:
        """Return a development token for the caller that only ``audience`` accepts."""
        principal = context.principal
        return self._issue(
            {
                "sub": principal.subject,
                "tenant": principal.tenant,
                "roles": sorted(principal.roles),
                "aud": audience,
                "act": [*principal.delegation_chain, context.application],
                "kind": principal.kind.value,
            }
        )

    async def service_token(self, audience: str) -> SecretStr:
        """Return a development token for this service itself, naming no person."""
        return self._issue(
            {
                "sub": _SERVICE_SUBJECT,
                "tenant": self._options.tenant,
                "roles": [],
                "aud": audience,
                "act": [],
                "kind": PrincipalKind.SERVICE.value,
            }
        )

    def _issue(self, claims: dict[str, object]) -> SecretStr:
        claims["exp"] = int(self._clock.now().timestamp()) + self._options.token_ttl_seconds
        body = _encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        return SecretStr(f"{_PREFIX}.{body}.{self._signature(body)}")

    async def verify(self, credential: str | None) -> Principal:
        """Return the principal a development token names, or the configured one.

        Raises:
            PolicyDenied: If a development token is malformed, altered, expired
                or meant for another audience.
        """
        if credential is None or not credential.startswith(f"{_PREFIX}."):
            return self._principal
        try:
            _, body, signature = credential.split(".")
            if not hmac.compare_digest(signature, self._signature(body)):
                raise PolicyDenied(
                    "the development token is not signed with this key",
                    reason_code="credential_invalid",
                )
            claims = json.loads(_decode(body))
            expires = claims["exp"]
            audience = claims["aud"]
            principal = Principal(
                subject=claims["sub"],
                tenant=claims["tenant"],
                roles=frozenset(claims["roles"]),
                authenticated_by=_ISSUER,
                delegation_chain=tuple(claims["act"]),
                kind=PrincipalKind(claims.get("kind", PrincipalKind.USER.value)),
            )
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise PolicyDenied(
                "the development token is malformed", reason_code="credential_invalid"
            ) from None
        if not isinstance(expires, int) or self._clock.now().timestamp() >= expires:
            raise PolicyDenied(
                "the development token has expired", reason_code="credential_expired"
            )
        if self._options.audience is not None and audience != self._options.audience:
            raise PolicyDenied(
                "the development token is for another service", reason_code="credential_audience"
            )
        return principal

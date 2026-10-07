"""Obtaining a token that lets this service act for its caller at another service.

Two request shapes are supported behind one port. ``token_exchange`` is OAuth
2.0 Token Exchange, RFC 8693. ``on_behalf_of`` is the JWT bearer grant of RFC
7523 as Microsoft Entra ID uses it for delegation. Everything specific to one
identity provider is in the few lines that build the request.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Mapping
from typing import Literal

import httpx
from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    Clock,
    PolicyDenied,
    PrincipalKind,
    RequestContext,
    TransientError,
)

__all__ = ["OAuthTokenExchanger"]

_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - a grant type
_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a token type
_EARLY_SECONDS = 60.0
_DEFAULT_LIFETIME_SECONDS = 300.0
_MAX_CACHED = 1024
_MAX_ERROR_CODE = 64
_WHAT = "token exchange"
# The cache key of this service's own token. No hash of a user's token can equal it.
_OWN_NAME = "client"


class OAuthTokenExchanger:
    """Exchanges the caller's token for one bound to the service being called.

    The caller's own token is used only as the assertion sent to the identity
    provider. It is never forwarded to the other service. Tokens are reused
    until shortly before they expire, so a conversation does not cost one
    round trip to the identity provider per tool call.

    Args:
        kind: The request shape: ``on_behalf_of`` or ``token_exchange``.
        token_url: The identity provider's token endpoint.
        client_id: This service's own identifier at the identity provider.
        client_secret: This service's client secret.
        scope: The scope to ask for, where ``{audience}`` stands for the audience.
        clock: Ages the cached tokens.
        timeout_seconds: How long one exchange may take.
        verify: How to verify the identity provider's certificate.
        proxy: The outbound proxy, when the identity provider is reached through it.
        transport: Replaces the network, for tests.
    """

    def __init__(
        self,
        *,
        kind: Literal["on_behalf_of", "token_exchange"],
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        scope: str | None,
        clock: Clock,
        timeout_seconds: float = 10.0,
        verify: object = True,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._kind = kind
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._clock = clock
        self._client = httpx.AsyncClient(
            verify=verify,  # type: ignore[arg-type]
            timeout=timeout_seconds,
            follow_redirects=False,
            # The environment is read by the configuration layer only.
            trust_env=False,
            proxy=proxy if transport is None else None,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._cache: OrderedDict[tuple[str, str], tuple[float, SecretStr]] = OrderedDict()

    def __repr__(self) -> str:
        return f"OAuthTokenExchanger(kind={self._kind!r}, client_id={self._client_id!r})"

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:
        """Return a token for the caller that only ``audience`` accepts.

        Raises:
            PolicyDenied: If the caller's own token is not at hand, or the
                identity provider refuses the exchange.
            TransientError: If the identity provider cannot be reached.
        """
        service = context.principal.kind is PrincipalKind.SERVICE
        assertion = context.credential.get_secret_value() if context.credential else None
        if assertion is None and not service:
            raise PolicyDenied(
                "the caller's token is not available to exchange", reason_code="credential_missing"
            )
        if service or assertion is None:
            # An application acting for itself has no user to act for: it asks in its own name.
            return await self.service_token(audience)
        subject = hashlib.sha256(assertion.encode()).hexdigest()
        return await self._cached((subject, audience), self._form(assertion, audience))

    async def service_token(self, audience: str) -> SecretStr:
        """Return a token for this service itself that only ``audience`` accepts.

        This is the client credentials grant: the service shows its own
        client ID and secret, and the token names no person.

        Raises:
            PolicyDenied: If the identity provider refuses.
            TransientError: If the identity provider cannot be reached.
        """
        return await self._cached((_OWN_NAME, audience), self._form(None, audience))

    async def _cached(self, key: tuple[str, str], form: Mapping[str, str]) -> SecretStr:
        now = self._clock.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] - now > _EARLY_SECONDS:
            self._cache.move_to_end(key)
            return cached[1]
        token, lifetime = await self._request(form)
        self._cache[key] = (now + lifetime, token)
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHED:
            self._cache.popitem(last=False)
        return token

    def _form(self, assertion: str | None, audience: str) -> dict[str, str]:
        scope = self._scope.replace("{audience}", audience) if self._scope is not None else None
        form: dict[str, str]
        if assertion is None:
            form = {"grant_type": "client_credentials"}
            if self._kind == "token_exchange":
                form["audience"] = audience
        elif self._kind == "on_behalf_of":
            form = {
                "grant_type": _JWT_BEARER,
                "assertion": assertion,
                "requested_token_use": "on_behalf_of",
            }
        else:
            form = {
                "grant_type": _TOKEN_EXCHANGE,
                "subject_token": assertion,
                "subject_token_type": _ACCESS_TOKEN,
                "requested_token_type": _ACCESS_TOKEN,
                "audience": audience,
            }
        if scope is not None:
            form["scope"] = scope
        return form

    async def _request(self, form: Mapping[str, str]) -> tuple[SecretStr, float]:
        secret = self._client_secret.get_secret_value()
        try:
            if self._kind == "on_behalf_of":
                # Entra documents the client credentials as form fields for this grant.
                body = {**form, "client_id": self._client_id, "client_secret": secret}
                response = await self._client.post(self._token_url, data=body)
            else:
                response = await self._client.post(
                    self._token_url, data=dict(form), auth=(self._client_id, secret)
                )
        except httpx.HTTPError as exc:
            raise TransientError(
                f"{_WHAT}: the identity provider could not be reached ({type(exc).__name__})"
            ) from None
        try:
            reply = response.json()
        except ValueError:
            reply = None
        if httpx.codes.is_server_error(response.status_code) or response.status_code in {408, 429}:
            raise TransientError(
                f"{_WHAT}: the identity provider returned HTTP {response.status_code}"
            )
        token = reply.get("access_token") if isinstance(reply, Mapping) else None
        if response.status_code != httpx.codes.OK or not isinstance(token, str) or not token:
            # Only the error code is repeated: the description can hold identifiers.
            code = reply.get("error") if isinstance(reply, Mapping) else None
            plain = (
                code
                if isinstance(code, str) and code.isidentifier() and len(code) <= _MAX_ERROR_CODE
                else "unknown"
            )
            raise PolicyDenied(
                f"{_WHAT}: the identity provider refused ({plain})",
                reason_code="token_exchange_refused",
            )
        expires_in = reply.get("expires_in") if isinstance(reply, Mapping) else None
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, int | float)
            and not isinstance(expires_in, bool)
            and expires_in > 0
            else _DEFAULT_LIFETIME_SECONDS
        )
        return SecretStr(token), lifetime

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

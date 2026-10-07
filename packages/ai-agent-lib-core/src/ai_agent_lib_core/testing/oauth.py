"""A stand-in identity provider, for testing services that verify real tokens.

It signs tokens with its own key, publishes that key, and answers the token
endpoint the way Microsoft Entra ID does for the on-behalf-of flow, the way
RFC 8693 describes for token exchange, and for client credentials. Nothing
leaves the process.

This module needs PyJWT; install the ``jwt`` extra of ``ai-agent-lib-core``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_agent_lib_core.contracts import Clock
from ai_agent_lib_core.testing.fakes import FrozenClock

__all__ = ["FakeIdentityProvider"]

_KEY_BITS = 2048


class FakeIdentityProvider:
    """Issues and exchanges signed tokens in the shape Microsoft Entra ID uses.

    Example::

        idp = FakeIdentityProvider()
        idp.register_client("agent-client-id", "s3cret")
        token = idp.user_token(audience="agent-client-id", subject="u-7", roles=["analyst"])

    Args:
        tenant_id: The tenant every token belongs to.
        authority: The sign-in host the URLs are built on.
        clock: Dates the tokens.

    Attributes:
        requests: Every request the provider received, in order.
        available: Set to ``False`` to make every request fail, as in an outage.
    """

    def __init__(
        self,
        *,
        tenant_id: str = "11111111-1111-1111-1111-111111111111",
        authority: str = "https://login.example.test",
        clock: Clock | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.authority = authority
        self.issuer = f"{authority}/{tenant_id}/v2.0"
        self.jwks_url = f"{authority}/{tenant_id}/discovery/v2.0/keys"
        self.token_url = f"{authority}/{tenant_id}/oauth2/v2.0/token"
        self.requests: list[httpx.Request] = []
        self.available = True
        self._clock: Clock = clock if clock is not None else FrozenClock()
        self._clients: dict[str, str] = {}
        self._roles: dict[tuple[str, str], tuple[str, ...]] = {}
        self._serial = 0
        self._keys: dict[str, rsa.RSAPrivateKey] = {}
        self.rotate_keys()

    # ------------------------------------------------------------------- setup

    def rotate_keys(self, *, keep_old: bool = False) -> str:
        """Start signing with a new key and return its ID."""
        self._serial += 1
        if not keep_old:
            self._keys.clear()
        kid = f"key-{self._serial}"
        self._keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)
        self._current = kid
        return kid

    def register_client(self, client_id: str, client_secret: str) -> None:
        """Register an application that may exchange tokens."""
        self._clients[client_id] = client_secret

    def assign_roles(self, subject: str, audience: str, roles: Iterable[str]) -> None:
        """Set the roles ``subject`` has at ``audience``, as app role assignments do."""
        self._roles[(subject, audience)] = tuple(roles)

    def options(self, audience: str | Iterable[str], **extra: Any) -> dict[str, Any]:
        """Return options for the ``jwt`` identity provider that trust this provider."""
        accepted = audience if isinstance(audience, str) else list(audience)
        return {
            "preset": "entra",
            "tenant_id": self.tenant_id,
            "authority": self.authority,
            "audience": accepted,
            **extra,
        }

    # ------------------------------------------------------------------ tokens

    def sign(self, claims: dict[str, Any], *, kid: str | None = None, **headers: Any) -> str:
        """Sign arbitrary claims, for tests of malformed or hostile tokens."""
        key_id = kid if kid is not None else self._current
        return jwt.encode(
            claims, self._keys[key_id], algorithm="RS256", headers={"kid": key_id, **headers}
        )

    def claims(self, *, audience: str, subject: str, expires_in: int = 3600) -> dict[str, Any]:
        """Return the claims every token of this provider carries."""
        now = int(self._clock.now().timestamp())
        return {
            "iss": self.issuer,
            "aud": audience,
            "tid": self.tenant_id,
            "oid": subject,
            "sub": f"pairwise-{subject}",
            "iat": now,
            "nbf": now,
            "exp": now + expires_in,
            "ver": "2.0",
        }

    def user_token(
        self,
        *,
        audience: str,
        subject: str,
        roles: Iterable[str] = (),
        client_id: str = "chat-ui",
        scopes: str = "access_as_user",
        expires_in: int = 3600,
        **extra: Any,
    ) -> str:
        """Return a token a user obtained for ``audience`` through ``client_id``."""
        claims = self.claims(audience=audience, subject=subject, expires_in=expires_in)
        claims.update({"azp": client_id, "scp": scopes, "roles": list(roles), **extra})
        return self.sign({key: value for key, value in claims.items() if value is not None})

    def app_token(
        self, *, audience: str, client_id: str, roles: Iterable[str] = (), **extra: Any
    ) -> str:
        """Return a token an application obtained for itself, with no user."""
        claims = self.claims(audience=audience, subject=f"sp-{client_id}")
        claims.update({"azp": client_id, "idtyp": "app", "roles": list(roles), **extra})
        return self.sign({key: value for key, value in claims.items() if value is not None})

    # --------------------------------------------------------------- endpoints

    @property
    def transport(self) -> httpx.MockTransport:
        """A transport that routes requests to this provider instead of the network."""
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.available:
            raise httpx.ConnectError("the identity provider is unreachable", request=request)
        url = str(request.url)
        if request.method == "GET" and url == self.jwks_url:
            return httpx.Response(200, json=self.jwks())
        if request.method == "POST" and url == self.token_url:
            return self._token(request)
        return httpx.Response(404, json={"error": "not_found"})

    def jwks(self) -> dict[str, Any]:
        """Return the published signing keys."""
        keys = []
        for kid, key in self._keys.items():
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        client_id, client_secret = form.get("client_id"), form.get("client_secret")
        basic = request.headers.get("authorization", "")
        if basic.lower().startswith("basic "):
            client_id, _, client_secret = base64.b64decode(basic[6:]).decode().partition(":")
        if client_id is None or self._clients.get(client_id) != client_secret:
            return self._refuse("invalid_client", "AADSTS7000215: Invalid client secret provided.")

        grant = form.get("grant_type")
        scope = form.get("scope", "")
        audience = form.get("audience") or scope.removesuffix("/.default")
        if not audience:
            return self._refuse("invalid_scope", "AADSTS70011: The scope is not valid.")
        if grant == "client_credentials":
            token = self.app_token(audience=audience, client_id=client_id)
            return self._issued(token)
        if grant == "urn:ietf:params:oauth:grant-type:jwt-bearer":
            assertion = form.get("assertion")
            if form.get("requested_token_use") != "on_behalf_of":
                return self._refuse("invalid_request", "requested_token_use is missing")
        elif grant == "urn:ietf:params:oauth:grant-type:token-exchange":
            assertion = form.get("subject_token")
        else:
            return self._refuse("unsupported_grant_type", "the grant type is not supported")
        try:
            # The assertion must be a token this application received for itself.
            presented = jwt.decode(
                assertion or "",
                self._keys[jwt.get_unverified_header(assertion or "")["kid"]].public_key(),
                algorithms=["RS256"],
                audience=client_id,
                issuer=self.issuer,
                options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
            )
        except (jwt.PyJWTError, KeyError):
            return self._refuse(
                "invalid_grant", "AADSTS50013: Assertion failed signature or audience validation."
            )
        if presented.get("idtyp") == "app":
            return self._refuse("invalid_grant", "AADSTS50013: app-only tokens cannot be exchanged")
        subject = presented["oid"]
        roles = self._roles.get((subject, audience), tuple(presented.get("roles", ())))
        token = self.user_token(
            audience=audience, subject=subject, roles=roles, client_id=client_id
        )
        return self._issued(token)

    @staticmethod
    def _issued(token: str) -> httpx.Response:
        return httpx.Response(
            200, json={"token_type": "Bearer", "expires_in": 3600, "access_token": token}
        )

    @staticmethod
    def _refuse(error: str, description: str) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": error,
                "error_description": f"{description} Trace ID: 0000aaaa Correlation ID: bbbb1111",
            },
        )

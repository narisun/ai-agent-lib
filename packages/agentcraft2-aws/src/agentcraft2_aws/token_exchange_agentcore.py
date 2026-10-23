"""Tokens for other services, obtained through AgentCore Identity.

On Bedrock AgentCore the runtime, not the agent, holds the client secret of
each downstream service's credential provider. The agent presents the workload
access token the runtime gave it, names a credential provider and the scopes
it needs, and AgentCore Identity returns a token bound to that service. As
with every exchange in the library, the caller's own credential is used only
to obtain that token and is never forwarded.

A person's request is exchanged with the ``USER_FEDERATION`` flow, which
needs the person to have authorised the service once; a service acting for
itself uses ``M2M``.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from botocore import exceptions as aws
from pydantic import Field, SecretStr

from agentcraft2_aws.session import AwsSessionFactory
from agentcraft2_core.contracts import (
    Clock,
    OptionsModel,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
    UpstreamError,
    describe,
)

__all__ = ["AgentCoreExchangeOptions", "AgentCoreTokenExchanger"]

_LOG = logging.getLogger(__name__)
_WHAT = "the agentcore token exchange"
_MAX_CACHED = 1024
_FLOWS: Mapping[PrincipalKind, str] = {
    PrincipalKind.USER: "USER_FEDERATION",
    PrincipalKind.SERVICE: "M2M",
}
_UNAUTHORIZED = "UnauthorizedException"


class AgentCoreExchangeOptions(OptionsModel):
    """How a service on AgentCore obtains tokens for the services it calls.

    Attributes:
        credential_provider: The name of the AgentCore Identity credential
            provider that knows the downstream service's identity provider.
        scopes: The scopes to ask for, where ``{audience}`` stands for the
            audience of the service being called.
        workload_name: This service's workload identity in AgentCore, needed
            to mint a workload token when a request brought none: for the
            service's own token, and for a caller whose token the runtime did
            not pass on.
        return_url: Where the person's browser is sent after they authorise
            this service, for the user federation flow.
        cache_seconds: How long an obtained token is reused for the same
            caller and audience.
    """

    credential_provider: str = Field(pattern=r"^[a-zA-Z0-9\-_]+$", max_length=128)
    scopes: tuple[str, ...]
    workload_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{3,255}$")
    return_url: str | None = Field(default=None, pattern=r"^\w+:/?/?\S+$", max_length=2048)
    cache_seconds: float = Field(default=300.0, gt=0)


class AgentCoreTokenExchanger:
    """Exchanges the runtime's workload token for a token another service accepts.

    Tokens are reused until ``cache_seconds`` pass, so a conversation does not
    cost one round trip to AgentCore Identity per tool call. The cache is keyed
    by a digest of the workload token, or of the caller, never by the token.

    Args:
        options: The credential provider, scopes and workload name.
        sessions: Builds the client and makes its calls.
        clock: Ages the cached tokens.
        client: A ``bedrock-agentcore`` client to use instead of building one.
    """

    def __init__(
        self,
        options: AgentCoreExchangeOptions,
        sessions: AwsSessionFactory,
        clock: Clock,
        *,
        client: Any = None,
    ) -> None:
        self._options = options
        self._sessions = sessions
        self._clock = clock
        self._client = client if client is not None else sessions.client("bedrock-agentcore")
        self._cache: OrderedDict[tuple[str, str, str], tuple[float, SecretStr]] = OrderedDict()

    def __repr__(self) -> str:
        options = self._options
        return (
            f"AgentCoreTokenExchanger(credential_provider={options.credential_provider!r}, "
            f"workload_name={options.workload_name!r})"
        )

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:
        """Return a token for the caller that only ``audience`` accepts.

        A person's request carries the workload token the runtime issued for
        them; when it does not, and the service has a workload name, a token
        is minted for the person. A service acting for itself asks in its own name.

        Raises:
            PolicyDenied: If there is nothing to exchange, if the person has
                not authorised this service for the downstream one, or if
                AgentCore Identity refuses the workload token.
            ConfigurationError: If AWS refused the service's access.
            TransientError: If AgentCore throttled the call or could not be reached.
            UpstreamError: If AgentCore answered with another error or no token.
        """
        principal = context.principal
        if context.credential is not None:
            token = context.credential.get_secret_value()
            key = ("token", _digest(token), audience)
            return await self._cached(key, lambda: self._resource_token(token, principal, audience))
        if principal.kind is PrincipalKind.SERVICE:
            return await self.service_token(audience)
        if self._options.workload_name is None:
            raise PolicyDenied(
                f"{_WHAT}: the caller's workload token is not available to exchange",
                reason_code="credential_missing",
                expected="the workload access token the runtime sets on a request, or a "
                "workload_name in the exchange options to mint one for the caller",
            )
        key = ("subject", _digest(f"{principal.tenant}\x1f{principal.subject}"), audience)

        async def mint() -> SecretStr:
            workload = await self._workload_token(userId=principal.subject)
            return await self._resource_token(workload, principal, audience)

        return await self._cached(key, mint)

    async def service_token(self, audience: str) -> SecretStr:
        """Return a token for this service itself that only ``audience`` accepts.

        The service's own workload token is minted from its workload name and
        exchanged with the machine-to-machine flow. The token names no person.

        Raises:
            PolicyDenied: If the service has no workload name, or AgentCore
                Identity refuses.
            ConfigurationError: If AWS refused the service's access.
            TransientError: If AgentCore throttled the call or could not be reached.
            UpstreamError: If AgentCore answered with another error or no token.
        """
        name = self._options.workload_name
        if name is None:
            raise PolicyDenied(
                f"{_WHAT}: this service has no workload identity to act in its own name",
                reason_code="credential_missing",
                expected="workload_name in the exchange options",
                fix="set workload_name to the service's AgentCore workload identity",
            )

        async def mint() -> SecretStr:
            workload = await self._workload_token()
            return await self._resource_token(workload, None, audience)

        return await self._cached(("service", name, audience), mint)

    async def _cached(
        self, key: tuple[str, str, str], obtain: Callable[[], Awaitable[SecretStr]]
    ) -> SecretStr:
        now = self._clock.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(key)
            return cached[1]
        token = await obtain()
        self._cache[key] = (now + self._options.cache_seconds, token)
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHED:
            self._cache.popitem(last=False)
        return token

    async def _workload_token(self, **user: str) -> str:
        """Mint a workload token for this service, or for the user in ``user``."""
        operation = (
            self._client.get_workload_access_token_for_user_id
            if user
            else self._client.get_workload_access_token
        )
        reply = await self._call(operation, workloadName=self._options.workload_name, **user)
        token = reply.get("workloadAccessToken")
        if not isinstance(token, str) or not token:
            raise _no_token("workloadAccessToken")
        return token

    async def _resource_token(
        self, workload_token: str, principal: Principal | None, audience: str
    ) -> SecretStr:
        """Exchange a workload token for a token bound to ``audience``.

        ``principal`` is the caller the token is for, or ``None`` for the service itself.
        """
        options = self._options
        kind = PrincipalKind.SERVICE if principal is None else PrincipalKind(principal.kind)
        request: dict[str, Any] = {
            "workloadIdentityToken": workload_token,
            "resourceCredentialProviderName": options.credential_provider,
            "scopes": [scope.replace("{audience}", audience) for scope in options.scopes],
            "oauth2Flow": _FLOWS[kind],
        }
        if kind is PrincipalKind.USER and options.return_url is not None:
            request["resourceOauth2ReturnUrl"] = options.return_url
        reply = await self._call(self._client.get_resource_oauth2_token, **request)
        token = reply.get("accessToken")
        if isinstance(token, str) and token:
            return SecretStr(token)
        if reply.get("authorizationUrl") or reply.get("sessionStatus") == "IN_PROGRESS":
            # The authorisation URL is for the person's browser, not for a log or a message.
            _LOG.info(
                "agentcore token exchange needs the user's consent",
                extra={"reason_code": "user_consent_required", "audience": audience},
            )
            raise PolicyDenied(
                f"{_WHAT}: the user has not authorised this service for {audience}",
                reason_code="user_consent_required",
                expected="a token for the service being called",
                actual="a request for the user's consent",
                fix="have the user authorise this service for the credential provider "
                f"{options.credential_provider!r} once, then ask again",
            )
        raise _no_token("accessToken")

    async def _call(self, operation: Callable[..., Any], **arguments: Any) -> Mapping[str, Any]:
        try:
            reply = await self._sessions.invoke(_WHAT, operation, **arguments)
        except (aws.BotoCoreError, aws.ClientError) as exc:
            code = (
                exc.response.get("Error", {}).get("Code")
                if isinstance(exc, aws.ClientError)
                else None
            )
            if code == _UNAUTHORIZED:
                raise PolicyDenied(
                    f"{_WHAT}: AgentCore Identity did not accept the workload token ({code})",
                    reason_code="credential_invalid",
                ) from exc
            # The service's message can name the caller: it stays in the cause.
            raise UpstreamError(
                f"{_WHAT} failed ({code or describe(exc)})",
                code=code,
                fix="check the credential provider name, the workload name and the scopes",
            ) from exc
        if not isinstance(reply, Mapping):
            raise _no_token("accessToken")
        return reply


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _no_token(field: str) -> UpstreamError:
    return UpstreamError(
        f"{_WHAT} returned no token",
        expected=f"a reply with {field!r}",
        actual="a reply without one",
    )

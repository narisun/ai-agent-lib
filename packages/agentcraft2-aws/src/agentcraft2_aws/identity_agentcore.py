"""Identity from the AgentCore runtime's perimeter, for runtimes in SigV4 mode.

A Bedrock AgentCore runtime with IAM authentication checks the caller's AWS
signature before the request reaches the container. No bearer token arrives;
the runtime passes the caller on as an opaque user ID in a header, and may
add the workload access token the agent needs for its own outbound calls.

This adapter trusts that perimeter. It builds the principal from the user
header and the configured tenant, and marks it ``authenticated_by:
agentcore-runtime`` so that policy can tell a perimeter-trusted identity
from one the library verified itself. It is refused in local development,
where there is no perimeter to trust.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator, model_validator

from agentcraft2_aws.token_exchange_agentcore import AgentCoreExchangeOptions
from agentcraft2_core.contracts import (
    OptionsModel,
    PolicyDenied,
    PresentedCredentials,
    Principal,
    PrincipalKind,
    RequestContext,
    SupportsAsyncClose,
    TokenExchanger,
    Verified,
)

__all__ = [
    "AUTHENTICATED_BY",
    "AgentCoreRuntimeIdentity",
    "AgentCoreRuntimeIdentityOptions",
    "ExchangingAgentCoreRuntimeIdentity",
]

AUTHENTICATED_BY = "agentcore-runtime"
"""What the principal's ``authenticated_by`` says: the runtime, not a verified token."""

_USER_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"
# The current runtime's name for the workload access token, then the older one.
_TOKEN_HEADERS = ("x-amz-bedrock-agentcore-identity-wat", "workloadaccesstoken")
_WHAT = "the agentcore_runtime identity"


class AgentCoreRuntimeIdentityOptions(OptionsModel):
    """Options of the ``agentcore_runtime`` identity provider.

    Attributes:
        tenant: The tenant every caller belongs to. The runtime says who the
            caller is, not which tenant they are in.
        roles: The roles every caller is granted. The runtime passes none.
        user_header: The header the runtime sets to the caller's identity.
        workload_token_headers: The headers that may carry the workload
            access token, in the order they are looked for.
        exchange: How tokens for other services are obtained through
            AgentCore Identity. Without it the identity only verifies.
    """

    tenant: str = Field(min_length=1)
    roles: tuple[str, ...] = ()
    user_header: str = _USER_HEADER
    workload_token_headers: tuple[str, ...] = _TOKEN_HEADERS
    exchange: AgentCoreExchangeOptions | None = None

    @field_validator("user_header")
    @classmethod
    def _lower_header(cls, value: str) -> str:
        return _header_name(value)

    @field_validator("workload_token_headers")
    @classmethod
    def _lower_headers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_header_name(name) for name in value)

    @model_validator(mode="after")
    def _principal_can_be_built(self) -> AgentCoreRuntimeIdentityOptions:
        try:
            Principal(
                subject="probe",
                tenant=self.tenant,
                roles=frozenset(self.roles),
                authenticated_by=AUTHENTICATED_BY,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        return self


def _header_name(value: str) -> str:
    name = value.strip().lower()
    if not name or any(ch.isspace() or not ch.isprintable() for ch in name):
        raise ValueError("a header name is one word without spaces or control characters")
    return name


class AgentCoreRuntimeIdentity:
    """The caller as the AgentCore runtime reports them, in the configured tenant.

    Args:
        options: The tenant, roles and header names.
    """

    def __init__(self, options: AgentCoreRuntimeIdentityOptions) -> None:
        self._options = options

    def __repr__(self) -> str:
        return f"AgentCoreRuntimeIdentity(tenant={self._options.tenant!r})"

    @property
    def trusts_perimeter(self) -> bool:
        """True: a request with no bearer token is accepted when the runtime's headers are set."""
        return True

    @property
    def required_headers(self) -> tuple[str, ...]:
        """The headers a request must carry for this identity to accept it."""
        return (self._options.user_header,)

    async def verify(self, credential: str | None) -> Principal:  # noqa: ARG002 - nothing to verify
        """Refuse: a bare bearer token proves nothing to this adapter.

        The caller's identity comes from the runtime's headers, which only
        :meth:`verify_request` sees.

        Raises:
            PolicyDenied: Always, with reason ``perimeter_identity_missing``.
        """
        raise self._missing(actual="a bearer token with no request headers")

    async def verify_request(self, credentials: PresentedCredentials) -> Verified:
        """Return the caller the runtime named, and the workload token it passed on.

        Raises:
            PolicyDenied: If the user header is absent (``perimeter_identity_missing``)
                or is not a usable identifier (``credential_invalid``).
        """
        options = self._options
        user = credentials.header(options.user_header)
        if user is None:
            raise self._missing(actual="a request without it")
        try:
            principal = Principal(
                subject=user,
                tenant=options.tenant,
                roles=frozenset(options.roles),
                authenticated_by=AUTHENTICATED_BY,
                kind=PrincipalKind.USER,
            )
        except (TypeError, ValueError) as exc:
            # The value itself stays out of the message: it is the caller's input.
            raise PolicyDenied(
                f"{_WHAT}: the caller identity the runtime passed is not a usable identifier",
                reason_code="credential_invalid",
                expected="a non-empty identifier without surrounding whitespace or control "
                "characters",
            ) from exc
        token = next(
            (
                value
                for name in options.workload_token_headers
                if (value := credentials.header(name))
            ),
            None,
        )
        return Verified(principal, SecretStr(token) if token is not None else None)

    def _missing(self, *, actual: str) -> PolicyDenied:
        header = self._options.user_header
        return PolicyDenied(
            f"{_WHAT}: the request carries no caller identity from the runtime",
            reason_code="perimeter_identity_missing",
            expected=f"the {header} header, which the runtime sets in IAM mode",
            actual=actual,
            fix="invoke the runtime as the user (InvokeAgentRuntime with a user ID), or give "
            "the runtime a JWT authorizer and select identity=jwt",
        )


class ExchangingAgentCoreRuntimeIdentity(AgentCoreRuntimeIdentity):
    """The runtime's identity for a service that also calls other services for its callers.

    Args:
        options: The tenant, roles and header names.
        exchanger: Obtains the tokens presented to the services this one calls.
    """

    def __init__(self, options: AgentCoreRuntimeIdentityOptions, exchanger: TokenExchanger) -> None:
        super().__init__(options)
        self._exchanger = exchanger

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:
        """Return a token for the caller that only ``audience`` accepts."""
        return await self._exchanger.exchange(context, audience)

    async def service_token(self, audience: str) -> SecretStr:
        """Return a token for this service itself that only ``audience`` accepts."""
        return await self._exchanger.service_token(audience)

    async def aclose(self) -> None:
        """Close the exchanger, when it holds anything to close."""
        if isinstance(self._exchanger, SupportsAsyncClose):
            await self._exchanger.aclose()

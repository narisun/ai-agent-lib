"""Request authentication and refusal auditing, independent of container lifecycle."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    AuditSink,
    Classification,
    Clock,
    IdentityVerifier,
    IdGenerator,
    PolicyDenied,
    PresentedCredentials,
    RegistrySource,
    RequestContext,
    RequestVerifier,
    Telemetry,
    Verified,
)
from ai_agent_lib_core.pipeline import record_refused_sign_in

__all__ = ["RequestAuthenticator", "verify_presented"]


async def verify_presented(
    identity: IdentityVerifier, credential: str | PresentedCredentials | None
) -> Verified:
    """Verify what was presented, through the verifier's widest interface.

    A verifier that reads headers (:class:`RequestVerifier`) sees everything
    that was presented; any other sees the bearer token alone, and the token
    is what is kept for exchange. Every entry point, HTTP or MCP, signs
    callers in through this one function.

    Raises:
        PolicyDenied: If the verifier rejects the credentials.
    """
    presented = (
        credential
        if isinstance(credential, PresentedCredentials)
        else PresentedCredentials(bearer=credential)
    )
    if isinstance(identity, RequestVerifier):
        return await identity.verify_request(presented)
    principal = await identity.verify(presented.bearer)
    return Verified(principal, SecretStr(presented.bearer) if presented.bearer else None)


@dataclass(frozen=True, slots=True)
class RequestAuthenticator:
    """Verify identity, audit sign-in refusals, and create request context.

    Dependencies are borrowed ports owned by the container. This collaborator
    has no lifecycle or framework dependency; ``ServiceContainer.authenticate``
    is the application-facing entry point.
    """

    identity: IdentityVerifier
    registry: RegistrySource
    audit: AuditSink
    telemetry: Telemetry
    clock: Clock
    ids: IdGenerator

    async def verify(self, credential: str | PresentedCredentials | None) -> Verified:
        """Verify what was presented, through the verifier's widest interface.

        A verifier that reads headers (:class:`RequestVerifier`) sees everything
        that was presented; any other sees the bearer token alone, and the
        token is what is kept for exchange.

        Raises:
            PolicyDenied: If the verifier rejects the credentials.
        """
        return await verify_presented(self.identity, credential)

    async def authenticate(
        self,
        credential: str | PresentedCredentials | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
        classification_ceiling: Classification | None = None,
        deadline: datetime | None = None,
    ) -> RequestContext:
        """Create a context with a fresh budget identity after successful verification.

        A verifier's ``PolicyDenied`` is audited before it propagates. Without an
        explicit ceiling, use the registered agent's ceiling or ``internal``.
        The public argument and error contract is documented on
        ``ServiceContainer.authenticate``.
        """
        request_id = request_id if request_id is not None else self.ids.new_id()
        try:
            verified = await self.verify(credential)
        except PolicyDenied as refusal:
            await record_refused_sign_in(
                self.audit,
                self.telemetry,
                self.clock,
                self.ids,
                refusal=refusal,
                application=application,
                request_id=request_id,
                thread_id=thread_id,
            )
            raise
        if classification_ceiling is None:
            entry = self.registry.agents.get(application)
            classification_ceiling = (
                entry.classification_ceiling if entry is not None else Classification.INTERNAL
            )
        return RequestContext(
            principal=verified.principal,
            application=application,
            request_id=request_id,
            thread_id=thread_id,
            classification_ceiling=classification_ceiling,
            deadline=deadline,
            credential=verified.credential,
            # Each request gets its own budget, whatever request ID it was sent.
            invocation_id=self.ids.new_id(),
        )

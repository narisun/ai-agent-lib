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
    RegistrySource,
    RequestContext,
    Telemetry,
)
from ai_agent_lib_core.pipeline import record_refused_sign_in


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

    async def authenticate(
        self,
        credential: str | None,
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
            principal = await self.identity.verify(credential)
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
            principal=principal,
            application=application,
            request_id=request_id,
            thread_id=thread_id,
            classification_ceiling=classification_ceiling,
            deadline=deadline,
            credential=SecretStr(credential) if credential else None,
            # Each request gets its own budget, whatever request ID it was sent.
            invocation_id=self.ids.new_id(),
        )

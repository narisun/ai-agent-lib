"""Authenticated identity and the per-request context.

Entry points create these immutable values from verified identity, not from
prompts, tool results, or graph state. The dataclasses validate shape, not
credentials: application code must use an identity verifier at the boundary.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import SecretStr

from ai_agent_lib_core.contracts._validation import require_identifier
from ai_agent_lib_core.contracts.scope import Scope

__all__ = ["Classification", "Principal", "PrincipalKind", "RequestContext"]


class Classification(enum.IntEnum):
    """Data classification levels, ordered from least to most sensitive."""

    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3


class PrincipalKind(enum.StrEnum):
    """What a principal is."""

    USER = "user"
    """A person who signed in."""

    SERVICE = "service"
    """An application acting for itself, with no person behind the call."""


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified caller.

    Production entry points obtain this value from an ``IdentityVerifier``.
    Tests may construct principals directly to exercise a particular role or
    tenant. Constructing the dataclass does not authenticate its contents.

    Attributes:
        subject: The authenticated person or service.
        tenant: The tenant the subject belongs to.
        roles: The roles granted to the subject.
        authenticated_by: The party that authenticated the subject.
        delegation_chain: The applications acting for the subject, outermost
            first, each named by its own (non-human) identifier. Empty when the
            subject is calling directly.
        kind: Whether the subject is a person or an application acting for itself.
    """

    subject: str
    tenant: str
    roles: frozenset[str] = frozenset()
    authenticated_by: str = "unknown"
    delegation_chain: tuple[str, ...] = ()
    kind: PrincipalKind = PrincipalKind.USER

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", PrincipalKind(self.kind))
        require_identifier("subject", self.subject)
        require_identifier("tenant", self.tenant)
        require_identifier("authenticated_by", self.authenticated_by)
        # Accept any iterable for convenience, but always store immutable copies.
        object.__setattr__(self, "roles", frozenset(self.roles))
        object.__setattr__(self, "delegation_chain", tuple(self.delegation_chain))
        for role in self.roles:
            require_identifier("role", role)
        for actor in self.delegation_chain:
            require_identifier("delegation_chain entry", actor)

    @property
    def actor(self) -> str | None:
        """The application that presented this request for the subject, if any."""
        return self.delegation_chain[-1] if self.delegation_chain else None

    def has_role(self, role: str) -> bool:
        """Return whether the principal holds ``role``."""
        return role in self.roles


@dataclass(frozen=True, slots=True, weakref_slot=True)
class RequestContext:
    """Everything the library needs to know about one request.

    The context travels as runtime context, outside graph state. This keeps
    authority and credentials out of checkpoints and model-visible messages.

    Attributes:
        principal: The verified caller.
        application: The agent or MCP server handling the request.
        request_id: Correlates audit records, telemetry and logs.
        thread_id: The conversation or workflow the request belongs to.
        classification_ceiling: The most sensitive data this request may touch.
        deadline: When the request must be finished, as an aware datetime.
        credential: The credential the caller presented. It is kept only so
            that it can be exchanged for a token bound to another service. It
            is excluded from this dataclass's repr and equality. Library
            integrations exchange it rather than forwarding it; application
            code must also keep it out of logs and serialized state.
        invocation_id: Identifies this one invocation inside the process, so
            its budget is its own. ``services.authenticate`` makes a new one
            for each request, independently of caller-supplied correlation IDs.
            Contexts built without one use ``request_id`` for budget accounting.
    """

    principal: Principal
    application: str
    request_id: str
    thread_id: str
    classification_ceiling: Classification = Classification.INTERNAL
    deadline: datetime | None = None
    credential: SecretStr | None = field(default=None, repr=False, compare=False)
    invocation_id: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.credential is not None and not isinstance(self.credential, SecretStr):
            raise TypeError("credential must be a SecretStr")
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be a Principal")
        require_identifier("application", self.application)
        require_identifier("request_id", self.request_id)
        require_identifier("thread_id", self.thread_id)
        if self.invocation_id is not None:
            require_identifier("invocation_id", self.invocation_id)
        object.__setattr__(
            self, "classification_ceiling", Classification(self.classification_ceiling)
        )
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")

    @property
    def budget_key(self) -> str:
        """What the budget of this request is counted under.

        The caller's tenant and subject and this application come first, so a
        request ID one caller chose can never spend another caller's budget.
        Then the invocation, when there is one, else the request ID.
        """
        principal = self.principal
        invocation = self.invocation_id or self.request_id
        return "\x1f".join((principal.tenant, principal.subject, self.application, invocation))

    @property
    def scope(self) -> Scope:
        """The storage scope for this request."""
        return Scope(
            tenant=self.principal.tenant,
            subject=self.principal.subject,
            application=self.application,
        )

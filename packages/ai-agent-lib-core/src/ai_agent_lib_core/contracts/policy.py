"""Policy decisions: one port answers every authorization question.

Anything other than a well-formed allow is a deny. A decision point never
raises to say no; it returns a :class:`Decision`, and the place that asked
enforces it.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from ai_agent_lib_core.contracts._validation import require_identifier
from ai_agent_lib_core.contracts.audit import AuditValue
from ai_agent_lib_core.contracts.data import Obligations
from ai_agent_lib_core.contracts.identity import Classification, Principal

__all__ = [
    "DECISION_SCHEMA",
    "Decision",
    "PolicyAction",
    "PolicyDecisionPoint",
    "PolicyRequest",
    "PolicyResource",
]

DECISION_SCHEMA = "agentlib.decision/v1"
"""The schema identifier of the decision input."""


class PolicyAction(enum.StrEnum):
    """What a caller is asking to do."""

    TOOL_CALL = "tool.call"
    DATA_QUERY = "data.query"
    MODEL_ROUTE = "model.route"
    AGENT_CALL = "agent.call"
    SKILL_ACTIVATE = "skill.activate"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"


@dataclass(frozen=True, slots=True)
class PolicyResource:
    """What the action is on.

    Attributes:
        kind: The kind of thing, for example ``tool``, ``query`` or ``model``.
        name: Its name.
        classification: How sensitive it is, when known.
        attributes: Further scalar facts a policy may use, such as the MCP
            server of a tool.
    """

    kind: str
    name: str
    classification: Classification | None = None
    attributes: Mapping[str, AuditValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier("resource kind", self.kind)
        require_identifier("resource name", self.name)
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    """One authorization question.

    Attributes:
        principal: The verified caller.
        action: What the caller wants to do.
        resource: What it wants to do it to.
        application: The agent or MCP server asking.
        environment: Where the process is running.
        attributes: Further scalar context, such as the model provider.
    """

    principal: Principal
    action: PolicyAction
    resource: PolicyResource
    application: str
    environment: str
    attributes: Mapping[str, AuditValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", PolicyAction(self.action))
        require_identifier("application", self.application)
        require_identifier("environment", self.environment)
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def to_input(self) -> dict[str, object]:
        """Return the versioned decision input a policy is written against."""
        resource: dict[str, object] = {
            **self.resource.attributes,
            "kind": self.resource.kind,
            "name": self.resource.name,
        }
        if self.resource.classification is not None:
            resource["classification"] = self.resource.classification.name.lower()
        return {
            "schema": DECISION_SCHEMA,
            "principal": {
                "subject": self.principal.subject,
                "tenant": self.principal.tenant,
                "roles": sorted(self.principal.roles),
                "kind": self.principal.kind.value,
                # The applications acting for the subject, outermost first.
                "actors": list(self.principal.delegation_chain),
            },
            "action": self.action.value,
            "resource": resource,
            "context": {
                **self.attributes,
                "application": self.application,
                "environment": self.environment,
            },
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """The answer to one authorization question.

    Attributes:
        allow: Whether the action may go ahead.
        reason_code: Why, as a short stable code.
        decision_id: Identifies this decision in the policy system's own log.
        obligations: Conditions attached to an allow. Always empty on a deny.
        bundle_revision: The version of the policy that decided, when known.
        cached: Whether the answer was reused from an earlier identical question.
        explanation: Why a deny was given, for the developer who reads it: for
            example which rule came closest and what it needs. It names rules,
            actions, resources and roles, never who the caller is. ``None``
            when the policy system does not say.
    """

    allow: bool
    reason_code: str
    decision_id: str
    obligations: Obligations = field(default_factory=Obligations)
    bundle_revision: str | None = None
    cached: bool = False
    explanation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allow, bool):
            raise TypeError("allow must be a bool")
        require_identifier("reason_code", self.reason_code)
        require_identifier("decision_id", self.decision_id)
        if not self.allow and not self.obligations.empty:
            raise ValueError("a deny carries no obligations")

    @classmethod
    def allowed(
        cls,
        decision_id: str,
        *,
        reason_code: str = "allowed",
        obligations: Obligations | None = None,
        bundle_revision: str | None = None,
    ) -> Decision:
        """Return an allow."""
        return cls(
            allow=True,
            reason_code=reason_code,
            decision_id=decision_id,
            obligations=obligations if obligations is not None else Obligations(),
            bundle_revision=bundle_revision,
        )

    @classmethod
    def denied(
        cls,
        decision_id: str,
        reason_code: str = "denied",
        *,
        bundle_revision: str | None = None,
        explanation: str | None = None,
    ) -> Decision:
        """Return a deny."""
        return cls(
            allow=False,
            reason_code=reason_code,
            decision_id=decision_id,
            bundle_revision=bundle_revision,
            explanation=explanation,
        )


class PolicyDecisionPoint(Protocol):
    """Decides whether a caller may do something.

    An implementation fails closed: if it cannot reach its policy, or the
    answer is not a well-formed allow, it returns a deny.
    """

    async def decide(self, request: PolicyRequest) -> Decision:
        """Return the decision for ``request``. Never raises to say no."""
        ...

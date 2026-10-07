"""The policy stage: every call is put to the policy decision point."""

from __future__ import annotations

from typing import Generic, TypeVar

from ai_agent_lib_core.contracts import (
    AuditValue,
    Classification,
    Clock,
    Decision,
    PolicyAction,
    PolicyDecisionPoint,
    PolicyDenied,
    PolicyRequest,
    PolicyResource,
    RequestContext,
)
from ai_agent_lib_core.pipeline.calls import Evidence, ModelCall, ToolCall
from ai_agent_lib_core.pipeline.identity import require_identified
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["ModelPolicyInterceptor", "ToolPolicyInterceptor", "enforce_decision"]

ResponseT = TypeVar("ResponseT")


def enforce_decision(decision: Decision, evidence: Evidence | None = None) -> Decision:
    """Record a decision as evidence and raise unless it is an allow.

    Raises:
        PolicyDenied: If the decision is a deny.
    """
    if evidence is not None:
        evidence.add(
            policy_decision_id=decision.decision_id,
            policy_reason_code=decision.reason_code,
            policy_bundle_revision=decision.bundle_revision,
            policy_cached=decision.cached,
        )
    if not decision.allow:
        raise PolicyDenied("the policy does not allow this", reason_code=decision.reason_code)
    return decision


def _refuse_obligations(decision: Decision, what: str) -> None:
    """Fail closed on an obligation this kind of call has no way to enforce."""
    if decision.obligations.require_approval:
        raise PolicyDenied(
            f"the policy requires a person to approve this {what}, and no approval step "
            "is available",
            reason_code="approval_unavailable",
        )
    if not decision.obligations.empty:
        raise PolicyDenied(
            f"the policy attached data conditions to a {what}, where they cannot be enforced",
            reason_code="obligation_unenforceable",
        )


def _tool_resource_name(request: ToolCall) -> str:
    """Name an MCP tool by its server, so two servers cannot share a grant by accident."""
    return f"{request.server}/{request.tool}" if request.server is not None else request.tool


def _classification(name: object) -> Classification | None:
    if isinstance(name, str) and name.upper() in Classification.__members__:
        return Classification[name.upper()]
    return None


class ToolPolicyInterceptor(Generic[ResponseT]):
    """Asks the policy whether this caller may call this tool.

    The stage first requires an identified caller whose deadline has not
    passed, since a policy question without a caller has no answer.

    Args:
        policy: The policy decision point.
        clock: The clock port.
        environment: Where the process is running.
    """

    def __init__(self, policy: PolicyDecisionPoint, clock: Clock, environment: str) -> None:
        self._policy = policy
        self._clock = clock
        self._environment = environment

    async def __call__(
        self, request: ToolCall, call_next: Handler[ToolCall, ResponseT]
    ) -> ResponseT:
        """Continue only if the policy allows the call."""
        context = require_identified(request.context, self._clock)
        facts = request.evidence.facts()
        attributes: dict[str, AuditValue] = {"read_only": request.read_only}
        if request.server is not None:
            attributes["server"] = request.server
        decision = enforce_decision(
            await self._policy.decide(
                PolicyRequest(
                    principal=context.principal,
                    action=PolicyAction.TOOL_CALL,
                    resource=PolicyResource(
                        kind="tool",
                        name=_tool_resource_name(request),
                        classification=_classification(facts.get("tool_classification")),
                        attributes=attributes,
                    ),
                    application=context.application,
                    environment=self._environment,
                )
            ),
            request.evidence,
        )
        _refuse_obligations(decision, "tool call")
        return await call_next(request)


class ModelPolicyInterceptor(Generic[ResponseT]):
    """Asks the policy whether this caller may use this model.

    Args:
        policy: The policy decision point.
        clock: The clock port.
        environment: Where the process is running.
    """

    def __init__(self, policy: PolicyDecisionPoint, clock: Clock, environment: str) -> None:
        self._policy = policy
        self._clock = clock
        self._environment = environment

    async def __call__(
        self, request: ModelCall, call_next: Handler[ModelCall, ResponseT]
    ) -> ResponseT:
        """Continue only if the policy allows the call."""
        context: RequestContext = require_identified(request.context, self._clock)
        decision = enforce_decision(
            await self._policy.decide(
                PolicyRequest(
                    principal=context.principal,
                    action=PolicyAction.MODEL_ROUTE,
                    resource=PolicyResource(
                        kind="model",
                        name=request.alias,
                        attributes={"model_id": request.model_id},
                    ),
                    application=context.application,
                    environment=self._environment,
                    attributes={"model_provider": request.provider},
                )
            ),
            request.evidence,
        )
        _refuse_obligations(decision, "model call")
        return await call_next(request)

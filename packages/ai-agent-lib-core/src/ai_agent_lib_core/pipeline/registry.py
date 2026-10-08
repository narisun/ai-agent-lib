"""The registry stage: a tool that is not registered cannot be called."""

from __future__ import annotations

import enum
import re
from dataclasses import replace
from typing import Generic, TypeVar

from ai_agent_lib_core.contracts import (
    AgentEntry,
    AgentRegistry,
    Classification,
    EntryStatus,
    PolicyDenied,
    Principal,
    RegistrySource,
    RequestContext,
    ServerEntry,
)
from ai_agent_lib_core.pipeline.calls import ToolCall
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["AgentCheck", "RegistryInterceptor", "allowed_agent", "name_actors"]

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _shown(name: str) -> str:
    """A tool name a caller sent, quoted, unless it is not shaped like one and may be content."""
    return repr(name) if _TOOL_NAME.match(name) else "with a malformed name"


ResponseT = TypeVar("ResponseT")


class AgentCheck(enum.Enum):
    """Which agent the registry stage holds to the agent registry."""

    SELF = "self"
    """This application is the agent. Used by an agent calling an MCP server."""

    CALLER = "caller"
    """The agent that presented the request, when the caller's token names one.

    Used by an MCP server in local development, where a developer may also
    call a tool directly.
    """

    CALLER_REQUIRED = "caller_required"
    """The agent that presented the request, which the token must name.

    Used by a deployed MCP server: a call that no registered agent stands
    behind is refused.
    """


def name_actors(principal: Principal, agents: AgentRegistry) -> Principal:
    """Return ``principal`` with each registered actor named by its registry ID.

    A token names an agent by its client ID at the identity provider. Policy,
    audit and the registry name it by its registry ID. Actors that are not
    registered keep the identifier the token gave them.
    """
    named = tuple(
        entry.id if (entry := agents.resolve(actor)) is not None else actor
        for actor in principal.delegation_chain
    )
    return (
        principal
        if named == principal.delegation_chain
        else replace(principal, delegation_chain=named)
    )


def allowed_agent(agent: AgentEntry | None, name: str, server: ServerEntry) -> AgentEntry:
    """Return ``agent`` if it is registered, enabled and listed for ``server``.

    Args:
        agent: The registry entry found for the agent, if any.
        name: How the caller named the agent, for the refusal.
        server: The MCP server the agent wants to reach.

    Raises:
        PolicyDenied: If the agent is not registered, is disabled, or is not
            listed for the server.
    """
    if agent is None:
        raise PolicyDenied(
            f"agent {name!r} is not registered",
            reason_code="agent_unregistered",
            fix=(
                f"add {name!r} to the agent registry (agentlib new agent does this), or "
                "check that the caller's token names the agent by its registered ID or client ID"
            ),
        )
    if agent.status is EntryStatus.DISABLED:
        raise PolicyDenied(
            f"agent {name!r} is disabled",
            reason_code="agent_disabled",
            fix="set its status to active in the agent registry, if it should run again",
        )
    if server.id not in agent.mcp_servers:
        listed = ", ".join(repr(s) for s in agent.mcp_servers) or "none"
        raise PolicyDenied(
            f"agent {name!r} is not listed for MCP server {server.id!r}",
            reason_code="server_not_allowed",
            expected=f"{server.id!r} among the agent's mcp_servers",
            actual=f"its mcp_servers are: {listed}",
            fix=f"run 'agentlib link {name} {server.id}', or add it to the agent registry",
        )
    return agent


class RegistryInterceptor(Generic[ResponseT]):
    """Checks a call to an MCP tool against the registries.

    The stage only ever narrows. It refuses a server or a tool that is not
    registered, an agent that is not registered or is disabled, a server the
    agent is not listed for, and a tool more sensitive than the request or the
    agent may handle. Passing it grants nothing: the policy stage still decides.

    A tool that is a function in the application's own code has no server and
    is not a registry matter, so it passes straight through.

    Args:
        registry: The agent and tool registries.
        agent_check: Which agent must be registered and listed for the server.
    """

    def __init__(
        self, registry: RegistrySource, *, agent_check: AgentCheck = AgentCheck.SELF
    ) -> None:
        self._registry = registry
        self._agent_check = agent_check

    async def __call__(
        self, request: ToolCall, call_next: Handler[ToolCall, ResponseT]
    ) -> ResponseT:
        """Continue only for a registered tool this caller may reach."""
        if request.server is None:
            return await call_next(request)
        context = request.context
        if not isinstance(context, RequestContext):
            raise PolicyDenied(
                "the call has no request context, so the caller is unknown",
                reason_code="identity_missing",
            )
        tools = self._registry.tools
        request.evidence.add(tool_registry_revision=tools.revision)
        server = tools.get(request.server)
        if server is None:
            raise PolicyDenied(
                f"MCP server {request.server!r} is not registered",
                reason_code="server_unregistered",
            )
        tool = server.tool(request.tool)
        if tool is None:
            registered = ", ".join(repr(t.name) for t in server.tools) or "none"
            raise PolicyDenied(
                f"tool {_shown(request.tool)} is not registered for MCP server {request.server!r}",
                reason_code="tool_unregistered",
                expected=f"one of the server's registered tools: {registered}",
                fix=(
                    f"add the tool under server {request.server!r} in the tool registry, then "
                    f"pin it with 'agentlib registry pin {request.server}'"
                ),
            )

        agents = self._registry.agents
        request.evidence.add(agent_registry_revision=agents.revision)
        ceiling = context.classification_ceiling
        if self._agent_check is AgentCheck.SELF:
            agent = allowed_agent(agents.get(context.application), context.application, server)
            ceiling = min(ceiling, agent.classification_ceiling)
        else:
            # From here on the agents acting for the caller go by their registry IDs.
            context = replace(context, principal=name_actors(context.principal, agents))
            ceiling = self._caller_ceiling(context.principal, agents, server, ceiling, request)
        if tool.classification > ceiling:
            raise PolicyDenied(
                f"tool {request.tool!r} returns {tool.classification.name.lower()} data, which "
                "is above what this request may handle",
                reason_code="classification_exceeded",
            )
        request.evidence.add(
            tool_version=tool.version,
            tool_classification=tool.classification.name.lower(),
            read_only=tool.read_only,
        )
        # What the tool does next (a query, a nested call) runs under the lower of the
        # request's and the agent's ceilings, not the one the request came in with.
        context = replace(context, classification_ceiling=ceiling)
        # The registry, not the caller, says whether a tool only reads.
        return await call_next(replace(request, context=context, read_only=tool.read_only))

    def _caller_ceiling(
        self,
        principal: Principal,
        agents: AgentRegistry,
        server: ServerEntry,
        ceiling: Classification,
        request: ToolCall,
    ) -> Classification:
        actor = principal.actor
        if actor is None:
            if self._agent_check is AgentCheck.CALLER_REQUIRED:
                raise PolicyDenied(
                    "the caller's token does not name the agent that presented the request",
                    reason_code="agent_unidentified",
                )
            return ceiling
        agent = allowed_agent(agents.get(actor), actor, server)
        request.evidence.add(calling_agent=agent.id)
        return min(ceiling, agent.classification_ceiling)

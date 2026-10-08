"""What a new service needs in the files every service shares.

An agent or an MCP server is of no use until it is registered, a rule allows
what it does, and a sample request shows that the rule decides as intended.
The functions here write those three for each kind of service, from the
answers it was generated from.
"""

from __future__ import annotations

from typing import Any

from ai_agent_lib_cli.dataplan import DataPlan, PlannedQuery
from ai_agent_lib_cli.envfiles import DEV_ROLE
from ai_agent_lib_cli.workspace import AgentAnswers, McpAnswers
from ai_agent_lib_core.contracts import AgentEntry, Classification, ServerEntry, ToolEntry

__all__ = [
    "ANALYST_ROWS",
    "VERSION",
    "agent_entry",
    "agent_rules",
    "agent_samples",
    "analyst_rule_id",
    "link_rule",
    "link_sample",
    "rules_title",
    "server_entry",
    "server_offers",
    "server_rules",
    "server_samples",
]

VERSION = "0.1.0"
"""The version a new service and its tools are registered with."""

ANALYST_ROWS = 50
"""The most rows an analyst is shown by a generated rule."""

_CEILING = Classification.CONFIDENTIAL
_CEILING_NAME = _CEILING.name.lower()
_MAX_RULE_ID = 63


def agent_rules(agent: str) -> list[dict[str, Any]]:
    """Return the rules a new agent needs: its models and its own tool."""
    return [
        {
            "id": f"{agent}-uses-its-models",
            "actions": ["model.route"],
            "applications": [agent],
            "resources": ["default"],
        },
        {
            "id": f"{agent}-calls-its-own-tools",
            "actions": ["tool.call"],
            "applications": [agent],
            "resources": ["greet"],
        },
    ]


def link_rule(agent: str, server_id: str) -> dict[str, Any]:
    """Return the rule that lets an agent call every tool of one MCP server."""
    return {
        "id": f"{agent}-calls-{server_id}",
        "actions": ["tool.call"],
        "applications": [agent],
        "resources": [f"{server_id}/*"],
        "max_classification": _CEILING_NAME,
    }


def analyst_rule_id(server_id: str, plan: DataPlan) -> str:
    """Return the ID of the rule that says what an analyst sees of the data."""
    masked = plan.masked
    if not masked:
        return f"{server_id}-analysts-read-the-data"
    named = f"{server_id}-analysts-see-no-{masked[0].replace('_', '-')}"
    if len(masked) == 1 and len(named) <= _MAX_RULE_ID:
        return named
    return f"{server_id}-analysts-see-masked-columns"


def server_rules(server: McpAnswers) -> list[dict[str, Any]]:
    """Return the rules a new MCP server needs.

    Analysts and managers may call its tools. Managers see every column of
    its data; analysts see the masked columns masked, and fewer rows.
    """
    plan = server.plan
    queries = {
        "actions": ["data.query"],
        "applications": [server.name],
        "resources": [f"{plan.source}.*"],
        "max_classification": _CEILING_NAME,
    }
    limits: dict[str, Any] = {"mask_columns": list(plan.masked)} if plan.masked else {}
    return [
        {
            "id": f"{server.server_id}-staff-call-the-tools",
            "actions": ["tool.call"],
            "applications": [server.name],
            "roles": [DEV_ROLE, "manager"],
            "resources": [f"{server.server_id}/*"],
            "max_classification": _CEILING_NAME,
        },
        {"id": f"{server.server_id}-managers-see-everything", "roles": ["manager"], **queries},
        {
            "id": analyst_rule_id(server.server_id, plan),
            "roles": [DEV_ROLE],
            **queries,
            "obligations": {**limits, "max_rows": ANALYST_ROWS},
        },
    ]


def rules_title(server: McpAnswers) -> str:
    """Return the comment written above a new server's rules."""
    masked = server.plan.masked
    seen = (
        f"Analysts see the rows but not {_listed(masked)}; managers see every column."
        if masked
        else "Analysts and managers see every column; analysts see fewer rows."
    )
    if server.plan.is_sample:
        seen = "Analysts see people but not their email; managers see every column."
    return f"{server.name}: who may call its tools, and what each role sees of the data.\n{seen}"


def _listed(names: tuple[str, ...]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def agent_samples(agent: str) -> list[dict[str, Any]]:
    """Return sample requests that show a new agent's rules deciding as intended."""
    common = {"application": agent}
    return [
        {
            "name": f"{agent} uses its default model",
            "action": "model.route",
            "resource": "default",
            "expect": "allow",
            "reason": f"{agent}-uses-its-models",
            **common,
        },
        {
            "name": f"{agent} calls its own tool",
            "action": "tool.call",
            "resource": "greet",
            "expect": "allow",
            "reason": f"{agent}-calls-its-own-tools",
            **common,
        },
        {
            "name": f"{agent} may not call a tool no rule names",
            "action": "tool.call",
            "resource": "delete_everything",
            "expect": "deny",
            "reason": "no_matching_rule",
            **common,
        },
    ]


def link_sample(agent: str, server: McpAnswers) -> dict[str, Any]:
    """Return a sample request for an agent calling the first tool of a server."""
    sid, query = server.server_id, server.plan.queries[0]
    return {
        "name": f"{agent} calls a tool of {sid}",
        "action": "tool.call",
        "application": agent,
        "resource": f"{sid}/{sid}.{query.name}",
        "classification": query.classification,
        "expect": "allow",
        "reason": f"{agent}-calls-{sid}",
    }


def server_samples(server: McpAnswers) -> list[dict[str, Any]]:
    """Return sample requests that show a new server's rules deciding as intended."""
    sid, plan = server.server_id, server.plan
    first = plan.queries[0]
    query = {
        "action": "data.query",
        "application": server.name,
        "resource": f"{plan.source}.{first.name}",
        "classification": first.classification,
        "expect": "allow",
    }
    call = {
        "action": "tool.call",
        "application": server.name,
        "resource": f"{sid}/{sid}.{first.name}",
        "classification": first.classification,
    }
    masked = plan.masked
    limited = f"without {masked[0]}" if len(masked) == 1 else "with columns masked"
    return [
        {
            "name": f"an analyst calls a tool of {sid}",
            "roles": [DEV_ROLE],
            "expect": "allow",
            "reason": f"{sid}-staff-call-the-tools",
            **call,
        },
        {
            "name": f"anyone else is denied at {sid}",
            "roles": ["intern"],
            "expect": "deny",
            "reason": "no_matching_rule",
            **call,
        },
        {
            "name": f"a manager sees every column of {sid}",
            "roles": ["manager"],
            "reason": f"{sid}-managers-see-everything",
            **query,
        },
        {
            "name": f"an analyst sees {sid} {limited}" if masked else f"an analyst reads {sid}",
            "roles": [DEV_ROLE],
            "reason": analyst_rule_id(sid, plan),
            **query,
        },
    ]


def _tool_entry(server_id: str, query: PlannedQuery) -> ToolEntry:
    return ToolEntry(
        name=f"{server_id}.{query.name}",
        version=VERSION,
        classification=Classification[query.classification.upper()],
        read_only=True,
        description=query.description,
    )


_GREET = "greet"


def server_entry(server: McpAnswers, owner: str) -> ServerEntry:
    """Return the registry entry of a new MCP server, with one tool for each query."""
    plan = server.plan
    plain = (
        (
            ToolEntry(
                name=f"{server.server_id}.{_GREET}",
                version=VERSION,
                classification=Classification.INTERNAL,
                read_only=True,
                description="A greeting for a person.",
            ),
        )
        if plan.is_sample
        else ()
    )
    return ServerEntry(
        id=server.server_id,
        owner=owner,
        url=f"http://127.0.0.1:{server.port}/mcp",
        audience=server.name,
        tools=(*plain, *(_tool_entry(server.server_id, query) for query in plan.queries)),
    )


def server_offers(plan: DataPlan) -> str:
    """Return the first thing the server tells a model about itself."""
    if plan.is_sample:
        return "A greeting, and read-only tools over the people directory."
    return f"Read-only tools over {plan.source.replace('_', ' ')}."


def agent_entry(agent: AgentAnswers, owner: str) -> AgentEntry:
    """Return the registry entry of a new agent."""
    return AgentEntry(
        id=agent.name,
        owner=owner,
        version=VERSION,
        description=agent.description,
        mcp_servers=agent.mcp_servers,
        model_aliases=("default",),
        classification_ceiling=_CEILING,
    )

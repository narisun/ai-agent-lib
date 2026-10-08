"""The files every service of a workspace shares: the two registries and the rules.

The registries are data the tool maintains: an entry is added, linked or
pinned by rewriting the file, so comments in them are not kept. The rules file
is the developer's: the tool only ever appends rules to it, and never changes
or removes one that is there.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.yaml_lists import extended, yaml_flow, yaml_scalar
from ai_agent_lib_core.adapters.policy_documents import parse_rules_document
from ai_agent_lib_core.adapters.registry_documents import (
    agents_document,
    parse_agents_document,
    parse_tools_document,
    tools_document,
)
from ai_agent_lib_core.contracts import AgentEntry, AgentLibError, ServerEntry, describe

__all__ = [
    "AGENTS_FILE",
    "RULES_FILE",
    "TOOLS_FILE",
    "agents_text",
    "pinned",
    "read_agents",
    "read_servers",
    "rules_text",
    "tools_text",
]

AGENTS_FILE = PurePosixPath("registry/agents.yaml")
TOOLS_FILE = PurePosixPath("registry/mcp-tools.yaml")
RULES_FILE = PurePosixPath("policies/agentlib/rules/data.yaml")

_MAINTAINED = (
    "# agentlib rewrites this file when it adds, links or pins a service; comments are not kept.\n"
)
_AGENTS_HEADER = (
    "# The agent registry: which agents exist and which MCP servers each may call.\n" + _MAINTAINED
)
_TOOLS_HEADER = (
    "# The MCP tool registry: which servers and tools exist. An agent can only use a tool\n"
    "# that is listed here, and only if the tool's live input schema still matches its pin.\n"
    "# After changing a tool's arguments, pin it again: agentlib registry pin <server>\n"
    + _MAINTAINED
)
_RULES_HEADER = """\
# What the services of this workspace allow. The first rule that matches a
# request allows it; a request that no rule matches is denied.
#
# One document, two engines. In local development the in-process `rules`
# provider reads this file. A deployed service asks OPA, which loads the
# `policies` folder as a bundle (this file becomes `data.agentlib.rules`).
#
# This file is yours. agentlib appends the rules a new service needs and never
# changes or removes a rule that is here.
schema: agentlib.rules/v1
rules: []
"""


def _dump(document: object) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


def _load(path: Path, what: str) -> object | None:
    if not path.is_file():
        return None
    try:
        document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CliError(f"{what} ({path}) cannot be read ({describe(exc)})") from exc
    return document


def read_agents(root: Path) -> tuple[AgentEntry, ...]:
    """Return the agents registered in the workspace at ``root``."""
    raw = _load(root.joinpath(*AGENTS_FILE.parts), "the agent registry")
    try:
        return parse_agents_document(raw) if raw is not None else ()
    except AgentLibError as exc:
        raise CliError(str(exc)) from None


def read_servers(root: Path) -> tuple[ServerEntry, ...]:
    """Return the MCP servers registered in the workspace at ``root``."""
    raw = _load(root.joinpath(*TOOLS_FILE.parts), "the tool registry")
    try:
        return parse_tools_document(raw) if raw is not None else ()
    except AgentLibError as exc:
        raise CliError(str(exc)) from None


def agents_text(agents: Sequence[AgentEntry]) -> str:
    """Return the agent registry file that holds ``agents``, ordered by ID."""
    ordered = sorted(agents, key=lambda agent: agent.id)
    return _AGENTS_HEADER + _dump(agents_document(ordered))


def tools_text(servers: Sequence[ServerEntry]) -> str:
    """Return the tool registry file that holds ``servers``, ordered by ID."""
    ordered = sorted(servers, key=lambda server: server.id)
    return _TOOLS_HEADER + _dump(tools_document(ordered))


def pinned(server: ServerEntry, pins: Mapping[str, str]) -> ServerEntry:
    """Return ``server`` with the schema pin of each tool that ``pins`` names."""
    return replace(
        server,
        tools=tuple(
            replace(tool, schema_sha256=pins[tool.name]) if tool.name in pins else tool
            for tool in server.tools
        ),
    )


def _rule_ids(text: str, what: str) -> list[str]:
    try:
        raw = yaml.safe_load(text)
        return [rule.id for rule in parse_rules_document(raw, what=what)]
    except (yaml.YAMLError, AgentLibError) as exc:
        raise CliError(f"{what} is not a valid rules document: {exc}") from None


_LIST_KEYS = ("actions", "applications", "roles", "agents", "kinds", "resources")


def _rule_block(rule: Mapping[str, Any]) -> str:
    """Write one rule the way a person writes it: one line per condition."""
    lines = [f"  - id: {yaml_scalar(rule['id'])}"]
    lines += [f"    {key}: {yaml_flow(rule[key])}" for key in _LIST_KEYS if key in rule]
    if "max_classification" in rule:
        lines.append(f"    max_classification: {yaml_scalar(rule['max_classification'])}")
    if rule.get("obligations"):
        lines.append("    obligations:")
        for key, value in rule["obligations"].items():
            written = yaml_flow(value) if isinstance(value, list | tuple) else yaml_scalar(value)
            lines.append(f"      {key}: {written}")
    return "\n".join(lines) + "\n"


def rules_text(existing: str | None, rules: Sequence[Mapping[str, Any]], *, title: str = "") -> str:
    """Return the rules file with ``rules`` appended, leaving what is there untouched.

    A rule whose ID is already in the file is not added again, so the
    developer's version of it stands. ``title`` is written as a comment above
    the rules that are added.

    Raises:
        CliError: If the file is not a rules document, or its rule list is not
            the last thing in it, so that nothing can be appended safely.
    """
    return extended(
        existing if existing is not None else _RULES_HEADER,
        key="rules",
        additions={str(rule["id"]): _rule_block(rule) for rule in rules},
        ids=lambda text: _rule_ids(text, f"the rules file ({RULES_FILE})"),
        what=f"the rules file ({RULES_FILE})",
        title=title,
    )

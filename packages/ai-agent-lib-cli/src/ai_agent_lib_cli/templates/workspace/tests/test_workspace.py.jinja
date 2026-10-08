"""The files every service shares are valid and agree with each other."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core.adapters import (
    FileRegistryOptions,
    FileRegistrySource,
    RulesPolicyDecisionPoint,
    RulesPolicyOptions,
)
from ai_agent_lib_core.testing import SequentialIds

ROOT = Path(__file__).resolve().parents[1]


def test_the_registries_load_and_every_agent_names_servers_that_exist() -> None:
    # Loading checks both documents, and that each agent's servers are registered.
    registry = FileRegistrySource(
        FileRegistryOptions(
            agents_path=ROOT / "registry" / "agents.yaml",
            tools_path=ROOT / "registry" / "mcp-tools.yaml",
        )
    )
    for agent in registry.agents.entries():
        assert agent.owner
    for server in registry.tools.entries():
        assert server.tools, f"{server.id} offers no tools"


def test_the_rules_are_a_valid_document() -> None:
    rules = RulesPolicyDecisionPoint(
        RulesPolicyOptions(path=ROOT / "policies" / "agentlib" / "rules" / "data.yaml"),
        SequentialIds(),
    )
    assert rules.revision.startswith("sha256:")

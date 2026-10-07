"""Every registry source passes the same contract suite."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ai_agent_lib_core.adapters import FileRegistryOptions, FileRegistrySource
from ai_agent_lib_core.adapters.registry_documents import agents_document, tools_document
from ai_agent_lib_core.contracts import AgentEntry, RegistrySource, ServerEntry
from ai_agent_lib_core.testing import FakeRegistry
from ai_agent_lib_core.testing.contracts import RegistrySourceContract


class TestYamlFileRegistrySource(RegistrySourceContract):
    def make_source(
        self, tmp_path: Path, agents: tuple[AgentEntry, ...], servers: tuple[ServerEntry, ...]
    ) -> RegistrySource:
        options = FileRegistryOptions(
            agents_path=tmp_path / "agents.yaml", tools_path=tmp_path / "mcp-tools.yaml"
        )
        options.agents_path.write_text(yaml.safe_dump(agents_document(agents)), encoding="utf-8")
        options.tools_path.write_text(yaml.safe_dump(tools_document(servers)), encoding="utf-8")
        return FileRegistrySource(options)


class TestJsonFileRegistrySource(RegistrySourceContract):
    def make_source(
        self, tmp_path: Path, agents: tuple[AgentEntry, ...], servers: tuple[ServerEntry, ...]
    ) -> RegistrySource:
        options = FileRegistryOptions(
            agents_path=tmp_path / "agents.json", tools_path=tmp_path / "mcp-tools.json"
        )
        options.agents_path.write_text(json.dumps(agents_document(agents)), encoding="utf-8")
        options.tools_path.write_text(json.dumps(tools_document(servers)), encoding="utf-8")
        return FileRegistrySource(options)


class TestFakeRegistry(RegistrySourceContract):
    def make_source(
        self, tmp_path: Path, agents: tuple[AgentEntry, ...], servers: tuple[ServerEntry, ...]
    ) -> RegistrySource:
        return FakeRegistry(agents, servers)

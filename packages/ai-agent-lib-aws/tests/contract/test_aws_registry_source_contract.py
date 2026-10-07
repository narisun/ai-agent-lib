"""The S3 registry passes the contract suite every registry source passes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from ai_agent_lib_aws.registry_s3 import S3RegistryOptions, S3RegistrySource
from ai_agent_lib_aws.testing import FakeS3, offline_sessions
from ai_agent_lib_core.contracts import AgentEntry, RegistrySource, ServerEntry
from ai_agent_lib_core.kit import agents_document, tools_document
from ai_agent_lib_core.testing.contracts import RegistrySourceContract


def started(storage: FakeS3, options: S3RegistryOptions) -> S3RegistrySource:
    source = S3RegistrySource(options, offline_sessions(), client=storage)
    asyncio.run(source.start())
    return source


class TestYamlS3RegistrySource(RegistrySourceContract):
    def make_source(
        self, tmp_path: Path, agents: tuple[AgentEntry, ...], servers: tuple[ServerEntry, ...]
    ) -> RegistrySource:
        storage = FakeS3()
        storage.put("eap-registry", "registry/agents.yaml", yaml.safe_dump(agents_document(agents)))
        storage.put(
            "eap-registry", "registry/mcp-tools.yaml", yaml.safe_dump(tools_document(servers))
        )
        return started(storage, S3RegistryOptions(bucket="eap-registry"))


class TestJsonS3RegistrySource(RegistrySourceContract):
    def make_source(
        self, tmp_path: Path, agents: tuple[AgentEntry, ...], servers: tuple[ServerEntry, ...]
    ) -> RegistrySource:
        storage = FakeS3(versioned=False)
        storage.put("eap-registry", "prod/agents.json", json.dumps(agents_document(agents)))
        storage.put("eap-registry", "prod/tools.json", json.dumps(tools_document(servers)))
        options = S3RegistryOptions(
            bucket="eap-registry", agents_key="prod/agents.json", tools_key="prod/tools.json"
        )
        return started(storage, options)

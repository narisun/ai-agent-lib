"""Testing a service as it is configured, with nothing outside the process.

The fakes in :mod:`ai_agent_lib_core.testing.harness` replace every adapter,
which is the fastest way to test graph and tool logic. The helpers here are
for the other question: does the service work with its own configuration? They
read the service's ``.env.example``, keep the real local adapters (the rules
file, the registry files, CSV data) and replace only what a test must control:
the model, and where local state is written.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_agent_lib_core.adapters import (
    FakeChatModelProvider,
    FileRegistryOptions,
    FileRegistrySource,
)
from ai_agent_lib_core.adapters.registry_documents import agents_document
from ai_agent_lib_core.config import ConfigResolver, DotenvConfigSource
from ai_agent_lib_core.contracts import (
    AgentEntry,
    Classification,
    ConfigurationError,
    ModelSection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import MODEL_PORT, BuildContext, ServiceContainer, ServiceProviders
from ai_agent_lib_core.testing.harness import FAKE_PROVIDER

__all__ = [
    "AUDIT_FILE",
    "CHECKPOINT_FILE",
    "TEST_AGENT",
    "audit_records",
    "last_shown_to_model",
    "load_test_config",
    "scripted_model",
    "scripted_providers",
]

AUDIT_FILE = "audit.jsonl"
CHECKPOINT_FILE = "checkpoints.sqlite"
TEST_AGENT = "test-agent"
"""The agent a test of an MCP server calls it through."""

_AGENTS_FILE = "agents-with-test-agent.json"
_FILE = "file"

_FAKE_MODEL_ID = "fake-model"
_JSONL, _SQLITE = "jsonl", "sqlite"
_STATIC = "static"


def _with_test_agent(config: ServiceConfig, name: str, state_dir: Path) -> ServiceConfig:
    """Register ``name`` for every MCP server, in a copy of the agent registry."""
    selection = config.section(Section.REGISTRY)
    if selection.provider != _FILE:
        raise ConfigurationError(
            "a test agent can only be added to a registry kept in files; this service "
            f"selects the {selection.provider!r} registry provider"
        )
    options = selection.parse_options(FileRegistryOptions)
    registry = FileRegistrySource(options)
    agent = AgentEntry(
        id=name,
        owner="tests",
        version="0",
        description="Stands in for an agent in the tests of an MCP server.",
        mcp_servers=tuple(server.id for server in registry.tools.entries()),
        classification_ceiling=Classification.RESTRICTED,
    )
    others = [entry for entry in registry.agents.entries() if entry.id != name]
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / _AGENTS_FILE
    path.write_text(json.dumps(agents_document([*others, agent])), encoding="utf-8")
    return config.with_section(
        Section.REGISTRY,
        replace(selection, options={**selection.options, "agents_path": str(path)}),
    )


def _with_roles(config: ServiceConfig, roles: Sequence[str]) -> ServiceConfig:
    """Give the caller who has no token these roles."""
    selection = config.section(Section.IDENTITY)
    if selection.provider != _STATIC:
        raise ConfigurationError(
            "roles can only be given to the development identity; this service selects the "
            f"{selection.provider!r} identity provider"
        )
    return config.with_section(
        Section.IDENTITY, replace(selection, options={**selection.options, "roles": list(roles)})
    )


def load_test_config(
    dotenv_path: Path,
    *,
    state_dir: Path,
    fake_model: bool = True,
    test_agent: str | None = None,
    roles: Sequence[str] | None = None,
) -> ServiceConfig:
    """Resolve a service's own ``.env`` file for a test.

    Nothing is read from the process environment, so the result is the same on
    every machine. Relative paths in the file are relative to its folder. The
    audit log and the checkpoint database, when they are local files, are
    written under ``state_dir`` instead of the service's own state folder.

    Args:
        dotenv_path: The file to read, normally the service's ``.env.example``.
        state_dir: Where the test's audit log and checkpoints go.
        fake_model: Select the ``fake`` model provider whatever the file says,
            so a test can never call a real model.
        test_agent: For a test of an MCP server. A server only answers an
            agent the registry lists for it. With this, the test gets a copy
            of the agent registry that also holds an agent of this name,
            listed for every server, so the server can be tested on its own.
            The registry files themselves are not changed.
        roles: The roles of the caller who has no token, in place of the ones
            in the file. For a service on the development identity: it is how
            a test says who is asking.

    Raises:
        ConfigurationError: If the file is missing or invalid, or an argument
            does not fit the adapters the file selects.
    """
    source = DotenvConfigSource(dotenv_path, required=True)
    config = ConfigResolver(source, base_dir=dotenv_path.resolve().parent).resolve()
    for section, provider, file_name in (
        (Section.AUDIT, _JSONL, AUDIT_FILE),
        (Section.CHECKPOINT, _SQLITE, CHECKPOINT_FILE),
    ):
        selection = config.section(section)
        if selection.provider == provider:
            options = {**selection.options, "path": str(state_dir / file_name)}
            config = config.with_section(section, replace(selection, options=options))
    if fake_model:
        config = replace(
            config, model=ModelSection(provider=FAKE_PROVIDER, model_id=_FAKE_MODEL_ID)
        )
    if test_agent is not None:
        config = _with_test_agent(config, test_agent, state_dir)
    if roles is not None:
        config = _with_roles(config, roles)
    return config


def scripted_providers(*replies: Any) -> ServiceProviders:
    """Return the local adapters, with a ``fake`` model that replays ``replies``.

    After the run, :func:`scripted_model` gives the provider, whose ``models``
    record every prompt they were sent, and :func:`last_shown_to_model` gives
    the last thing the model saw.
    """

    def fake_model(context: BuildContext) -> FakeChatModelProvider:  # noqa: ARG001 - no options
        return FakeChatModelProvider(list(replies))

    return ServiceProviders.default().register(MODEL_PORT, FAKE_PROVIDER, fake_model, replace=True)


def scripted_model(services: ServiceContainer) -> FakeChatModelProvider:
    """Return the scripted model of a container, to see what it was sent.

    Raises:
        TypeError: If the container's ``fake`` model is not a scripted one.
    """
    provider = services.model_provider(FAKE_PROVIDER)
    if not isinstance(provider, FakeChatModelProvider):
        raise TypeError("this container has no scripted model; build it with scripted_providers()")
    return provider


def last_shown_to_model(services: ServiceContainer) -> str:
    """Return the last message of the last prompt the scripted model was sent.

    After a tool call that is the tool's result, exactly as the model saw it:
    marked as data, and with anything the caller may not see already masked.

    Raises:
        LookupError: If the model was never called.
    """
    calls = [call for model in scripted_model(services).models for call in model.calls]
    if not calls or not calls[-1]:
        raise LookupError("the scripted model was never called")
    return str(calls[-1][-1].content)


def audit_records(state_dir: Path) -> list[dict[str, Any]]:
    """Return the audit records a test wrote under ``state_dir``, oldest first."""
    path = state_dir / AUDIT_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

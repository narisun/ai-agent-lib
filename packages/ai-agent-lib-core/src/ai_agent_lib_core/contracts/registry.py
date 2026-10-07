"""The agent and tool registries: which agents and MCP tools exist.

A registry holds data only. An entry never carries a command, an import path
or a credential, and being registered grants nothing by itself: policy still
decides every call. What the registries add is the opposite guarantee, that an
agent or a tool which is not registered cannot be used at all.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, TypeVar

from ai_agent_lib_core.contracts._validation import require_identifier
from ai_agent_lib_core.contracts.identity import Classification

__all__ = [
    "REGISTRY_SCHEMA",
    "AgentEntry",
    "AgentRegistry",
    "AgentSnapshot",
    "EntryStatus",
    "RegistrySource",
    "ServerEntry",
    "ToolEntry",
    "ToolRegistry",
    "ToolSnapshot",
    "schema_fingerprint",
]

REGISTRY_SCHEMA = "agentlib.registry/v1"
"""The schema identifier every registry document must declare."""

_SHA256_HEX_LENGTH = 64


class EntryStatus(enum.StrEnum):
    """Whether a registered agent may be used."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


def schema_fingerprint(schema: Mapping[str, object]) -> str:
    """Return the SHA-256 of a tool's input schema, as lower-case hex.

    The schema is written as canonical JSON first, so two schemas that differ
    only in key order or spacing have the same fingerprint.
    """
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One registered tool of an MCP server.

    Attributes:
        name: The tool's name on its server.
        version: The registered version of the tool.
        classification: How sensitive the data the tool returns is.
        read_only: Whether the tool only reads. Only read-only tools are retried.
        schema_sha256: The pinned fingerprint of the tool's input schema, if any.
        description: What the tool does, for people reading the registry.
    """

    name: str
    version: str
    classification: Classification = Classification.INTERNAL
    read_only: bool = False
    schema_sha256: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        require_identifier("tool name", self.name)
        require_identifier("tool version", self.version)
        pin = self.schema_sha256
        if pin is not None and (
            len(pin) != _SHA256_HEX_LENGTH or any(ch not in "0123456789abcdef" for ch in pin)
        ):
            raise ValueError("schema_sha256 must be 64 lower-case hexadecimal characters")


@dataclass(frozen=True, slots=True)
class ServerEntry:
    """One registered MCP server and its tools.

    Attributes:
        id: The name agents use for the server.
        owner: The team that owns it.
        url: Where the server is reached.
        audience: The audience a token for this server must carry, if any.
        tools: The tools the server is registered to offer.
    """

    id: str
    owner: str
    url: str
    audience: str | None = None
    tools: tuple[ToolEntry, ...] = ()

    def __post_init__(self) -> None:
        require_identifier("server id", self.id)
        require_identifier("owner", self.owner)
        require_identifier("url", self.url)
        object.__setattr__(self, "tools", tuple(self.tools))
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"server {self.id!r} lists a tool more than once")

    def tool(self, name: str) -> ToolEntry | None:
        """Return the registered tool called ``name``, or ``None``."""
        return next((tool for tool in self.tools if tool.name == name), None)


@dataclass(frozen=True, slots=True)
class AgentEntry:
    """One registered agent.

    Attributes:
        id: The agent's name. It is the application name in a request context.
        client_id: The agent's own identifier at the identity provider, which
            is how a token names the agent that requested it.
        owner: The team that owns it.
        version: The registered version of the agent.
        description: What the agent does.
        url: Where the agent is reached, if it serves other agents.
        mcp_servers: The MCP servers the agent may call.
        model_aliases: The model aliases the agent is expected to use. This
            is a description for people and tooling. Which models an agent may
            use is decided by the policy, on every model call.
        classification_ceiling: The most sensitive data the agent may handle.
        status: Whether the agent may be used.
    """

    id: str
    owner: str
    version: str
    description: str = ""
    url: str | None = None
    mcp_servers: tuple[str, ...] = ()
    model_aliases: tuple[str, ...] = ()
    classification_ceiling: Classification = Classification.INTERNAL
    status: EntryStatus = EntryStatus.ACTIVE
    client_id: str | None = None

    def __post_init__(self) -> None:
        if self.client_id is not None:
            require_identifier("client_id", self.client_id)
        require_identifier("agent id", self.id)
        require_identifier("owner", self.owner)
        require_identifier("agent version", self.version)
        object.__setattr__(self, "mcp_servers", tuple(self.mcp_servers))
        object.__setattr__(self, "model_aliases", tuple(self.model_aliases))
        object.__setattr__(self, "status", EntryStatus(self.status))


EntryT = TypeVar("EntryT", AgentEntry, ServerEntry)


class AgentRegistry(Protocol):
    """Answers which agents exist."""

    @property
    def revision(self) -> str:
        """Identifies the version of the registry these answers come from."""
        ...

    def get(self, agent_id: str) -> AgentEntry | None:
        """Return the agent called ``agent_id``, or ``None`` if it is not registered."""
        ...

    def resolve(self, identifier: str) -> AgentEntry | None:
        """Return the agent that ``identifier`` names, or ``None``.

        The identifier is how a token names an agent: its client ID at the
        identity provider, or its registry ID.
        """
        ...

    def entries(self) -> tuple[AgentEntry, ...]:
        """Return every registered agent, sorted by ID."""
        ...


class ToolRegistry(Protocol):
    """Answers which MCP servers and tools exist."""

    @property
    def revision(self) -> str:
        """Identifies the version of the registry these answers come from."""
        ...

    def get(self, server_id: str) -> ServerEntry | None:
        """Return the server called ``server_id``, or ``None`` if it is not registered."""
        ...

    def entries(self) -> tuple[ServerEntry, ...]:
        """Return every registered server, sorted by ID."""
        ...


class RegistrySource(Protocol):
    """Where both registries come from: files now, a registry database later."""

    @property
    def agents(self) -> AgentRegistry:
        """The agent registry."""
        ...

    @property
    def tools(self) -> ToolRegistry:
        """The MCP tool registry."""
        ...


def _index(kind: str, entries: Iterable[EntryT]) -> Mapping[str, EntryT]:
    ordered = sorted(entries, key=lambda entry: entry.id)
    indexed = {entry.id: entry for entry in ordered}
    if len(indexed) != len(ordered):
        raise ValueError(f"an {kind} ID is registered more than once")
    return MappingProxyType(indexed)


class AgentSnapshot:
    """An immutable agent registry, as loaded at one moment.

    Args:
        entries: The registered agents.
        revision: Identifies what was loaded.

    Raises:
        ValueError: If two agents share an ID.
    """

    def __init__(self, entries: Iterable[AgentEntry] = (), *, revision: str = "empty") -> None:
        self._entries = _index("agent", entries)
        self._revision = revision
        by_client: dict[str, AgentEntry] = {}
        for entry in self._entries.values():
            if entry.client_id is None:
                continue
            if entry.client_id in by_client or entry.client_id in self._entries:
                raise ValueError("an agent client ID is registered more than once")
            by_client[entry.client_id] = entry
        self._by_client: Mapping[str, AgentEntry] = MappingProxyType(by_client)

    @property
    def revision(self) -> str:
        """Identifies what was loaded."""
        return self._revision

    def get(self, agent_id: str) -> AgentEntry | None:
        """Return the agent called ``agent_id``, or ``None``."""
        return self._entries.get(agent_id)

    def resolve(self, identifier: str) -> AgentEntry | None:
        """Return the agent with this client ID or registry ID, or ``None``."""
        return self._by_client.get(identifier) or self._entries.get(identifier)

    def entries(self) -> tuple[AgentEntry, ...]:
        """Return every registered agent, sorted by ID."""
        return tuple(self._entries.values())


class ToolSnapshot:
    """An immutable MCP tool registry, as loaded at one moment.

    Args:
        entries: The registered servers.
        revision: Identifies what was loaded.

    Raises:
        ValueError: If two servers share an ID.
    """

    def __init__(self, entries: Iterable[ServerEntry] = (), *, revision: str = "empty") -> None:
        self._entries = _index("MCP server", entries)
        self._revision = revision

    @property
    def revision(self) -> str:
        """Identifies what was loaded."""
        return self._revision

    def get(self, server_id: str) -> ServerEntry | None:
        """Return the server called ``server_id``, or ``None``."""
        return self._entries.get(server_id)

    def entries(self) -> tuple[ServerEntry, ...]:
        """Return every registered server, sorted by ID."""
        return tuple(self._entries.values())

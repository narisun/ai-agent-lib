"""The workspace file: the answers a workspace was generated from.

``agentlib.toml`` sits at the root of a workspace. It marks the folder as a
workspace, and it records every answer, so the same workspace can be generated
again from the file alone.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Self

from ai_agent_lib_cli.errors import CliError

__all__ = [
    "MODEL_PROVIDERS",
    "WORKSPACE_FILE",
    "AgentAnswers",
    "LibrarySource",
    "McpAnswers",
    "WorkspaceAnswers",
    "find_workspace",
    "load_answers",
]

WORKSPACE_FILE = "agentlib.toml"
MODEL_PROVIDERS = ("fake", "anthropic", "bedrock")

_HEADER = (
    "# Written by agentlib. It records the answers this workspace was generated from.\n"
    "# Generate the same workspace elsewhere with: agentlib init --answers agentlib.toml <folder>\n"
)


def _text(value: str) -> str:
    """Return ``value`` as a TOML string."""
    return json.dumps(value, ensure_ascii=False)


def _list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_text(value) for value in values) + "]"


@dataclass(frozen=True, slots=True)
class LibrarySource:
    """Where a generated project installs the library from.

    Attributes:
        kind: ``path`` for a local checkout, ``index`` for a package index.
        path: The checkout folder, with forward slashes, when ``kind`` is ``path``.
        version: The version specifier, when ``kind`` is ``index``.
    """

    kind: Literal["path", "index"]
    path: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "path" and not self.path:
            raise CliError("a library installed from a path needs the path of the checkout")
        if self.kind == "index" and not self.version:
            raise CliError("a library installed from an index needs a version, such as '>=0.1'")

    @classmethod
    def from_path(cls, checkout: Path) -> Self:
        """Return the source for a local checkout of the library."""
        return cls(kind="path", path=checkout.resolve().as_posix())


@dataclass(frozen=True, slots=True)
class AgentAnswers:
    """The answers one agent was generated from."""

    name: str
    description: str
    model_provider: str = "fake"
    port: int = 8000
    mcp_servers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class McpAnswers:
    """The answers one MCP server was generated from."""

    name: str
    server_id: str
    description: str
    port: int = 8100


@dataclass(frozen=True, slots=True)
class WorkspaceAnswers:
    """Every answer a workspace was generated from."""

    name: str
    owner: str
    library: LibrarySource
    agents: tuple[AgentAnswers, ...] = field(default=())
    mcp_servers: tuple[McpAnswers, ...] = field(default=())

    def agent(self, name: str) -> AgentAnswers | None:
        """Return the agent called ``name``, if the workspace has it."""
        return next((agent for agent in self.agents if agent.name == name), None)

    def mcp_server(self, name_or_id: str) -> McpAnswers | None:
        """Return the MCP server with that name or registry ID, if the workspace has it."""
        return next(
            (s for s in self.mcp_servers if name_or_id in (s.name, s.server_id)),
            None,
        )

    def with_agent(self, agent: AgentAnswers) -> WorkspaceAnswers:
        """Return a copy that holds ``agent``, replacing one of the same name."""
        kept = tuple(a for a in self.agents if a.name != agent.name)
        return replace(self, agents=tuple(sorted((*kept, agent), key=lambda a: a.name)))

    def with_mcp_server(self, server: McpAnswers) -> WorkspaceAnswers:
        """Return a copy that holds ``server``, replacing one of the same name."""
        kept = tuple(s for s in self.mcp_servers if s.name != server.name)
        return replace(self, mcp_servers=tuple(sorted((*kept, server), key=lambda s: s.name)))

    def service_names(self) -> frozenset[str]:
        """Return the name of every agent and MCP server."""
        return frozenset(a.name for a in self.agents) | frozenset(s.name for s in self.mcp_servers)

    def used_ports(self) -> frozenset[int]:
        """Return every port a service of the workspace listens on."""
        return frozenset(a.port for a in self.agents) | frozenset(s.port for s in self.mcp_servers)

    def to_toml(self) -> str:
        """Return the workspace file."""
        library = self.library
        source = (
            f"path = {_text(library.path or '')}"
            if library.kind == "path"
            else f"version = {_text(library.version or '')}"
        )
        lines = [
            _HEADER,
            "[workspace]",
            f"name = {_text(self.name)}",
            f"owner = {_text(self.owner)}",
            "",
            "[library]",
            f"source = {_text(library.kind)}",
            source,
        ]
        for server in self.mcp_servers:
            lines += [
                "",
                "[[mcp_servers]]",
                f"name = {_text(server.name)}",
                f"server_id = {_text(server.server_id)}",
                f"description = {_text(server.description)}",
                f"port = {server.port}",
            ]
        for agent in self.agents:
            lines += [
                "",
                "[[agents]]",
                f"name = {_text(agent.name)}",
                f"description = {_text(agent.description)}",
                f"model_provider = {_text(agent.model_provider)}",
                f"port = {agent.port}",
                f"mcp_servers = {_list(agent.mcp_servers)}",
            ]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_toml(cls, text: str, *, what: str = WORKSPACE_FILE) -> Self:
        """Read a workspace file.

        Raises:
            CliError: If the text is not a workspace file.
        """
        try:
            raw = tomllib.loads(text)
            workspace, library = raw["workspace"], raw["library"]
            return cls(
                name=_string(workspace, "name"),
                owner=_string(workspace, "owner"),
                library=LibrarySource(
                    kind=library["source"],
                    path=library.get("path"),
                    version=library.get("version"),
                ),
                agents=tuple(
                    AgentAnswers(
                        name=_string(agent, "name"),
                        description=_string(agent, "description"),
                        model_provider=_string(agent, "model_provider"),
                        port=int(agent["port"]),
                        mcp_servers=tuple(agent.get("mcp_servers", ())),
                    )
                    for agent in raw.get("agents", ())
                ),
                mcp_servers=tuple(
                    McpAnswers(
                        name=_string(server, "name"),
                        server_id=_string(server, "server_id"),
                        description=_string(server, "description"),
                        port=int(server["port"]),
                    )
                    for server in raw.get("mcp_servers", ())
                ),
            )
        except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CliError(
                f"{what} is not a workspace file ({type(exc).__name__}: {exc})"
            ) from None


def _string(table: Mapping[str, Any], key: str) -> str:
    value = table[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be text")
    return value


def find_workspace(start: Path) -> Path:
    """Return the root of the workspace that holds ``start``.

    Raises:
        CliError: If ``start`` is not inside a workspace.
    """
    here = start.resolve()
    for folder in (here, *here.parents):
        if (folder / WORKSPACE_FILE).is_file():
            return folder
    raise CliError(
        f"{start} is not inside a workspace: no {WORKSPACE_FILE} here or above. "
        "Create one with 'agentlib init <name>'."
    )


def load_answers(root: Path) -> WorkspaceAnswers:
    """Read the workspace file of the workspace at ``root``."""
    path = root / WORKSPACE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise CliError(f"{path} cannot be read") from None
    return WorkspaceAnswers.from_toml(text, what=str(path))

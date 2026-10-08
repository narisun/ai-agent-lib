"""The workspace file: the answers a workspace was generated from.

``agentlib.toml`` sits at the root of a workspace. It marks the folder as a
workspace, and it records every answer, so the same workspace can be generated
again from the file alone.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Self

from ai_agent_lib_cli.dataplan import DataPlan, sample_plan
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.toml_text import string_of, toml_list, toml_text

__all__ = [
    "AGENTS_FOLDER",
    "MODEL_PROVIDERS",
    "SERVERS_FOLDER",
    "WORKSPACE_FILE",
    "AgentAnswers",
    "LibrarySource",
    "McpAnswers",
    "WorkspaceAnswers",
    "find_workspace",
    "load_answers",
]

WORKSPACE_FILE = "agentlib.toml"
AGENTS_FOLDER = "agents"
"""The folder of a workspace that holds one folder per agent."""
SERVERS_FOLDER = "mcp-servers"
"""The folder of a workspace that holds one folder per MCP server."""
MODEL_PROVIDERS = ("fake", "anthropic", "bedrock")

_HEADER = (
    "# Written by agentlib. It records the answers this workspace was generated from.\n"
    "# Generate the same workspace elsewhere with: agentlib init --answers agentlib.toml <folder>\n"
)


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
    """The answers one MCP server was generated from.

    Attributes:
        name: The service's name.
        server_id: The server's ID in the tool registry.
        description: One line on what the server offers.
        port: The local HTTP port.
        data: The queries the server offers, when a reader proposed them from
            the developer's data. Without it the server is the hello-world sample.
    """

    name: str
    server_id: str
    description: str
    port: int = 8100
    data: DataPlan | None = None

    @property
    def plan(self) -> DataPlan:
        """The plan the server is generated from."""
        return self.data if self.data is not None else sample_plan()


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
            f"path = {toml_text(library.path or '')}"
            if library.kind == "path"
            else f"version = {toml_text(library.version or '')}"
        )
        lines = [
            _HEADER,
            "[workspace]",
            f"name = {toml_text(self.name)}",
            f"owner = {toml_text(self.owner)}",
            "",
            "[library]",
            f"source = {toml_text(library.kind)}",
            source,
        ]
        for server in self.mcp_servers:
            lines += [
                "",
                "[[mcp_servers]]",
                f"name = {toml_text(server.name)}",
                f"server_id = {toml_text(server.server_id)}",
                f"description = {toml_text(server.description)}",
                f"port = {server.port}",
            ]
            if server.data is not None:
                lines += server.data.to_toml("mcp_servers.data")
        for agent in self.agents:
            lines += [
                "",
                "[[agents]]",
                f"name = {toml_text(agent.name)}",
                f"description = {toml_text(agent.description)}",
                f"model_provider = {toml_text(agent.model_provider)}",
                f"port = {agent.port}",
                f"mcp_servers = {toml_list(agent.mcp_servers)}",
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
                name=string_of(workspace, "name"),
                owner=string_of(workspace, "owner"),
                library=LibrarySource(
                    kind=library["source"],
                    path=library.get("path"),
                    version=library.get("version"),
                ),
                agents=tuple(
                    AgentAnswers(
                        name=string_of(agent, "name"),
                        description=string_of(agent, "description"),
                        model_provider=string_of(agent, "model_provider"),
                        port=int(agent["port"]),
                        mcp_servers=tuple(agent.get("mcp_servers", ())),
                    )
                    for agent in raw.get("agents", ())
                ),
                mcp_servers=tuple(
                    McpAnswers(
                        name=string_of(server, "name"),
                        server_id=string_of(server, "server_id"),
                        description=string_of(server, "description"),
                        port=int(server["port"]),
                        data=DataPlan.from_toml(server["data"]) if "data" in server else None,
                    )
                    for server in raw.get("mcp_servers", ())
                ),
            )
        except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CliError(
                f"{what} is not a workspace file ({type(exc).__name__}: {exc})"
            ) from None


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

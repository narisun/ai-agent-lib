"""Registries read from local YAML or JSON files."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core.adapters.registry_documents import (
    ABSENT_REVISION,
    RegistryDocument,
    load_registries,
)
from ai_agent_lib_core.contracts import (
    AgentRegistry,
    ConfigurationError,
    OptionsModel,
    ToolRegistry,
)

__all__ = ["ABSENT_REVISION", "FileRegistryOptions", "FileRegistrySource"]


class FileRegistryOptions(OptionsModel):
    """Options of the ``file`` registry provider.

    Attributes:
        agents_path: The agent registry file.
        tools_path: The MCP tool registry file.
    """

    agents_path: Path = Path("registry/agents.yaml")
    tools_path: Path = Path("registry/mcp-tools.yaml")


def _read(path: Path, kind: str) -> RegistryDocument | None:
    what = f"{kind} ({path})"
    if not path.exists():
        return None
    try:
        content = path.read_bytes()
    except OSError:
        raise ConfigurationError(f"{what}: the file could not be read as text") from None
    return RegistryDocument(content=content, suffix=path.suffix, what=what)


class FileRegistrySource:
    """Loads both registries once and holds them as immutable snapshots.

    A file that does not exist gives an empty registry: nothing is registered,
    so nothing that needs registration can be used. A file that exists and is
    not valid stops startup.

    Args:
        options: Where the files are.

    Raises:
        ConfigurationError: If a file is invalid, or an agent names an MCP
            server that the tool registry does not hold.
    """

    def __init__(self, options: FileRegistryOptions) -> None:
        self._options = options
        self._agents, self._tools = load_registries(
            agents=_read(options.agents_path, "agent registry"),
            tools=_read(options.tools_path, "tool registry"),
        )

    @property
    def agents(self) -> AgentRegistry:
        """The agent registry."""
        return self._agents

    @property
    def tools(self) -> ToolRegistry:
        """The MCP tool registry."""
        return self._tools

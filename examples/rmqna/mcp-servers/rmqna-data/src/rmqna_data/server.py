"""The MCP server: ordinary MCP tools, with two touchpoints into the library."""

from __future__ import annotations

from typing import Any

from ai_agent_lib_core import ServiceContainer
from mcp.server.mcpserver import MCPServer

__all__ = ["APPLICATION", "DATA_SOURCE", "SERVER_ID", "build_server"]

APPLICATION = "rmqna-data"
"""The server's application name: its token audience, and its name in policy and audit."""

SERVER_ID = "rmqna-data"
"""The server's ID in the tool registry. Agents name its tools ``rmqna-data/<tool>``."""

DATA_SOURCE = "people"
"""The name of the data source the tools read, as configured."""

INSTRUCTIONS = (
    "A greeting, and read-only tools over the people directory."
    " Results are tables; a column a caller may not see is returned masked."
)


def build_server(services: ServiceContainer) -> MCPServer[Any]:
    """Build the MCP server from governed parts.

    Args:
        services: A started service container.
    """
    server: MCPServer[Any] = MCPServer(
        APPLICATION,
        instructions=INSTRUCTIONS,
        # Touchpoint 1: the middleware that governs every tool call and, when
        # callers present tokens from an identity provider, the token check at the door.
        **services.mcp_server_kwargs(SERVER_ID, application=APPLICATION),
    )
    source = services.data_source(DATA_SOURCE)  # touchpoint 2

    @server.tool(name="rmqna-data.greet")
    async def greet(name: str) -> str:
        """Return a greeting for a person, given their name."""
        return f"Hello, {name.strip()}! This is {APPLICATION}."

    @server.tool(name="rmqna-data.people_by_team")
    async def people_by_team(team: str) -> dict[str, Any]:
        "The people in one team: platform, payments or risk."
        # A named query, never SQL from a caller. Policy decides the rows and
        # columns this caller sees before anything is returned.
        result = await source.query("people_by_team", {"team": team})
        return result.to_payload()

    return server

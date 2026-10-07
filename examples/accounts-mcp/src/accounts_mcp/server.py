"""The MCP server: ordinary MCP tools, with two touchpoints into the library."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ai_agent_lib_core import ServiceContainer

__all__ = ["APPLICATION", "DATA_SOURCE", "SERVER_ID", "build_server"]

APPLICATION = "accounts-mcp"
"""The server's application name: its token audience, and its name in policy and audit."""

SERVER_ID = "accounts"
"""The server's ID in the tool registry."""

DATA_SOURCE = "ledger"
"""The name of the data source the tools read, as configured."""

INSTRUCTIONS = (
    "Read-only tools over the accounts ledger. Results are tables; a column a "
    "caller may not see is returned masked."
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
    ledger = services.data_source(DATA_SOURCE)  # touchpoint 2

    @server.tool(name="accounts.by_region")
    async def by_region(region: str, min_balance: float = 0) -> dict[str, Any]:
        """List the accounts in one region, optionally only those above a balance."""
        result = await ledger.query(
            "accounts_by_region", {"region": region, "min_balance": min_balance}
        )
        return result.to_payload()

    @server.tool(name="accounts.balance")
    async def balance(account_id: int) -> dict[str, Any]:
        """Return the holder and balance of one account, given its account number."""
        result = await ledger.query("account_balance", {"account_id": account_id})
        return result.to_payload()

    return server

"""Testing MCP servers and agents together without a network.

This module needs the MCP SDK; install the ``mcp`` extra of ``ai-agent-lib-core``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent_lib_core.contracts import ServerEntry, TransientError
from ai_agent_lib_core.integrations.mcp import META_AUTHORIZATION

__all__ = ["InProcessMcpConnector"]


class InProcessMcpConnector:
    """Connects an agent to MCP servers that live in the same process.

    There is no HTTP, so the caller's token travels in the request metadata.

    Args:
        servers: The servers, by their ID in the tool registry.

    Attributes:
        connections: The server ID of every connection made, in order.
    """

    def __init__(self, servers: Mapping[str, MCPServer[Any]]) -> None:
        self._servers = dict(servers)
        self.connections: list[str] = []

    @asynccontextmanager
    async def connect(self, server: ServerEntry, token: SecretStr | None) -> AsyncIterator[Client]:  # noqa: ARG002
        """Connect to the in-process server registered under ``server.id``."""
        target = self._servers.get(server.id)
        if target is None:
            raise TransientError(f"MCP server {server.id!r} is not running in this process")
        self.connections.append(server.id)
        async with Client(target) as client:
            yield client

    def credential_meta(self, token: SecretStr | None) -> Mapping[str, str]:
        """Return the token as request metadata."""
        return {META_AUTHORIZATION: token.get_secret_value()} if token is not None else {}

"""Testing MCP servers and agents together without a network.

This module needs the MCP SDK; install the ``mcp`` extra of ``ai-agent-lib-core``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    Principal,
    RequestContext,
    ServerEntry,
    TokenExchanger,
    TransientError,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.mcp import META_AUTHORIZATION
from ai_agent_lib_core.testing.local import TEST_AGENT

__all__ = ["InProcessMcpConnector", "call_tool_as"]


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


async def call_tool_as(
    services: ServiceContainer,
    server: MCPServer[Any],
    tool: str,
    arguments: Mapping[str, Any],
    *,
    roles: Sequence[str],
    subject: str = "u-1",
    tenant: str = "t-1",
    agent: str = TEST_AGENT,
) -> Any:
    """Call a tool of an in-process MCP server the way an agent does for a caller.

    The server's own identity provider issues a token for a caller with
    ``roles`` who came through ``agent``, and the call presents it. That works
    with the development identity, which is what a test of a server runs on.

    Args:
        services: The server's started container.
        server: The server, built from ``services``.
        tool: The tool's name on the server.
        arguments: The tool's arguments.
        roles: The roles the caller holds at this server.
        subject: The caller.
        tenant: The caller's tenant.
        agent: The agent the caller came through. A server only answers an
            agent the registry lists for it: load the server's configuration
            with ``load_test_config(..., test_agent=TEST_AGENT)``.

    Returns:
        The MCP SDK's tool result.

    Raises:
        TypeError: If the server's identity provider cannot issue tokens.
    """
    identity = services.identity
    if not isinstance(identity, TokenExchanger):
        raise TypeError(
            "the identity provider of this container cannot issue a token for a test caller; "
            "test a server with the development identity"
        )
    caller = RequestContext(
        principal=Principal(subject=subject, tenant=tenant, roles=frozenset(roles)),
        application=agent,
        request_id="r-test",
        thread_id="th-test",
    )
    token = await identity.exchange(caller, server.name)
    async with Client(server) as client:
        meta: Any = {META_AUTHORIZATION: token.get_secret_value()}
        return await client.call_tool(tool, dict(arguments), meta=meta)

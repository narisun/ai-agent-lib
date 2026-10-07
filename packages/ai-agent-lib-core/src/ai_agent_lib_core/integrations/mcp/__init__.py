"""MCP bindings. The only package in core that imports the MCP SDK.

The names here are for an MCP server. The agent side is reached through
``services.mcp_tools(server)``; its module also needs LangChain, which a
server does not have to load.
"""

from ai_agent_lib_core.integrations.mcp.server import (
    DoorTokenVerifier,
    GovernedToolsMiddleware,
    resource_server_settings,
    tool_fingerprints,
    verify_registration,
)
from ai_agent_lib_core.integrations.mcp.wire import (
    META_AUTHORIZATION,
    META_CLASSIFICATION_CEILING,
    META_REQUEST_ID,
    META_THREAD_ID,
    is_error_result,
    mcp_result_text,
)

__all__ = [
    "META_AUTHORIZATION",
    "META_CLASSIFICATION_CEILING",
    "META_REQUEST_ID",
    "META_THREAD_ID",
    "DoorTokenVerifier",
    "GovernedToolsMiddleware",
    "is_error_result",
    "mcp_result_text",
    "resource_server_settings",
    "tool_fingerprints",
    "verify_registration",
]

"""Print the schema pins of this server's tools, for the tool registry."""

from __future__ import annotations

import asyncio
import sys

from accounts_mcp.server import build_server
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.integrations.mcp import tool_fingerprints

__all__ = ["main"]


async def main() -> None:
    """Write one ``name: pin`` line per tool."""
    async with ServiceContainer.from_env() as services:
        for name, pin in sorted((await tool_fingerprints(build_server(services))).items()):
            sys.stdout.write(f"{name}: {pin}\n")


if __name__ == "__main__":
    asyncio.run(main())

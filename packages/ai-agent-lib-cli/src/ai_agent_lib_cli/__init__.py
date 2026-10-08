"""Developer tooling for the Enterprise Agentic Platform library.

The ``agentlib`` command creates a local workspace and adds agents and MCP
servers to it. What it writes is ordinary code with its tests: a starting
point the developer owns from the first minute.
"""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("ai-agent-lib-cli")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"

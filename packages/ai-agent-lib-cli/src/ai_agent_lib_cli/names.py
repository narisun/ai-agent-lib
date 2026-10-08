"""The names of generated things, and the names derived from them."""

from __future__ import annotations

import keyword
import re

from ai_agent_lib_cli.errors import CliError

__all__ = ["check_name", "package_name", "server_id_for"]

_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_MAX_LENGTH = 28  # two names and a few words must fit in a rule ID
_MCP_SUFFIX = "-mcp"
# Names that would shadow a module every project has, or that a tool treats specially.
_RESERVED = frozenset({"test", "tests", "src", "site", "agentlib", "mcp", "langgraph"})


def check_name(kind: str, value: str) -> str:
    """Return ``value`` if it can name a workspace or a service.

    A name is lower-case words joined by hyphens, such as ``claims-agent``. It
    becomes a folder, a distribution, a Python package and an identifier in
    the registry and in policy, so it has to be valid as all of them.

    Raises:
        CliError: If it cannot.
    """
    if not _NAME.match(value) or len(value) > _MAX_LENGTH:
        raise CliError(
            f"{kind} name {value!r} is not usable: use lower-case letters, digits and "
            f"single hyphens, start with a letter, and stay within {_MAX_LENGTH} characters"
        )
    package = package_name(value)
    if package in _RESERVED or keyword.iskeyword(package):
        raise CliError(f"{kind} name {value!r} is reserved; choose another")
    return value


def package_name(name: str) -> str:
    """Return the Python package name of a service."""
    return name.replace("-", "_")


def server_id_for(name: str) -> str:
    """Return the registry ID an MCP server gets by default: its name without ``-mcp``."""
    trimmed = name.removesuffix(_MCP_SUFFIX)
    return trimmed or name

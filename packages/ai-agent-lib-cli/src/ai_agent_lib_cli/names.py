"""Translate service and external names into generated Python identifiers.

A service name such as ``claims-mcp`` names its directory and distribution;
``package_name`` produces ``claims_mcp`` and ``server_id_for`` produces the
default registry ID ``claims``. Schema readers normalize external identifiers
before ``python_name`` escapes reserved Python symbols.
"""

from __future__ import annotations

import builtins
import keyword
import re

from ai_agent_lib_cli.errors import CliError

__all__ = ["PLAIN_NAME", "check_name", "package_name", "python_name", "server_id_for"]

PLAIN_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
"""What a query, a parameter and a column may be called: lower case, digits, underscores."""

_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_MAX_LENGTH = 28  # two names and a few words must fit in a rule ID
_MCP_SUFFIX = "-mcp"
# Names that would shadow a module every project has, or that a tool treats specially.
_RESERVED = frozenset({"test", "tests", "src", "site", "agentlib", "mcp", "langgraph"})


def python_name(name: str) -> str:
    """Escape a normalized identifier when it would shadow a reserved symbol.

    The caller must first validate the identifier and afterwards check for
    collisions: both ``class`` and ``class_`` become ``class_``. This function
    does not normalize punctuation or guarantee uniqueness.
    """
    reserved = {"source", "server", "result", "build_server", "greet"}
    if keyword.iskeyword(name) or hasattr(builtins, name) or name in reserved:
        return name + "_"
    return name


def check_name(kind: str, value: str) -> str:
    """Return ``value`` if it can name a workspace or a service.

    A name is lower-case words joined by hyphens, such as ``claims-agent``. It
    names a folder and distribution, and may appear in registry and policy
    entries. ``package_name`` converts hyphens to underscores for Python;
    reserved package names are rejected here.

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

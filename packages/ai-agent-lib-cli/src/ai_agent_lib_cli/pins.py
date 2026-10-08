"""Asking a generated MCP server for the schema pins of its tools.

The server's own ``pins`` module prints them. It runs in a separate process,
in the service's folder, with the interpreter this command runs on.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.processes import run_code

__all__ = ["PinReader", "read_pins"]

PinReader = Callable[[Path, str], dict[str, str]]
"""Given a service folder and its package name, returns ``{tool name: pin}``."""

_LINE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]{1,64}): (?P<pin>[0-9a-f]{64})$")
_RUN = (
    "import runpy, sys; sys.path.insert(0, 'src'); "
    "runpy.run_module(sys.argv[1], run_name='__main__')"
)


def read_pins(service_dir: Path, package: str) -> dict[str, str]:
    """Run the server's ``pins`` module and return what it prints.

    Raises:
        CliError: If the module fails or prints nothing usable.
    """
    try:
        printed = run_code(service_dir, _RUN, f"{package}.pins")
    except CliError as problem:
        raise CliError(f"the tools of {package} could not be pinned: {problem}") from None
    pins = {
        match["name"]: match["pin"]
        for match in (_LINE.match(line.strip()) for line in printed.splitlines())
        if match is not None
    }
    if not pins:
        raise CliError(f"the tools of {package} could not be pinned: no output")
    return pins

"""Asking a generated MCP server for the schema pins of its tools.

The server's own ``pins`` module prints them. It runs in a separate process,
in the service's folder, with the interpreter this command runs on.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from ai_agent_lib_cli.errors import CliError

__all__ = ["PinReader", "read_pins"]

PinReader = Callable[[Path, str], dict[str, str]]
"""Given a service folder and its package name, returns ``{tool name: pin}``."""

_LINE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]{1,64}): (?P<pin>[0-9a-f]{64})$")
_TIMEOUT_SECONDS = 120
# The service is run from its source folder, installed or not.
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
        done = subprocess.run(  # noqa: S603 - this interpreter, a module of the generated service
            [sys.executable, "-c", _RUN, f"{package}.pins"],
            cwd=service_dir,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(
            f"the tools of {package} could not be pinned ({type(exc).__name__})"
        ) from None
    pins = {
        match["name"]: match["pin"]
        for match in (_LINE.match(line.strip()) for line in done.stdout.splitlines())
        if match is not None
    }
    if done.returncode != 0 or not pins:
        last = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise CliError(f"the tools of {package} could not be pinned: {last}")
    return pins

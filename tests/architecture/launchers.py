"""Support for tests that run the repository's batch launchers as child processes.

The ``.bat`` files run whatever ``python`` is first on PATH, which on a
contributor's machine or a CI runner may not be the interpreter running the
tests. These helpers make the launcher resolve to the test interpreter, so
the test checks the launcher and not the machine's PATH.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["interpreter_first_environment"]


def interpreter_first_environment() -> dict[str, str]:
    """Return a copy of the environment in which ``python`` is the test interpreter.

    The interpreter's folder goes first on PATH, and the variables that make
    one interpreter load another's standard library are removed.
    """
    environment = dict(os.environ)
    folder = str(Path(sys.executable).resolve().parent)
    environment["PATH"] = os.pathsep.join(
        [folder, *filter(None, environment.get("PATH", "").split(os.pathsep))]
    )
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSAFEPATH"):
        environment.pop(name, None)
    return environment

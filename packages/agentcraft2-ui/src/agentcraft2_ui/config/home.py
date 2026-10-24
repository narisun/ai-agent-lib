"""The project home, as Agent Lab reads it.

The UI may import only the library's contracts, so it reads ``AC2_HOME`` itself, as it
reads the agent's other settings. The meaning is the library's: one folder that holds a
project's files and the state it writes under ``.agentcraft2/``. Without it, state goes
under the working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["config_file", "project_home"]

_HOME = "AC2_HOME"
_STATE = ".agentcraft2"


def project_home() -> Path | None:
    """Return the project home, or ``None`` when ``AC2_HOME`` is not set.

    Raises:
        ValueError: If it names something that is not a folder.
    """
    raw = os.environ.get(_HOME, "").strip()
    if not raw:
        return None
    home = Path(raw).expanduser()
    if not home.is_dir():
        reason = "not a folder" if home.exists() else "no such folder"
        raise ValueError(f"{_HOME} does not name a folder ({reason}): create it, or correct it")
    return home.resolve()


def config_file(given: Path | None) -> Path:
    """Return the Agent Lab config file: the one named, or ui/config.json in the state folder."""
    if given is not None:
        return given.resolve()
    return ((project_home() or Path.cwd()) / _STATE / "ui" / "config.json").resolve()

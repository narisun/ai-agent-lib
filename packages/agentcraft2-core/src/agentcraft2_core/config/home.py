"""The project home: the one folder that can hold everything the SDK reads and writes.

``AC2_HOME`` names it. With it set, the ``.env`` file, the registries, the rules,
the data and the state under ``.agentcraft2/`` (audit, checkpoints, traces, tokens,
Agent Lab, Phoenix) are found there, so a project is one folder to copy, back up or
point an editor at. Without it, each tool keeps its own anchor: the working directory
for the command line, and the folder of a service's own ``.env`` for the service.

It locates the ``.env`` file, so it is read from the process environment only.
"""

from __future__ import annotations

from pathlib import Path

from agentcraft2_core.config.bindings import DEFAULT_BINDINGS, Key
from agentcraft2_core.config.sources import EnvironConfigSource
from agentcraft2_core.contracts import ConfigSource, ConfigurationError

__all__ = ["STATE_FOLDER", "home_variable", "project_home", "state_dir"]

STATE_FOLDER = ".agentcraft2"
"""The folder under the home, or beside a service's ``.env``, that holds generated state."""


def home_variable() -> str:
    """Return the name of the variable that names the home."""
    return next(binding.name for binding in DEFAULT_BINDINGS if binding.key == Key.HOME)


def project_home(source: ConfigSource | None = None) -> Path | None:
    """Return the project home, or ``None`` when ``AC2_HOME`` is not set.

    Args:
        source: Where the variable is read from; by default the process environment.

    Raises:
        ConfigurationError: If it names something that is not a folder. The path
            is not repeated beyond what the person wrote, and a missing folder is
            not created: a typo would otherwise start a second, empty project.
    """
    name = home_variable()
    raw = (source if source is not None else EnvironConfigSource()).get(name)
    if raw is None or not raw.strip():
        return None
    home = Path(raw.strip()).expanduser()
    if not home.is_dir():
        raise ConfigurationError(
            f"{name} does not name a folder",
            expected="an existing folder, which holds the project's .env and state",
            actual=f"{raw.strip()!r}: " + ("not a folder" if home.exists() else "no such folder"),
            fix=f"create the folder, or correct {name}",
        )
    return home.resolve()


def state_dir(beside: Path, source: ConfigSource | None = None) -> Path:
    """Return the folder generated state goes in: under the home if set, else beside ``beside``.

    Args:
        beside: The folder that holds state when there is no home, such as a service's folder.
        source: Where ``AC2_HOME`` is read from; by default the process environment.
    """
    home = project_home(source)
    return (home if home is not None else beside) / STATE_FOLDER

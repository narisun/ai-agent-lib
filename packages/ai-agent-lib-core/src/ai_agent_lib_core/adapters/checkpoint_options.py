"""The options of the SQLite checkpoint store.

They live apart from the store itself, which needs LangGraph, so that the
provider registry can declare and check them without loading it.
"""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core.contracts import OptionsModel

__all__ = ["SqliteCheckpointOptions"]


class SqliteCheckpointOptions(OptionsModel):
    """Options for the SQLite checkpoint backend.

    Attributes:
        path: The database file. Parent directories are created.
    """

    path: Path = Path(".agentlib/checkpoints.sqlite")

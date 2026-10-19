"""Prompt resources of the agent, packaged with it and read independently of the working directory.

A resource is versioned by its file name. A change to one is a new ``_v2`` file next to the old
one, never an edit, so that a running agent never changes under a deployed version.
"""

from __future__ import annotations

from importlib.resources import files

__all__ = ["schema_document"]


def schema_document() -> str:
    """Return the schema document the agent reasons over (``schema_v1.md``).

    The file is rendered, not written: ``python -m tools.build_schema`` in the workspace root
    regenerates it from the data dictionary, the data and ``tools/schema_notes.toml``.
    """
    return files(__package__).joinpath("schema_v1.md").read_text(encoding="utf-8")

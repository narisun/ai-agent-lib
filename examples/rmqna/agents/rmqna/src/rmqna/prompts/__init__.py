"""Prompt resources of the agent, packaged with it and read independently of the working directory.

A resource is versioned by its file name, ``<kind>_v<N>.md``. A change to one is a new ``_v<N+1>``
file next to the old one and a new pin below, never an edit, so that a running agent never changes
under a deployed version. There are three kinds:

``system``
    The instructions: role, scope, the flow (clarify, resolve names, write SQL, repair, re-examine,
    answer) and the DuckDB cheat-sheet with the limits the server enforces.
``schema``
    The schema document. It is rendered, not written: ``python -m tools.build_schema`` in the
    workspace root regenerates it from the data dictionary, the data and ``tools/schema_notes.toml``.
``examples``
    The worked question-to-SQL examples. Every statement in them is run against the real engine
    by the workspace's ``tests/test_examples.py``, so none teaches what the server refuses.

:func:`system_prompt` joins the three into the one static prompt every request carries.

The metric definitions (exposure, utilization, weighted pipeline, covenant breach, cash conversion
cycle, the as-of rule) are written once, in ``tools/schema_notes.toml`` under ``[metrics]``. The
build script renders them into the schema document, and the schema document is part of the prompt
whole, so the prompt carries them without a second copy that could drift. The instructions point
at them as authoritative rather than restating them.
"""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import files

__all__ = [
    "EXAMPLES_VERSION",
    "SCHEMA_VERSION",
    "SYSTEM_VERSION",
    "load_prompt",
    "schema_document",
    "system_prompt",
]

# Pin each resource explicitly. A new file does not silently change a running agent. The server
# serves the schema document from the same file (rmqna_data.settings.SCHEMA_PATH), and the
# workspace's tests fail if the two ever name different documents.
SYSTEM_VERSION = 1
SCHEMA_VERSION = 1
EXAMPLES_VERSION = 1


def load_prompt(kind: str, version: int) -> str:
    """Read ``<kind>_v<version>.md`` from the package; a missing version fails early.

    Raises:
        ValueError: If ``kind`` is not lower-case letters, digits, ``_`` or ``-``, or ``version``
            is not a positive integer, so a name cannot reach outside the package.
        FileNotFoundError: If there is no such version.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", kind) or isinstance(version, bool) or version < 1:
        raise ValueError(
            "Use a prompt kind of lower-case letters, digits, _ or - and a positive integer version"
        )
    return files(__package__).joinpath(f"{kind}_v{version}.md").read_text(encoding="utf-8")


def schema_document() -> str:
    """Return the schema document the agent reasons over (``schema_v1.md``)."""
    return load_prompt("schema", SCHEMA_VERSION)


@cache
def system_prompt() -> str:
    """Return the system prompt: the instructions, then the schema document, then the examples.

    The text is the same on every call, so it is the static prefix of every model request and a
    provider that caches prompt prefixes caches all of it. The schema and the examples are wrapped
    in tags so that their own headings cannot be mistaken for the instructions'.
    """
    return (
        load_prompt("system", SYSTEM_VERSION).rstrip()
        + "\n\n<schema_document>\n"
        + schema_document().rstrip()
        + "\n</schema_document>\n\n<examples>\n"
        + load_prompt("examples", EXAMPLES_VERSION).rstrip()
        + "\n</examples>\n"
    )

"""Schema identifiers: the current spelling, and how to read files written before the rename.

Every versioned document names its format in a ``schema`` field, such as
``agentcraft2.registry/v1``. Before the rename the prefix was the old product name.
Files written then still load: a reader passes the value through ``current_schema``
and compares it with the current identifier. Writers always use the current one.
"""

from __future__ import annotations

__all__ = ["current_schema", "is_schema"]

CURRENT_SCHEMA_PREFIX = "agentcraft2."
LEGACY_SCHEMA_PREFIX = "agentlib."


def current_schema(value: object) -> object:
    """Return a schema identifier in its current spelling.

    A value that is not a string, or does not start with the old prefix, comes back
    unchanged so that the reader's own check still rejects it.

    Args:
        value: What the document's ``schema`` field held.
    """
    if isinstance(value, str) and value.startswith(LEGACY_SCHEMA_PREFIX):
        return CURRENT_SCHEMA_PREFIX + value[len(LEGACY_SCHEMA_PREFIX) :]
    return value


def is_schema(value: object, expected: str) -> bool:
    """Return whether ``value`` names ``expected``, in either spelling of the prefix.

    Args:
        value: What the document's ``schema`` field held.
        expected: The current identifier, such as ``"agentcraft2.registry/v1"``.
    """
    return current_schema(value) == expected

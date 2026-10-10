"""Writing and reading the few TOML values the workspace file holds."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["string_of", "toml_list", "toml_text"]


def toml_text(value: str) -> str:
    """Return ``value`` as a TOML string."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_list(values: Sequence[str]) -> str:
    """Return ``values`` as a TOML array of strings."""
    return "[" + ", ".join(toml_text(value) for value in values) + "]"


def string_of(table: Mapping[str, Any], key: str) -> str:
    """Return the text a parsed table holds under ``key``.

    Raises:
        KeyError: If the key is missing.
        TypeError: If the value is not text.
    """
    value = table[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be text")
    return value

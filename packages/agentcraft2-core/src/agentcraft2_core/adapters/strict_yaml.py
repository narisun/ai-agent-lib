"""Loading YAML that is meant to be plain data.

The safe loader already refuses anything that would build a Python object.
This module also refuses two things YAML allows and that hide mistakes: the
same key written twice in one mapping, where the last one silently wins, and
keys that are not text, which is what the bare words ``on``, ``off``, ``yes``
and ``no`` become.
"""

from __future__ import annotations

from typing import Any

import yaml

__all__ = ["StrictYamlError", "load_strict_yaml"]


class StrictYamlError(ValueError):
    """The text is not YAML, or it is not plain, unambiguous data."""


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses duplicate and non-text mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise StrictYamlError(
                    f"the key {key!r} on line {key_node.start_mark.line + 1} was not read as "
                    "text; YAML treats words such as on, off, yes and no as true or false, so "
                    "put the name in quotes"
                )
            if key in seen:
                raise StrictYamlError(
                    f"the key {key!r} appears twice (line {key_node.start_mark.line + 1})"
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_strict_yaml(text: str) -> object:
    """Return the data ``text`` describes.

    Raises:
        StrictYamlError: If the text is not valid YAML, tries to build an
            object, repeats a key or has a key that is not text.
    """
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError:
        raise StrictYamlError("the text is not valid YAML") from None

"""Adding to a list at the end of a YAML file that a person maintains.

The rules file and the policy samples file belong to the developer: their
comments and their order stay as they are. A command only ever appends to the
list that ends the file, in the layout a person would write by hand, and
checks afterwards that the file still says what it should.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence

from ai_agent_lib_cli.errors import CliError

__all__ = ["extended", "yaml_flow", "yaml_scalar"]

_PLAIN = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]*$")
# Words YAML reads as something other than text.
_YAML_WORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "y", "n"})


def yaml_scalar(value: object) -> str:
    """Return a value as YAML: plain when that is safe, quoted otherwise."""
    if not isinstance(value, str):
        return json.dumps(value)
    if _PLAIN.match(value) and value.lower() not in _YAML_WORDS and not value.endswith(" "):
        return value
    return json.dumps(value, ensure_ascii=False)


def yaml_flow(values: Sequence[object]) -> str:
    """Return values as a YAML list on one line."""
    return "[" + ", ".join(yaml_scalar(value) for value in values) + "]"


def extended(
    text: str,
    *,
    key: str,
    additions: Mapping[str, str],
    ids: Callable[[str], Iterable[str]],
    what: str,
    title: str = "",
) -> str:
    """Return ``text`` with the additions it does not hold appended to its last list.

    An item whose ID is already there is not added again, so the developer's
    version of it stands.

    Args:
        text: The file. Its last key is ``key``, holding a list or ``[]``.
        key: The key of the list.
        additions: The items to add, each written as a block of the list, by ID.
        ids: Reads the IDs of the items a file holds. It raises ``CliError``
            for a file that is not valid.
        what: Names the file, for an error.
        title: Written as a comment above the items that are added.

    Raises:
        CliError: If the file is not valid, or its list is not the last thing
            in it, so that nothing can be appended safely.
    """
    present = set(ids(text))
    missing = {name: block for name, block in additions.items() if name not in present}
    if not missing:
        return text
    empty = f"{key}: []"
    if text.rstrip().endswith(empty):
        text = text.rstrip().removesuffix(empty) + f"{key}:\n"
    blocks = list(missing.values())
    if title:
        blocks[0] = "".join(f"  # {line}\n" for line in title.splitlines()) + blocks[0]
    separator = "" if text.endswith(f"{key}:\n") else "\n"
    result = text.rstrip("\n") + "\n" + separator + "\n".join(blocks)
    try:
        written = set(ids(result))
    except CliError:
        written = set()
    if written != present | set(missing):
        raise CliError(
            f"{what} could not be extended safely: keep '{key}:' as its last key, "
            f"or add these by hand: {', '.join(missing)}"
        )
    return result

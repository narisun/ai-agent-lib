"""Documentation generated from the binding table.

The variable reference and the ``.env.example`` file are produced from the same
table the resolver reads, so they cannot drift from the code.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from ai_agent_lib_core.config.bindings import (
    DEFAULT_BINDINGS,
    SECRET_KEY_PREFIX,
    SECRET_VARIABLE_PREFIX,
    Binding,
    ValueKind,
)

__all__ = [
    "EnvSetting",
    "render_env_example",
    "render_env_file",
    "render_reference",
    "variable_for",
]

_HEADER = "<!-- Generated from the binding table. Do not edit by hand. -->"
_SECRET_PATTERN = f"{SECRET_VARIABLE_PREFIX}<NAME>"
_SECRET_PURPOSE = (
    "A named secret for the env secrets adapter. "
    f"`{SECRET_VARIABLE_PREFIX}RATES_TOKEN` supplies the secret `rates_token`."
)


def _notes(binding: Binding) -> str:
    notes = []
    if binding.kind is ValueKind.JSON:
        notes.append("JSON")
    if binding.sensitive:
        notes.append("sensitive")
    if binding.alternate_names:
        notes.append("also read from " + ", ".join(f"`{n}`" for n in binding.alternate_names))
    if binding.deprecated_names:
        notes.append("replaces " + ", ".join(f"`{n}`" for n in binding.deprecated_names))
    return "; ".join(notes)


def _table(bindings: Sequence[Binding]) -> list[str]:
    lines = ["| Variable | Purpose | Notes |", "| --- | --- | --- |"]
    lines += [f"| `{b.name}` | {b.description} | {_notes(b)} |" for b in bindings]
    return lines


def render_reference(bindings: Sequence[Binding] = DEFAULT_BINDINGS) -> str:
    """Return the variable reference as Markdown."""
    owned = [binding for binding in bindings if binding.owned]
    external = [binding for binding in bindings if not binding.owned]
    lines = [
        _HEADER,
        "",
        "# Configuration variables",
        "",
        "## Variables owned by the library",
        "",
        *_table(owned),
        f"| `{_SECRET_PATTERN}` | {_SECRET_PURPOSE} | sensitive |",
        "",
        "## Variables that belong to other tools",
        "",
        "These keep their own names. The library reads them and never renames them.",
        "",
        *_table(external),
        "",
    ]
    return "\n".join(lines)


def render_env_example(bindings: Sequence[Binding] = DEFAULT_BINDINGS) -> str:
    """Return a commented ``.env.example`` listing every variable."""
    lines = [
        "# Generated from the binding table. Do not edit by hand.",
        "# Copy to .env and uncomment what you need. Unset variables use the profile defaults.",
        "",
    ]
    for binding in bindings:
        lines.append(f"# {binding.description}")
        lines.append(f"# {binding.name}={binding.example}")
        lines.append("")
    lines += [f"# {_SECRET_PURPOSE.replace('`', '')}", f"# {_SECRET_PATTERN}=", ""]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class EnvSetting:
    """One line of a ``.env`` file, named by its logical key.

    Tools that write configuration files, such as the project generator, say
    what they set by key. The variable name comes from the binding table, so a
    renamed variable changes the files they write and none of their code.

    Attributes:
        key: The logical key, for example ``Key.MODEL_PROVIDER`` or
            ``options_key(Section.AUDIT)``.
        value: The value. For a JSON variable, any JSON-like value.
        comment: Lines of explanation written above the setting.
        enabled: When false the setting is written commented out, as a hint.
    """

    key: str
    value: object
    comment: str = ""
    enabled: bool = True


def variable_for(key: str, bindings: Sequence[Binding] = DEFAULT_BINDINGS) -> str:
    """Return the name of the variable that feeds a logical key.

    Raises:
        KeyError: If no variable feeds ``key``.
    """
    for binding in bindings:
        if binding.key == key:
            return binding.name
    if key.startswith(SECRET_KEY_PREFIX):
        return SECRET_VARIABLE_PREFIX + key.removeprefix(SECRET_KEY_PREFIX).upper()
    raise KeyError(f"no variable feeds the key {key!r}")


def _env_value(setting: EnvSetting, bindings: Sequence[Binding]) -> str:
    kind = next((b.kind for b in bindings if b.key == setting.key), ValueKind.TEXT)
    if kind is ValueKind.JSON:
        return json.dumps(setting.value, ensure_ascii=False)
    text = str(setting.value)
    if "\n" in text or "\r" in text:
        raise ValueError(f"the value of {setting.key} must be one line")
    return text


def render_env_file(
    settings: Sequence[EnvSetting],
    *,
    header: str = "",
    bindings: Sequence[Binding] = DEFAULT_BINDINGS,
) -> str:
    """Return the text of a ``.env`` file that makes the given settings.

    Args:
        settings: What to set, in order.
        header: Lines of explanation for the top of the file.
        bindings: The variable table. Defaults to the library's own table.

    Raises:
        KeyError: If a setting names a key no variable feeds.
        ValueError: If a text value spans more than one line.
    """
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    for setting in settings:
        if lines:
            lines.append("")
        lines += [f"# {line}".rstrip() for line in setting.comment.splitlines()]
        assignment = f"{variable_for(setting.key, bindings)}={_env_value(setting, bindings)}"
        lines.append(assignment if setting.enabled else f"# {assignment}")
    return "\n".join(lines) + "\n"

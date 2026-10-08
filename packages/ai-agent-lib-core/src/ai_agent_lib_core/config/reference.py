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
    Key,
    ValueKind,
)

__all__ = [
    "EnvSetting",
    "default_text",
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


def _profile_defaults() -> dict[str, dict[str, str]]:
    """What resolving an empty configuration gives under each profile, by key."""
    from ai_agent_lib_core.config.resolver import ConfigResolver
    from ai_agent_lib_core.config.sources import MappingConfigSource
    from ai_agent_lib_core.contracts import Profile

    found: dict[str, dict[str, str]] = {}
    for profile in Profile:
        source = MappingConfigSource({variable_for(Key.PROFILE): profile.value})
        for key, value in ConfigResolver(source).explain().shown.items():
            found.setdefault(key, {})[profile.value] = value
    return found


def default_text(binding: Binding) -> str:
    """Say what applies when ``binding``'s variable is not set, in Markdown.

    Defaults chosen by the profile are read by resolving an empty
    configuration, so the text cannot drift from the resolver.
    """
    if binding.key == Key.PROFILE:
        return f"`{_profile_defaults()[Key.PROFILE]['local']}`"
    by_profile = _profile_defaults().get(binding.key)
    if by_profile:
        values = set(by_profile.values())
        if len(values) == 1:
            return f"`{values.pop()}`"
        return ", ".join(f"{profile}: `{value}`" for profile, value in by_profile.items())
    return binding.default or "not set"


def _table(bindings: Sequence[Binding]) -> list[str]:
    lines = ["| Variable | Purpose | Default | Notes |", "| --- | --- | --- | --- |"]
    lines += [
        f"| `{b.name}` | {b.description}{'<br>' + b.details if b.details else ''} "
        f"| {default_text(b)} | {_notes(b)} |"
        for b in bindings
    ]
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
        f"| `{_SECRET_PATTERN}` | {_SECRET_PURPOSE} | not set | sensitive |",
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
        lines.append(f"# Default: {default_text(binding).replace('`', '')}")
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

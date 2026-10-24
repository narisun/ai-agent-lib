"""Documentation generated from the binding table.

The variable reference and the ``.env.example`` file are produced from the same
table the resolver reads, so they cannot drift from the code.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass

from agentcraft2_core.config.bindings import (
    DEFAULT_BINDINGS,
    LEGACY_PREFIX,
    SECRET_KEY_PREFIX,
    SECRET_VARIABLE_PREFIX,
    Binding,
    Key,
    ValueKind,
    legacy_spelling,
    provider_key,
)
from agentcraft2_core.config.phoenix import PHOENIX_VARIABLES, phoenix_env_example
from agentcraft2_core.contracts import Profile, Section

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
_LOCAL_FIRST = (
    "With nothing set, every provider is local: files, SQLite and in-process engines, with no "
    "cloud infrastructure (the model's local default is `anthropic`, a hosted API; use "
    "`AC2_MODEL_PROVIDER=fake` to stay fully offline). `AC2_PROFILE=aws` switches every section "
    "to its AWS provider, and any single `AC2_<SECTION>_PROVIDER` opts one section in. A few "
    "local adapters are local only: the one-way guard (`AC2_DEPLOYMENT_ENV`) refuses them "
    "anywhere but local development."
)
_RENAME_NOTE = (
    f"Variables were previously spelled `{LEGACY_PREFIX}*`; the old spellings keep working for "
    "one minor release and warn."
)
_PROFILE_TABLE_INTRO = (
    "What each profile selects when nothing else is set. `AC2_MODEL_PROVIDER` and each "
    "`AC2_<SECTION>_PROVIDER` override one row."
)
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
    # The legacy spelling of every owned variable is stated once, in the preamble.
    legacy = legacy_spelling(binding.name) if binding.owned else None
    replaced = [n for n in binding.deprecated_names if n != legacy]
    if replaced:
        notes.append("replaces " + ", ".join(f"`{n}`" for n in replaced))
    return "; ".join(notes)


def _profile_defaults() -> dict[str, dict[str, str]]:
    """What resolving an empty configuration gives under each profile, by key."""
    from agentcraft2_core.config.resolver import ConfigResolver
    from agentcraft2_core.config.sources import MappingConfigSource

    found: dict[str, dict[str, str]] = {}
    # AgentCore Runtime starts from the AWS defaults, so it has no column of its own.
    for profile in (Profile.LOCAL, Profile.AWS):
        source = MappingConfigSource(
            {
                variable_for(Key.PROFILE): profile.value,
                # A server profile has no default environment; this only reads the defaults.
                variable_for(Key.DEPLOYMENT_ENV): "dev"
                if profile is not Profile.LOCAL
                else "local",
            }
        )
        for key, value in ConfigResolver(source).explain().shown.items():
            found.setdefault(key, {})[profile.value] = value
    # The environment above only let the defaults be read; its own default is stated in words.
    found.pop(Key.DEPLOYMENT_ENV, None)
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


def _profile_table() -> list[str]:
    """One row per section, and the model, with what each profile selects."""
    found = _profile_defaults()
    rows = [("model", Key.MODEL_PROVIDER), *((s.value, provider_key(s)) for s in Section)]
    lines = ["| Section | Local | AWS |", "| --- | --- | --- |"]
    lines += [
        f"| {label} | `{found[key][Profile.LOCAL.value]}` | `{found[key][Profile.AWS.value]}` |"
        for label, key in rows
    ]
    return lines


def render_reference(bindings: Sequence[Binding] = DEFAULT_BINDINGS) -> str:
    """Return the variable reference as Markdown."""
    owned = [binding for binding in bindings if binding.owned]
    external = [binding for binding in bindings if not binding.owned]
    lines = [_HEADER, "", "# Configuration variables", ""]
    if bindings is DEFAULT_BINDINGS:
        lines += [
            _LOCAL_FIRST,
            "",
            _RENAME_NOTE,
            "",
            "## Profile defaults",
            "",
            _PROFILE_TABLE_INTRO,
            "",
            *_profile_table(),
            "",
        ]
    lines += [
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
    if bindings is DEFAULT_BINDINGS:
        lines += [
            "## Optional Phoenix integration",
            "",
            "These settings are resolved by `load_phoenix_settings` separately from "
            "`ServiceConfig`. See [Trace agents with Phoenix](phoenix-trial.md) for "
            "purposes, defaults, precedence and the separate SQLite server setup.",
            "",
            *[f"- `{name}`" for name in PHOENIX_VARIABLES],
            "",
        ]
    return "\n".join(lines)


def render_env_example(bindings: Sequence[Binding] = DEFAULT_BINDINGS) -> str:
    """Return a commented ``.env.example`` listing every variable."""
    lines = [
        "# Generated from the binding table. Do not edit by hand.",
        "# With nothing set, every provider is local: no cloud infrastructure. AC2_PROFILE=aws",
        "# or one AC2_<SECTION>_PROVIDER opts into AWS. Copy to .env and uncomment what you need.",
    ]
    if bindings is DEFAULT_BINDINGS:
        # The rename belongs to the library's own table, not to a custom one.
        lines += (f"# {line}" for line in textwrap.wrap(_RENAME_NOTE.replace("`", ""), width=96))
    lines.append("")
    for binding in bindings:
        lines.append(f"# {binding.description}")
        lines.append(f"# Default: {default_text(binding).replace('`', '')}")
        lines.append(f"# {binding.name}={binding.example}")
        lines.append("")
    lines += [f"# {_SECRET_PURPOSE.replace('`', '')}", f"# {_SECRET_PATTERN}=", ""]
    return "\n".join(lines) + (phoenix_env_example() if bindings is DEFAULT_BINDINGS else "")


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

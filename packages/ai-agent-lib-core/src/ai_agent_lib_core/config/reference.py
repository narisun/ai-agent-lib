"""Documentation generated from the binding table.

The variable reference and the ``.env.example`` file are produced from the same
table the resolver reads, so they cannot drift from the code.
"""

from __future__ import annotations

from collections.abc import Sequence

from ai_agent_lib_core.config.bindings import (
    DEFAULT_BINDINGS,
    SECRET_VARIABLE_PREFIX,
    Binding,
    ValueKind,
)

__all__ = ["render_env_example", "render_reference"]

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

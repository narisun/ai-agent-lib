"""What 'agentlib config options' prints: the adapters, and each one's options."""

from __future__ import annotations

import json
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic.json_schema import GenerateJsonSchema

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_core.config import Key, options_key, variable_for
from ai_agent_lib_core.contracts import Limits, NoOptions, Section
from ai_agent_lib_core.contracts.options_help import options_help, options_summary
from ai_agent_lib_core.di import DATA_PORT, MODEL_PORT, ServiceProviders

__all__ = ["LIMITS", "PORTS", "adapter_lines", "limit_lines", "option_lines", "options_schema"]

PORTS: tuple[str, ...] = (MODEL_PORT, *(section.value for section in Section), DATA_PORT)
"""Every port, in the order the developer guide lists them."""

_WIDTH = 96


def _plain(text: str) -> str:
    """Drop the docstring markup around literals: ``x`` reads as x on a terminal."""
    return text.replace("``", "")


def _where(port: str) -> str:
    if port == DATA_PORT:
        return f'beside "kind" in each entry of {variable_for(Key.DATA_SOURCES)}'
    if port == MODEL_PORT:
        return "nowhere: model providers take no options"
    return f"as one JSON object in {variable_for(options_key(Section(port)))}"


def _known_port(port: str) -> str:
    if port not in PORTS:
        raise CliError(
            f"there is no port called {port!r}",
            expected="one of: " + ", ".join(PORTS),
            actual=repr(port),
        )
    return port


def adapter_lines(providers: ServiceProviders, ports: Sequence[str] = PORTS) -> list[str]:
    """List the adapters of each port, marking the local-only ones."""
    lines: list[str] = []
    for port in (_known_port(p) for p in ports):
        lines.append(f"{port}")
        for name in providers.names(port):
            spec = providers.lookup(port, name)
            notes = []
            if spec.local_only:
                notes.append("local only")
            if spec.options is NoOptions:
                notes.append("no options")
            elif spec.options is not None:
                count = len(options_help(spec.options))
                notes.append(f"{count} option{'s' if count != 1 else ''}")
            lines.append(f"  {name:<16} {', '.join(notes)}".rstrip())
    lines.append("")
    lines.append("Show one adapter's options: agentlib config options PORT NAME")
    lines.append("Show the timeouts, retries and budgets: agentlib config options limits")
    return lines


LIMITS = "limits"
"""Not a port: the timeouts, retries and budgets, which have options of their own."""


def limit_lines() -> list[str]:
    """Describe the keys of the limits variable."""
    lines = [
        f"limits: {_plain(options_summary(Limits))}",
        f"Set them as one JSON object in {variable_for(Key.LIMITS)}; a key left out keeps "
        "its default.",
        "",
    ]
    for option in options_help(Limits):
        default = "keys below" if option.group else f"default {option.default or 'not set'}"
        lines.append(f"  {option.name}  ({option.type}; {default})")
        if option.meaning:
            lines += textwrap.wrap(
                _plain(option.meaning), _WIDTH, initial_indent="      ", subsequent_indent="      "
            )
    return lines


def option_lines(providers: ServiceProviders, port: str, name: str) -> list[str]:
    """Describe one adapter's options: type, default and meaning of each."""
    spec = providers.lookup(_known_port(port), name)
    guard = " (local only)" if spec.local_only else ""
    if spec.options is None:
        return [f"{port} {name}{guard}: this adapter does not declare its options"]
    if spec.options is NoOptions:
        return [f"{port} {name}{guard}: no options"]
    lines = [
        f"{port} {name}{guard}: {options_summary(spec.options)}",
        f"Set them {_where(port)}.",
        "",
    ]
    for option in options_help(spec.options):
        if option.required:
            default = "required"
        elif option.group and option.default is None:
            default = "keys below"
        else:
            default = f"default {option.default or 'not set'}"
        lines.append(f"  {option.name}  ({option.type}; {default})")
        if option.meaning:
            lines += textwrap.wrap(
                _plain(option.meaning), _WIDTH, initial_indent="      ", subsequent_indent="      "
            )
    return lines


def options_schema(providers: ServiceProviders, port: str, name: str) -> str:
    """Return one adapter's options as JSON Schema text."""
    spec = providers.lookup(_known_port(port), name)
    if spec.options is None:
        raise CliError(f"{port} {name} does not declare its options, so there is no schema")
    return json.dumps(
        spec.options.model_json_schema(schema_generator=_OptionsSchema), indent=2, sort_keys=True
    )


class _OptionsSchema(GenerateJsonSchema):
    """Serialize concrete OS path defaults identically on Windows and Unix."""

    def encode_default(self, dft: Any) -> Any:
        if isinstance(dft, Path):
            return dft.as_posix()
        return super().encode_default(dft)

"""What the optional tracing backends share: origin validation and layered resolution.

Langfuse and Phoenix settings are resolved the same way. The process environment
wins over an optional separate settings file, which wins over the agent's dotenv
file; an explicit ``false`` in the process environment skips the separate file.
Both need a bare HTTPS origin (HTTP for loopback), true/false flags, and masked
credentials when they explain where each value came from.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from ai_agent_lib_core.config.sources import (
    DotenvConfigSource,
    EnvironConfigSource,
    MappingConfigSource,
)
from ai_agent_lib_core.contracts import ConfigSource, ConfigurationError

__all__ = [
    "IDENTIFIER",
    "MASKED",
    "Layer",
    "Lookup",
    "ResolvedVariables",
    "read_variable",
    "resolve_variables",
    "tracing_layers",
    "validate_origin",
]

IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
"""A project identifier that is safe inside a URL path."""

MASKED = "********"
"""What a configured credential is shown as."""

_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f<>\\()]")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_origin(url: str, *, variable: str) -> None:
    """Refuse anything but a bare origin: HTTPS anywhere, HTTP only for loopback.

    Args:
        url: The configured value.
        variable: The variable it was read from, named in the error.

    Raises:
        ConfigurationError: If ``url`` carries a path, query, fragment or
            credentials, contains control or delimiter characters, is not
            HTTPS (or HTTP to a loopback host), or cannot be parsed.
    """
    expected = "https://host[:port], or http://localhost[:port], with nothing after the port"
    fix = f"set {variable} to the trace server's origin only"
    try:
        parts = urlsplit(url)
        # Accessing port also validates malformed or out-of-range port numbers.
        _ = parts.port
    except ValueError as error:
        raise ConfigurationError(
            f"{variable} must be a valid origin", expected=expected, fix=fix
        ) from error
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or _UNSAFE.search(url)
        or not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK))
    ):
        raise ConfigurationError(
            f"{variable} must be an HTTPS origin (HTTP for loopback)", expected=expected, fix=fix
        )


Lookup = Callable[[ConfigSource, str], "tuple[str, str] | None"]
"""Read one variable from one layer: ``(value, name actually read)``, or ``None``."""


def read_variable(source: ConfigSource, name: str) -> tuple[str, str] | None:
    """The plain lookup: the variable under its own name."""
    value = source.get(name)
    return None if value is None else (value, name)


@dataclass(frozen=True)
class Layer:
    """One configuration layer and how its values are described in an explanation."""

    origin: str
    source: ConfigSource


def tracing_layers(
    dotenv_path: Path | None,
    *,
    source: ConfigSource | None,
    environment: ConfigSource | None,
    working_dir: Path | None,
    enabled: str,
    env_file: str,
    lookup: Lookup = read_variable,
    caller: str,
) -> tuple[Layer, ...]:
    """Order the layers: process environment, optional separate file, service dotenv.

    ``source`` replaces every layer, for tests and tools that hold a complete
    configuration. ``environment`` replaces the process environment, for a
    launcher planning a child process. A separate file named in the process
    environment is relative to ``working_dir`` when one is given; a file named in
    the dotenv file is relative to that file. An explicit ``false`` for ``enabled``
    in the process layer skips the separate file, so disabling tracing never
    requires that file to exist.

    Args:
        dotenv_path: The agent's dotenv file, or ``None`` for no service layer.
        source: A complete configuration that bypasses file layering.
        environment: The process environment to use instead of the real one.
        working_dir: Where a process-supplied separate file path is relative to.
        enabled: The variable that turns the backend on.
        env_file: The variable that names the separate settings file.
        lookup: How a variable is read from a layer, for migration aliases.
        caller: The public function named when both overrides are supplied.

    Raises:
        ValueError: If both ``source`` and ``environment`` are supplied.
    """
    if source is not None and environment is not None:
        raise ValueError(f"Supply either source or environment to {caller}")
    if source is not None:
        return (Layer("injected source", source),)

    def value(layer: ConfigSource, name: str) -> str:
        found = lookup(layer, name)
        return found[0] if found else ""

    process = environment if environment is not None else EnvironConfigSource()
    service = DotenvConfigSource(dotenv_path) if dotenv_path else MappingConfigSource({})
    layers = [Layer("process environment", process)]
    # An explicit process disable must also bypass a missing separate file.
    if value(process, enabled).lower() != "false":
        extra = value(process, env_file)
        if extra and working_dir is not None:
            extra = str(working_dir / extra)
        if not extra and (extra := value(service, env_file)):
            extra = str((dotenv_path.parent if dotenv_path else Path.cwd()) / extra)
        if extra:
            path = Path(extra)
            layers.append(Layer(str(path.resolve()), DotenvConfigSource(path, required=True)))
    layers.append(Layer(str(dotenv_path.resolve()) if dotenv_path else "service settings", service))
    return tuple(layers)


@dataclass(frozen=True)
class ResolvedVariables:
    """Each variable's resolved text and which layer (or default) supplied it."""

    values: Mapping[str, str]
    origins: Mapping[str, str]

    def flag(self, name: str) -> bool:
        """Parse a true/false variable; an unset or empty value is ``false``.

        Raises:
            ConfigurationError: If the value is anything but ``true`` or ``false``.
        """
        raw = self.values[name] or "false"
        if raw.lower() not in {"true", "false"}:
            raise ConfigurationError(
                f"{name} must be true or false",
                expected="true or false",
                fix=f"set {name} to true or false, or leave it unset",
            )
        return raw.lower() == "true"

    def shown(self, secrets: Iterable[str]) -> Mapping[str, str]:
        """Return the values with every configured credential masked."""
        hidden = set(secrets)
        return MappingProxyType(
            {
                name: MASKED if name in hidden and value else value
                for name, value in self.values.items()
            }
        )


def resolve_variables(
    layers: Iterable[Layer],
    names: Iterable[str],
    *,
    defaults: Mapping[str, str],
    lookup: Lookup = read_variable,
) -> ResolvedVariables:
    """Take each variable from the first layer that has it, else from ``defaults``.

    A variable missing from ``defaults`` resolves to the empty string. Origins
    read ``"<layer origin>: <name read>"`` or ``"default"``.
    """
    ordered = tuple(layers)
    values: dict[str, str] = {}
    origins: dict[str, str] = {}
    for name in names:
        for layer in ordered:
            if (found := lookup(layer.source, name)) is not None:
                values[name], origins[name] = found[0], f"{layer.origin}: {found[1]}"
                break
        else:
            values[name], origins[name] = defaults.get(name, ""), "default"
    return ResolvedVariables(MappingProxyType(values), MappingProxyType(origins))

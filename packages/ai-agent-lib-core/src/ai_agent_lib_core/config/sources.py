"""Configuration sources: where raw name and value pairs come from.

This is the only module in the library that reads the process environment.
Every source takes a snapshot when it is created, so configuration cannot
change underneath a running process.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

from dotenv import dotenv_values

from ai_agent_lib_core.contracts import ConfigSource, ConfigurationError

__all__ = [
    "DotenvConfigSource",
    "EnvironConfigSource",
    "LayeredConfigSource",
    "MappingConfigSource",
]


class MappingConfigSource:
    """A source backed by an in-memory mapping. Used directly in tests."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values: Mapping[str, str] = MappingProxyType(dict(values))

    def get(self, name: str) -> str | None:
        """Return the raw value for ``name``, or ``None`` when it is not set."""
        return self._values.get(name)

    def names(self) -> Iterable[str]:
        """Return every name the source holds."""
        return tuple(self._values)


class EnvironConfigSource(MappingConfigSource):
    """A snapshot of the process environment, taken at construction."""

    def __init__(self) -> None:
        super().__init__(os.environ)


class DotenvConfigSource(MappingConfigSource):
    """Values from a ``.env`` file. The process environment is not modified.

    Args:
        path: The file to read.
        required: When false, a missing file gives an empty source.

    Raises:
        ConfigurationError: If the file is required and missing, or cannot be read.
    """

    def __init__(self, path: Path, *, required: bool = False) -> None:
        if not path.is_file():
            if required:
                raise ConfigurationError(f"configuration file not found: {path}")
            super().__init__({})
            return
        try:
            parsed = dotenv_values(path, interpolate=False, encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigurationError(f"configuration file cannot be read: {path}") from exc
        super().__init__({name: value for name, value in parsed.items() if value is not None})


class LayeredConfigSource:
    """Several sources read in order; the first one that has a name wins."""

    def __init__(self, *layers: ConfigSource) -> None:
        if not layers:
            raise ValueError("at least one layer is required")
        self._layers = layers

    def get(self, name: str) -> str | None:
        """Return the value from the first layer that defines ``name``."""
        for layer in self._layers:
            value = layer.get(name)
            if value is not None:
                return value
        return None

    def names(self) -> Iterable[str]:
        """Return the names from every layer, without duplicates."""
        return tuple(dict.fromkeys(name for layer in self._layers for name in layer.names()))

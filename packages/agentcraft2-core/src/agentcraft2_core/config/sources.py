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

from agentcraft2_core.contracts import ConfigSource, ConfigurationError

__all__ = [
    "DotenvConfigSource",
    "EnvironConfigSource",
    "LayeredConfigSource",
    "MappingConfigSource",
]


class MappingConfigSource:
    """A source backed by an in-memory mapping. Used directly in tests.

    Args:
        values: The names and values.
        base_dir: The folder a relative path in these values is relative to; ``None``
            leaves that to the resolver's own.
    """

    def __init__(self, values: Mapping[str, str], *, base_dir: Path | None = None) -> None:
        self._values: Mapping[str, str] = MappingProxyType(dict(values))
        self._base_dir = base_dir

    def get(self, name: str) -> str | None:
        """Return the raw value for ``name``, or ``None`` when it is not set."""
        return self._values.get(name)

    def base_dir_for(self, name: str) -> Path | None:
        """Return the folder a relative path in the value of ``name`` is relative to, if known."""
        return self._base_dir if name in self._values else None

    def names(self) -> Iterable[str]:
        """Return every name the source holds."""
        return tuple(self._values)


class EnvironConfigSource(MappingConfigSource):
    """A snapshot of the process environment, taken at construction.

    Args:
        base_dir: The folder a relative path in these values is relative to.
    """

    def __init__(self, *, base_dir: Path | None = None) -> None:
        super().__init__(os.environ, base_dir=base_dir)


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
        super().__init__(
            {name: value for name, value in parsed.items() if value is not None},
            base_dir=path.resolve().parent,
        )


class LayeredConfigSource:
    """Several sources read in order; the first one that has a value for a name wins.

    A blank value is not a value. A compose file that passes ``NAME=${NAME}``
    from an empty variable would otherwise hide the setting in ``.env`` behind
    it, and the resolver would then read the name as unset.

    Args:
        layers: The sources, the strongest first.
        blanks_count: Whether a blank value is a value that hides the layers
            behind it. A launcher that builds a child's environment sets this,
            because a variable the shell set to empty must reach the child empty.
    """

    def __init__(self, *layers: ConfigSource, blanks_count: bool = False) -> None:
        if not layers:
            raise ValueError("at least one layer is required")
        self._layers = layers
        self._blanks_count = blanks_count

    def get(self, name: str) -> str | None:
        """Return the value from the first layer that holds ``name``, ignoring blanks."""
        layer = self._supplying(name)
        return layer.get(name) if layer is not None else None

    def base_dir_for(self, name: str) -> Path | None:
        """Return the folder a relative path in the value of ``name`` is relative to, if known.

        It is the folder of the layer that supplies the value: a path written in a service's
        ``.env`` is relative to that file, whatever else is layered above or below it.
        """
        layer = self._supplying(name)
        finder = getattr(layer, "base_dir_for", None)
        return finder(name) if callable(finder) else None

    def origin_of(self, name: str) -> str | None:
        """Say where the value of ``name`` was written, when the layer that supplies it knows."""
        layer = self._supplying(name)
        finder = getattr(layer, "origin_of", None)
        return finder(name) if callable(finder) else None

    def _supplying(self, name: str) -> ConfigSource | None:
        for layer in self._layers:
            value = layer.get(name)
            if value is not None and (self._blanks_count or value.strip()):
                return layer
        return None

    def names(self) -> Iterable[str]:
        """Return the names from every layer, without duplicates."""
        return tuple(dict.fromkeys(name for layer in self._layers for name in layer.names()))

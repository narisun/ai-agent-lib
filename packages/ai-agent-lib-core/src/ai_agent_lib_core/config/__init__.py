"""Configuration: the only package that knows a variable name.

Raw values come from a :class:`~ai_agent_lib_core.contracts.ConfigSource`. The
binding table maps variable names to logical keys, and the resolver turns them
into an immutable :class:`~ai_agent_lib_core.contracts.ServiceConfig`. Every
other package receives typed values and never sees a variable name.
"""

from pathlib import Path

from ai_agent_lib_core.config.bindings import DEFAULT_BINDINGS, PREFIX, Binding, ValueKind
from ai_agent_lib_core.config.profiles import PROFILE_DEFAULTS, ProfileDefaults
from ai_agent_lib_core.config.reference import render_env_example, render_reference
from ai_agent_lib_core.config.resolver import ConfigResolver, Resolution
from ai_agent_lib_core.config.sources import (
    DotenvConfigSource,
    EnvironConfigSource,
    LayeredConfigSource,
    MappingConfigSource,
)
from ai_agent_lib_core.contracts import ConfigSource, ServiceConfig

__all__ = [
    "DEFAULT_BINDINGS",
    "PREFIX",
    "PROFILE_DEFAULTS",
    "Binding",
    "ConfigResolver",
    "DotenvConfigSource",
    "EnvironConfigSource",
    "LayeredConfigSource",
    "MappingConfigSource",
    "ProfileDefaults",
    "Resolution",
    "ValueKind",
    "load_service_config",
    "render_env_example",
    "render_reference",
]


def load_service_config(dotenv_path: Path | None = Path(".env")) -> ServiceConfig:
    """Resolve configuration from the process environment and a ``.env`` file.

    The process environment wins over the file. A missing file is not an error.

    Args:
        dotenv_path: The ``.env`` file to read, or ``None`` to read only the
            process environment.
    """
    layers: list[ConfigSource] = [EnvironConfigSource()]
    if dotenv_path is not None:
        layers.append(DotenvConfigSource(dotenv_path))
    return ConfigResolver(LayeredConfigSource(*layers)).resolve()

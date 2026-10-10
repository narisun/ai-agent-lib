"""Configuration: the only package that knows a variable name.

Raw values come from a :class:`~ai_agent_lib_core.contracts.ConfigSource`. The
binding table maps variable names to logical keys, and the resolver turns them
into an immutable :class:`~ai_agent_lib_core.contracts.ServiceConfig`. Every
other package receives typed values and never sees a variable name.
"""

from pathlib import Path

from ai_agent_lib_core.config.bindings import (
    DEFAULT_BINDINGS,
    PREFIX,
    Binding,
    Key,
    ValueKind,
    options_key,
    provider_key,
    secret_key,
)
from ai_agent_lib_core.config.profiles import PROFILE_DEFAULTS, ProfileDefaults
from ai_agent_lib_core.config.reference import (
    EnvSetting,
    render_env_example,
    render_env_file,
    render_reference,
    variable_for,
)
from ai_agent_lib_core.config.resolver import MASKED, ConfigResolver, Resolution
from ai_agent_lib_core.config.sources import (
    DotenvConfigSource,
    EnvironConfigSource,
    LayeredConfigSource,
    MappingConfigSource,
)
from ai_agent_lib_core.config.telemetry import telemetry_enabled
from ai_agent_lib_core.contracts import ConfigSource, ServiceConfig

__all__ = [
    "DEFAULT_BINDINGS",
    "MASKED",
    "PREFIX",
    "PROFILE_DEFAULTS",
    "Binding",
    "ConfigResolver",
    "DotenvConfigSource",
    "EnvSetting",
    "EnvironConfigSource",
    "Key",
    "LayeredConfigSource",
    "MappingConfigSource",
    "ProfileDefaults",
    "Resolution",
    "ValueKind",
    "load_service_config",
    "options_key",
    "provider_key",
    "render_env_example",
    "render_env_file",
    "render_reference",
    "secret_key",
    "service_resolver",
    "telemetry_enabled",
    "variable_for",
]


def load_service_config(dotenv_path: Path | None = Path(".env")) -> ServiceConfig:
    """Resolve configuration from the process environment and a ``.env`` file.

    The process environment wins over the file. A missing file is not an error.
    When the file exists, a relative path in any value is relative to the
    file's folder, so a service finds its files whatever directory it is
    started from.

    Args:
        dotenv_path: The ``.env`` file to read, or ``None`` to read only the
            process environment.
    """
    return service_resolver(dotenv_path).resolve()


def service_resolver(dotenv_path: Path | None = Path(".env")) -> ConfigResolver:
    """Return the resolver a service's configuration comes from.

    ``resolve()`` on it gives the configuration; ``explain()`` also says which
    variable or default supplied each setting.
    """
    layers: list[ConfigSource] = [EnvironConfigSource()]
    base_dir: Path | None = None
    if dotenv_path is not None:
        layers.append(DotenvConfigSource(dotenv_path))
        if dotenv_path.is_file():
            base_dir = dotenv_path.resolve().parent
    return ConfigResolver(LayeredConfigSource(*layers), base_dir=base_dir)

"""Configuration: the only package that knows a variable name.

Raw values come from a :class:`~agentcraft2_core.contracts.ConfigSource`. The
binding table maps variable names to logical keys, and the resolver turns them
into an immutable :class:`~agentcraft2_core.contracts.ServiceConfig`. Every
other package receives typed values and never sees a variable name.
"""

from pathlib import Path

from agentcraft2_core.config.bindings import (
    DEFAULT_BINDINGS,
    LEGACY_PREFIX,
    PREFIX,
    Binding,
    Key,
    ValueKind,
    options_key,
    provider_key,
    secret_key,
)
from agentcraft2_core.config.deprecations import log_deprecation, log_resolution_warnings
from agentcraft2_core.config.home import (
    STATE_FOLDER,
    home_variable,
    project_home,
    state_dir,
)
from agentcraft2_core.config.profiles import PROFILE_DEFAULTS, ProfileDefaults
from agentcraft2_core.config.reference import (
    EnvSetting,
    render_env_example,
    render_env_file,
    render_reference,
    variable_for,
)
from agentcraft2_core.config.resolver import MASKED, ConfigResolver, Resolution
from agentcraft2_core.config.sources import (
    DotenvConfigSource,
    EnvironConfigSource,
    LayeredConfigSource,
    MappingConfigSource,
)
from agentcraft2_core.config.telemetry import telemetry_enabled
from agentcraft2_core.contracts import ConfigSource, ConfigurationError, ServiceConfig

__all__ = [
    "DEFAULT_BINDINGS",
    "DEFAULT_DOTENV",
    "LEGACY_PREFIX",
    "MASKED",
    "PREFIX",
    "PROFILE_DEFAULTS",
    "STATE_FOLDER",
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
    "log_deprecation",
    "log_resolution_warnings",
    "options_key",
    "project_home",
    "provider_key",
    "render_env_example",
    "render_env_file",
    "render_reference",
    "secret_key",
    "service_resolver",
    "state_dir",
    "telemetry_enabled",
    "variable_for",
]


DEFAULT_DOTENV = Path(".env")
"""The ``.env`` file read when none is named: the project home's, or the working directory's."""


def load_service_config(dotenv_path: Path | None = DEFAULT_DOTENV) -> ServiceConfig:
    """Resolve configuration from the process environment and a ``.env`` file.

    The process environment wins over the file. A missing file is not an error.
    When the file exists, a relative path in a value it holds is relative to the
    file's folder, so a service finds its files whatever directory it is
    started from.

    With ``AC2_HOME`` set, the home's ``.env`` is read too, below the file named
    here, and the home is where a relative path in the process environment, and the
    state a service keeps by default, are found.

    Args:
        dotenv_path: The ``.env`` file to read, or ``None`` to read only the
            process environment. By default the home's, or the working directory's.
    """
    return service_resolver(dotenv_path).resolve()


def service_resolver(dotenv_path: Path | None = DEFAULT_DOTENV) -> ConfigResolver:
    """Return the resolver a service's configuration comes from.

    ``resolve()`` on it gives the configuration; ``explain()`` also says which
    variable or default supplied each setting.
    """
    home = project_home()
    if dotenv_path is DEFAULT_DOTENV and home is not None:
        dotenv_path = home / ".env"
    layers: list[ConfigSource] = [EnvironConfigSource(base_dir=home)]
    base_dir = home
    files: list[Path] = []
    if dotenv_path is not None:
        files.append(dotenv_path)
        if dotenv_path.is_file() and base_dir is None:
            base_dir = dotenv_path.resolve().parent
    if home is not None and not any(_same_file(home / ".env", file) for file in files):
        files.append(home / ".env")
    for file in files:
        source = DotenvConfigSource(file)
        if home_variable() in set(source.names()):
            raise ConfigurationError(
                f"{home_variable()} is set in {file}, and it locates that file",
                expected="the variable in the process environment",
                fix=f"remove it from {file} and set it in the shell that starts the service",
            )
        layers.append(source)
    return ConfigResolver(LayeredConfigSource(*layers), base_dir=base_dir)


def _same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()

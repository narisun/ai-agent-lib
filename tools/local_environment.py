"""Load launcher overrides for the agent without changing the supervisor's environment."""

from pathlib import Path

from tools.local_processes import report


def agent_environment(
    service_env: Path, overrides: Path, *, required: bool, working_dir: Path | None = None
) -> dict[str, str]:
    """Merge shell > launcher file > service defaults, keeping secrets in the child.

    Service defaults stay in the service's own file. Relative configuration paths
    therefore keep the same base directory as a direct ``python -m`` launch.
    Imports are lazy so batch help and first-time setup need only standard Python.
    """
    from ai_agent_lib_core.config import (
        ConfigResolver,
        DotenvConfigSource,
        EnvironConfigSource,
        LayeredConfigSource,
        MappingConfigSource,
        log_resolution_warnings,
    )
    from ai_agent_lib_core.config.phoenix import resolve_phoenix_settings
    from ai_agent_lib_core.contracts import ConfigurationError
    from tools.local_config_report import report_environment

    try:
        shell = EnvironConfigSource()
        launcher = DotenvConfigSource(overrides, required=required)
        service = DotenvConfigSource(service_env)
        source = LayeredConfigSource(shell, launcher)
        environment = {
            name: value for name in source.names() if (value := source.get(name)) is not None
        }
        resolution = ConfigResolver(
            LayeredConfigSource(MappingConfigSource(environment), service),
            base_dir=service_env.parent,
        ).explain()
        # Python hides DeprecationWarning; the launcher is where an operator first looks.
        log_resolution_warnings(resolution)
        tracing = resolve_phoenix_settings(
            service_env, environment=MappingConfigSource(environment), working_dir=working_dir
        )
    except ConfigurationError as error:
        raise ValueError(str(error)) from error
    if overrides.is_file():
        report(f"Agent environment file: {overrides}")
    else:
        report(f"Agent settings: {service_env} (no repository-root .env)")
    report(f"Agent service defaults: {service_env}")
    config = resolution.config
    report(
        f"Agent model: {config.model.provider} / {config.model.model_id or '(provider default)'}"
    )
    report_environment(
        resolution,
        (("shell", shell), ("launcher .env", launcher), ("service .env", service)),
        tracing=tracing,
    )
    return environment

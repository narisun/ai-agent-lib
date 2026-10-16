"""Resolve optional agent tracing consistently for runtime, launchers and diagnostics."""

from dataclasses import dataclass
from pathlib import Path

from ai_agent_lib_core.config.langfuse import LangfuseResolution, resolve_langfuse_settings
from ai_agent_lib_core.config.phoenix import PhoenixResolution, resolve_phoenix_settings
from ai_agent_lib_core.contracts import ConfigSource, ConfigurationError


@dataclass(frozen=True)
class TracingResolution:
    """Each backend's settings and safe diagnostic values."""

    langfuse: LangfuseResolution
    phoenix: PhoenixResolution

    def validate(self) -> None:
        """One request exports to at most one agent trace destination."""
        if self.langfuse.settings is not None and self.phoenix.settings is not None:
            raise ConfigurationError("Enable one agent trace destination: Phoenix or Langfuse")


def resolve_tracing_settings(
    dotenv_path: Path | None = Path(".env"),
    *,
    environment: ConfigSource | None = None,
    working_dir: Path | None = None,
    legacy_prefix: str | None = None,
) -> TracingResolution:
    """Apply runtime file precedence, including a planned child working directory."""
    resolution = TracingResolution(
        resolve_langfuse_settings(
            dotenv_path,
            environment=environment,
            working_dir=working_dir,
            legacy_prefix=legacy_prefix,
        ),
        resolve_phoenix_settings(dotenv_path, environment=environment, working_dir=working_dir),
    )
    resolution.validate()
    return resolution

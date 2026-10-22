"""Opt-in Phoenix tracing settings, separate from service and operational telemetry."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ai_agent_lib_core.config._tracing_support import (
    IDENTIFIER,
    resolve_variables,
    tracing_layers,
    validate_origin,
)
from ai_agent_lib_core.contracts import ConfigSource, ConfigurationError

PHOENIX_VARIABLES = (
    "EAP_PHOENIX_ENABLED",
    "EAP_PHOENIX_CAPTURE_CONTENT",
    "EAP_PHOENIX_ENV_FILE",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_PROJECT_NAME",
    "PHOENIX_API_KEY",
)

_DEFAULTS = {
    "EAP_PHOENIX_ENABLED": "false",
    "EAP_PHOENIX_CAPTURE_CONTENT": "false",
    "PHOENIX_COLLECTOR_ENDPOINT": "http://127.0.0.1:6006",
    "PHOENIX_PROJECT_NAME": "agent-lab",
}


@dataclass(frozen=True)
class PhoenixSettings:
    """An OTLP/HTTP origin and project; content capture requires an explicit opt-in."""

    base_url: str = "http://127.0.0.1:6006"
    project_name: str = "agent-lab"
    capture_content: bool = False
    api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        validate_origin(self.base_url, variable="PHOENIX_COLLECTOR_ENDPOINT")
        if not IDENTIFIER.fullmatch(self.project_name):
            raise ConfigurationError(
                "PHOENIX_PROJECT_NAME must contain 1-128 letters, digits, underscores or hyphens"
            )

    def trace_url(self, trace_id: str) -> str:
        """Link to a trace without including API credentials."""
        return f"{self.base_url.rstrip('/')}/redirects/traces/{trace_id}"


def load_phoenix_settings(
    dotenv_path: Path | None = Path(".env"),
    *,
    source: ConfigSource | None = None,
    environment: ConfigSource | None = None,
) -> PhoenixSettings | None:
    """Resolve environment > separate file > service settings without changing the process.

    A launcher can supply its planned child ``environment`` instead of changing
    ``os.environ``. ``source`` supplies a complete configuration and bypasses
    file layering; supply only one of these overrides.
    """
    return resolve_phoenix_settings(dotenv_path, source=source, environment=environment).settings


@dataclass(frozen=True)
class PhoenixResolution:
    """Validated optional settings and a credential-masked explanation of origins."""

    settings: PhoenixSettings | None
    shown: Mapping[str, str]
    origins: Mapping[str, str]


def resolve_phoenix_settings(
    dotenv_path: Path | None = Path(".env"),
    *,
    source: ConfigSource | None = None,
    environment: ConfigSource | None = None,
    working_dir: Path | None = None,
) -> PhoenixResolution:
    """Resolve the runtime settings for diagnostics or a planned child process."""
    layers = tracing_layers(
        dotenv_path,
        source=source,
        environment=environment,
        working_dir=working_dir,
        enabled="EAP_PHOENIX_ENABLED",
        env_file="EAP_PHOENIX_ENV_FILE",
        caller="load_phoenix_settings",
    )
    resolved = resolve_variables(layers, PHOENIX_VARIABLES, defaults=_DEFAULTS)
    values = resolved.values
    settings = (
        PhoenixSettings(
            base_url=values["PHOENIX_COLLECTOR_ENDPOINT"]
            or _DEFAULTS["PHOENIX_COLLECTOR_ENDPOINT"],
            project_name=values["PHOENIX_PROJECT_NAME"] or _DEFAULTS["PHOENIX_PROJECT_NAME"],
            capture_content=resolved.flag("EAP_PHOENIX_CAPTURE_CONTENT"),
            api_key=values["PHOENIX_API_KEY"],
        )
        if resolved.flag("EAP_PHOENIX_ENABLED")
        else None
    )
    return PhoenixResolution(settings, resolved.shown(("PHOENIX_API_KEY",)), resolved.origins)

"""Opt-in Phoenix tracing settings, separate from service and operational telemetry."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ai_agent_lib_core.config.sources import (
    DotenvConfigSource,
    EnvironConfigSource,
    LayeredConfigSource,
    MappingConfigSource,
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


@dataclass(frozen=True)
class PhoenixSettings:
    """An OTLP/HTTP origin and project; content capture requires an explicit opt-in."""

    base_url: str = "http://127.0.0.1:6006"
    project_name: str = "agent-lab"
    capture_content: bool = False
    api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.base_url)
            _ = parts.port
        except ValueError as error:
            raise ConfigurationError("PHOENIX_COLLECTOR_ENDPOINT must be a valid origin") from error
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
            or re.search(r"[\s\x00-\x1f\x7f<>\\()]", self.base_url)
            or not (
                parts.scheme == "https"
                or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise ConfigurationError(
                "PHOENIX_COLLECTOR_ENDPOINT must be an HTTPS origin (HTTP for loopback)"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.project_name):
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
    if source is not None and environment is not None:
        raise ValueError("Supply either source or environment to load_phoenix_settings")
    if source is None:
        environment = environment if environment is not None else EnvironConfigSource()
        service = DotenvConfigSource(dotenv_path) if dotenv_path else MappingConfigSource({})
        layers: list[ConfigSource] = [environment]
        if (environment.get("EAP_PHOENIX_ENABLED") or "").lower() != "false":
            extra = environment.get("EAP_PHOENIX_ENV_FILE")
            if not extra and (extra := service.get("EAP_PHOENIX_ENV_FILE")):
                extra = str((dotenv_path.parent if dotenv_path else Path.cwd()) / extra)
            if extra:
                layers.append(DotenvConfigSource(Path(extra), required=True))
        layers.append(service)
        source = LayeredConfigSource(*layers)

    def flag(name: str) -> bool:
        value = source.get(name) or "false"
        if value.lower() not in {"true", "false"}:
            raise ConfigurationError(f"{name} must be true or false")
        return value.lower() == "true"

    if not flag("EAP_PHOENIX_ENABLED"):
        return None
    return PhoenixSettings(
        base_url=source.get("PHOENIX_COLLECTOR_ENDPOINT") or "http://127.0.0.1:6006",
        project_name=source.get("PHOENIX_PROJECT_NAME") or "agent-lab",
        capture_content=flag("EAP_PHOENIX_CAPTURE_CONTENT"),
        api_key=source.get("PHOENIX_API_KEY") or "",
    )

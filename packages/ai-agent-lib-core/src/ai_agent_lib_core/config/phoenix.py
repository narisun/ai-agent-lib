"""Opt-in Phoenix tracing settings, separate from service and operational telemetry."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from ai_agent_lib_core.config.sources import (
    DotenvConfigSource,
    EnvironConfigSource,
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
    if source is not None and environment is not None:
        raise ValueError("Supply either source or environment to load_phoenix_settings")
    if source is None:
        environment = environment if environment is not None else EnvironConfigSource()
        service = DotenvConfigSource(dotenv_path) if dotenv_path else MappingConfigSource({})
        layers: list[tuple[str, ConfigSource]] = [("process environment", environment)]
        if (environment.get("EAP_PHOENIX_ENABLED") or "").lower() != "false":
            extra = environment.get("EAP_PHOENIX_ENV_FILE")
            if extra and working_dir is not None:
                extra = str(working_dir / extra)
            if not extra and (extra := service.get("EAP_PHOENIX_ENV_FILE")):
                extra = str((dotenv_path.parent if dotenv_path else Path.cwd()) / extra)
            if extra:
                layers.append(
                    (str(Path(extra).resolve()), DotenvConfigSource(Path(extra), required=True))
                )
        layers.append((str(dotenv_path.resolve()) if dotenv_path else "service settings", service))
    else:
        layers = [("injected source", source)]
    defaults = {
        "EAP_PHOENIX_ENABLED": "false",
        "EAP_PHOENIX_CAPTURE_CONTENT": "false",
        "PHOENIX_COLLECTOR_ENDPOINT": "http://127.0.0.1:6006",
        "PHOENIX_PROJECT_NAME": "agent-lab",
    }
    values, origins = {}, {}
    for name in PHOENIX_VARIABLES:
        found = next(
            (
                (value, f"{label}: {name}")
                for label, layer in layers
                if (value := layer.get(name)) is not None
            ),
            None,
        )
        values[name], origins[name] = found or (defaults.get(name, ""), "default")

    def flag(name: str) -> bool:
        value = values[name] or "false"
        if value.lower() not in {"true", "false"}:
            raise ConfigurationError(f"{name} must be true or false")
        return value.lower() == "true"

    settings = (
        PhoenixSettings(
            base_url=values["PHOENIX_COLLECTOR_ENDPOINT"] or defaults["PHOENIX_COLLECTOR_ENDPOINT"],
            project_name=values["PHOENIX_PROJECT_NAME"] or defaults["PHOENIX_PROJECT_NAME"],
            capture_content=flag("EAP_PHOENIX_CAPTURE_CONTENT"),
            api_key=values["PHOENIX_API_KEY"],
        )
        if flag("EAP_PHOENIX_ENABLED")
        else None
    )
    shown = {**values, "PHOENIX_API_KEY": "********" if values["PHOENIX_API_KEY"] else ""}
    return PhoenixResolution(settings, MappingProxyType(shown), MappingProxyType(origins))

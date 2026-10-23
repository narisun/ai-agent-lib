"""Opt-in Phoenix tracing settings, separate from service and operational telemetry."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from ai_agent_lib_core.config.bindings import PREFIX, legacy_spelling
from ai_agent_lib_core.config.deprecations import log_deprecation
from ai_agent_lib_core.config.sources import (
    DotenvConfigSource,
    EnvironConfigSource,
    MappingConfigSource,
)
from ai_agent_lib_core.contracts import ConfigSource, ConfigurationError

PHOENIX_LABELS = MappingProxyType(
    {
        "enabled": "AC2_PHOENIX_ENABLED",
        "capture_content": "AC2_PHOENIX_CAPTURE_CONTENT",
        "env_file": "AC2_PHOENIX_ENV_FILE",
        "endpoint": "PHOENIX_COLLECTOR_ENDPOINT",
        "project": "PHOENIX_PROJECT_NAME",
        "api_key": "PHOENIX_API_KEY",
    }
)
"""Variable names by the short label generated documents refer to them by."""

PHOENIX_VARIABLES = tuple(PHOENIX_LABELS.values())

_Layers = Sequence[tuple[str, ConfigSource]]


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


def _lookup(
    layers: _Layers, name: str, deprecations: list[str] | None = None
) -> tuple[str, str] | None:
    """Find a variable's value and origin in the first layer that sets it.

    An ``AC2_`` name is also read in its ``EAP_`` spelling, which keeps working
    for one minor release. Equal values in both spellings coexist silently. A
    different value is a conflict, wherever each spelling is set, because
    guessing which one was meant is the hazard. A legacy spelling that supplies
    the value is logged as deprecated, and its message is added to
    ``deprecations`` when the caller passes a list to collect them in.

    Raises:
        ConfigurationError: If the two spellings carry different values.
    """

    def first(variable: str) -> tuple[str, str] | None:
        return next(
            (
                (value, f"{label}: {variable}")
                for label, layer in layers
                if (value := layer.get(variable)) is not None
            ),
            None,
        )

    current = first(name)
    if not name.startswith(PREFIX):
        return current
    old_name = legacy_spelling(name)
    old = first(old_name)
    if old is None:
        return current
    if current is not None:
        if current[0] != old[0]:
            raise ConfigurationError(
                f"{old_name} is deprecated and conflicts with {name}; set only {name}"
            )
        return current
    message = f"{old_name} is deprecated; use {name} instead"
    log_deprecation(message)
    if deprecations is not None:
        deprecations.append(message)
    return old


def _value(layers: _Layers, name: str) -> str | None:
    found = _lookup(layers, name)
    return found[0] if found else None


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
    """Validated optional settings and a credential-masked explanation of origins.

    Attributes:
        settings: The validated settings, or ``None`` when tracing is off.
        shown: Each variable's value as it may be shown.
        origins: Where each variable's value came from.
        warnings: One message for each legacy spelling that supplied a value, such
            as ``"EAP_PHOENIX_ENABLED is deprecated; use AC2_PHOENIX_ENABLED instead"``.
            They are also logged, once per process, through the library logger.
    """

    settings: PhoenixSettings | None
    shown: Mapping[str, str]
    origins: Mapping[str, str]
    warnings: tuple[str, ...] = ()


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
        from_process = layers[:1]
        enabled, env_file = PHOENIX_LABELS["enabled"], PHOENIX_LABELS["env_file"]
        if (_value(from_process, enabled) or "").lower() != "false":
            extra = _value(from_process, env_file)
            if extra and working_dir is not None:
                extra = str(working_dir / extra)
            if not extra and (extra := _value([("service settings", service)], env_file)):
                extra = str((dotenv_path.parent if dotenv_path else Path.cwd()) / extra)
            if extra:
                layers.append(
                    (str(Path(extra).resolve()), DotenvConfigSource(Path(extra), required=True))
                )
        layers.append((str(dotenv_path.resolve()) if dotenv_path else "service settings", service))
    else:
        layers = [("injected source", source)]
    defaults = {
        "AC2_PHOENIX_ENABLED": "false",
        "AC2_PHOENIX_CAPTURE_CONTENT": "false",
        "PHOENIX_COLLECTOR_ENDPOINT": "http://127.0.0.1:6006",
        "PHOENIX_PROJECT_NAME": "agent-lab",
    }
    values, origins = {}, {}
    # Collected here only: the earlier reads of the enable and file settings merely decide
    # which layers exist, and read the same names again below, so a name is told once.
    deprecations: list[str] = []
    for name in PHOENIX_VARIABLES:
        found = _lookup(layers, name, deprecations)
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
            capture_content=flag("AC2_PHOENIX_CAPTURE_CONTENT"),
            api_key=values["PHOENIX_API_KEY"],
        )
        if flag("AC2_PHOENIX_ENABLED")
        else None
    )
    shown = {**values, "PHOENIX_API_KEY": "********" if values["PHOENIX_API_KEY"] else ""}
    return PhoenixResolution(
        settings, MappingProxyType(shown), MappingProxyType(origins), tuple(deprecations)
    )


def phoenix_env_example(guide: str = "docs/phoenix-trial.md") -> str:
    """Return commented defaults for opt-in tracing, for generated and example ``.env`` files.

    Args:
        guide: The path, from the file that carries the block, of the tracing guide.
    """
    defaults = PhoenixSettings()
    examples = {
        "enabled": "false",
        "capture_content": "false",
        "env_file": "path/to/phoenix.env",
        "endpoint": defaults.base_url,
        "project": defaults.project_name,
        "api_key": "your-api-key",
    }
    return (
        f"\n# Optional Phoenix tracing: see {guide}. Keep real keys in private files.\n"
        + "".join(f"# {PHOENIX_LABELS[label]}={value}\n" for label, value in examples.items())
    )

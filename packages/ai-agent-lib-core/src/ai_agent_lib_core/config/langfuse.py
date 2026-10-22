"""Optional Langfuse settings, kept separate from operational telemetry configuration."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ai_agent_lib_core.config._tracing_support import (
    IDENTIFIER,
    Lookup,
    read_variable,
    resolve_variables,
    tracing_layers,
    validate_origin,
)
from ai_agent_lib_core.contracts import ConfigSource, ConfigurationError

__all__ = [
    "LANGFUSE_VARIABLES",
    "LangfuseResolution",
    "LangfuseSettings",
    "langfuse_env_example",
    "load_langfuse_settings",
    "resolve_langfuse_settings",
]

LANGFUSE_VARIABLES = {
    "enabled": "EAP_LANGFUSE_ENABLED",
    "capture_content": "EAP_LANGFUSE_CAPTURE_CONTENT",
    "env_file": "EAP_LANGFUSE_ENV_FILE",
    "base_url": "LANGFUSE_BASE_URL",
    "project_id": "LANGFUSE_PROJECT_ID",
    "public_key": "LANGFUSE_PUBLIC_KEY",
    "secret_key": "LANGFUSE_SECRET_KEY",
}
"""Names used by the loader, generated projects and configuration reference."""

_ALIASED = ("enabled", "capture_content", "env_file")
"""The labels a migrating application may still set under its own prefix."""

_SECRETS = (LANGFUSE_VARIABLES["public_key"], LANGFUSE_VARIABLES["secret_key"])


@dataclass(frozen=True)
class LangfuseSettings:
    """Separate trace export from the service's operational metrics exporter."""

    base_url: str
    project_id: str
    public_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    capture_content: bool = False

    def __post_init__(self) -> None:
        validate_origin(self.base_url, variable=LANGFUSE_VARIABLES["base_url"])
        if not IDENTIFIER.fullmatch(self.project_id):
            raise ConfigurationError("LANGFUSE_PROJECT_ID must be the project ID from its URL")
        if not self.public_key or not self.secret_key:
            raise ConfigurationError("Langfuse requires public and secret project keys")

    def trace_url(self, trace_id: str) -> str:
        """Build a private project link; keys are never included in URLs."""
        return f"{self.base_url.rstrip('/')}/project/{self.project_id}/traces/{trace_id}"


@dataclass(frozen=True)
class LangfuseResolution:
    """Validated optional settings and a key-masked explanation of their origins."""

    settings: LangfuseSettings | None
    shown: Mapping[str, str]
    origins: Mapping[str, str]


def _project_prefix(dotenv_path: Path | None) -> str | None:
    """Read an explicit migration alias without importing or executing service code."""
    if dotenv_path is None or not (path := dotenv_path.parent / "pyproject.toml").is_file():
        return None
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8"))
        prefix = (
            project.get("tool", {}).get("ai-agent-lib", {}).get("langfuse", {}).get("legacy-prefix")
        )
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as error:
        raise ConfigurationError(
            "Cannot read Langfuse migration settings in pyproject.toml"
        ) from error
    if prefix is not None and (
        not isinstance(prefix, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", prefix)
    ):
        raise ConfigurationError("Langfuse legacy-prefix must be an uppercase variable prefix")
    return prefix


def _lookup(prefix: str | None) -> Lookup:
    """Read the canonical name first, then a prefixed alias for the switches only."""
    if prefix is None:
        return read_variable
    aliases = {
        LANGFUSE_VARIABLES[label]: f"{prefix}_LANGFUSE_{label.upper()}" for label in _ALIASED
    }

    def find(source: ConfigSource, name: str) -> tuple[str, str] | None:
        if (value := source.get(name)) is not None:
            return value, name
        if (alias := aliases.get(name)) and (value := source.get(alias)):
            return value, alias
        return None

    return find


def load_langfuse_settings(
    dotenv_path: Path | None = Path(".env"),
    *,
    source: ConfigSource | None = None,
    legacy_prefix: str | None = None,
) -> LangfuseSettings | None:
    """Load opt-in tracing without changing the service configuration or process environment.

    Process variables override an optional separate settings file, which overrides
    the agent's dotenv file. A file path set in dotenv is relative to that file;
    a process-supplied path is relative to the working directory. ``source`` makes
    resolution testable without environment access. ``legacy_prefix`` supports
    applications migrating from prefixed trial settings; canonical names win
    within each layer. Explicitly disabling in the process skips the extra file.
    """
    return resolve_langfuse_settings(
        dotenv_path, source=source, legacy_prefix=legacy_prefix
    ).settings


def resolve_langfuse_settings(
    dotenv_path: Path | None = Path(".env"),
    *,
    source: ConfigSource | None = None,
    legacy_prefix: str | None = None,
    environment: ConfigSource | None = None,
    working_dir: Path | None = None,
) -> LangfuseResolution:
    """Resolve exactly as startup does, exposing file/process origins and masked keys.

    Services migrating old names may declare ``legacy-prefix`` in the pyproject
    table ``[tool.ai-agent-lib.langfuse]``. An explicit argument takes precedence.
    Disabled tracing does not validate inactive credentials or contact a server.
    """
    prefix = legacy_prefix
    if source is None and prefix is None:
        prefix = _project_prefix(dotenv_path)
    lookup = _lookup(prefix)
    layers = tracing_layers(
        dotenv_path,
        source=source,
        environment=environment,
        working_dir=working_dir,
        enabled=LANGFUSE_VARIABLES["enabled"],
        env_file=LANGFUSE_VARIABLES["env_file"],
        lookup=lookup,
        caller="resolve_langfuse_settings",
    )
    resolved = resolve_variables(
        layers,
        LANGFUSE_VARIABLES.values(),
        defaults={LANGFUSE_VARIABLES[label]: "false" for label in ("enabled", "capture_content")},
        lookup=lookup,
    )

    def value(label: str) -> str:
        return resolved.values[LANGFUSE_VARIABLES[label]]

    settings = (
        LangfuseSettings(
            base_url=value("base_url"),
            project_id=value("project_id"),
            public_key=value("public_key"),
            secret_key=value("secret_key"),
            capture_content=resolved.flag(LANGFUSE_VARIABLES["capture_content"]),
        )
        if resolved.flag(LANGFUSE_VARIABLES["enabled"])
        else None
    )
    return LangfuseResolution(settings, resolved.shown(_SECRETS), resolved.origins)


def langfuse_env_example() -> str:
    """Return commented defaults for opt-in tracing in generated agents."""
    examples = {
        "enabled": "false",
        "capture_content": "false",
        "env_file": "path/to/langfuse.env",
        "base_url": "http://localhost:3000",
        "project_id": "your-project-id",
        "public_key": "your-project-public-key",
        "secret_key": "your-project-secret-key",
    }
    return (
        "\n# Optional Langfuse tracing: see docs/langfuse.md. Keep real keys in private files.\n"
        + "".join(f"# {LANGFUSE_VARIABLES[label]}={value}\n" for label, value in examples.items())
    )

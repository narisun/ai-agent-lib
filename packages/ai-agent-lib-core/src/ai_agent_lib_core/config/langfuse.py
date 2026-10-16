"""Optional Langfuse settings, kept separate from operational telemetry configuration."""

from __future__ import annotations

import re
import tomllib
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


@dataclass(frozen=True)
class LangfuseSettings:
    """Separate trace export from the service's operational metrics exporter."""

    base_url: str
    project_id: str
    public_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    capture_content: bool = False

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.base_url)
            # Accessing port also validates malformed or out-of-range port numbers.
            _ = parts.port
        except ValueError as error:
            raise ConfigurationError("LANGFUSE_BASE_URL is not a valid origin") from error
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
                "LANGFUSE_BASE_URL must be an HTTPS origin (HTTP for loopback)"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.project_id):
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


@dataclass(frozen=True)
class _Layer:
    source: ConfigSource
    origin: str
    prefix: str | None

    def find(self, label: str) -> tuple[str, str] | None:
        name = LANGFUSE_VARIABLES[label]
        if (value := self.source.get(name)) is not None:
            return value, f"{self.origin}: {name}"
        if self.prefix and label in {"enabled", "capture_content", "env_file"}:
            alias = f"{self.prefix}_LANGFUSE_{label.upper()}"
            if value := self.source.get(alias):
                return value, f"{self.origin}: {alias}"
        return None

    def value(self, label: str) -> str:
        found = self.find(label)
        return found[0] if found else ""


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
) -> LangfuseResolution:
    """Resolve exactly as startup does, exposing file/process origins and masked keys.

    Services migrating old names may declare ``legacy-prefix`` in the pyproject
    table ``[tool.ai-agent-lib.langfuse]``. An explicit argument takes precedence.
    Disabled tracing does not validate inactive credentials or contact a server.
    """
    prefix = legacy_prefix
    if source is None:
        if prefix is None:
            prefix = _project_prefix(dotenv_path)
        environment = _Layer(EnvironConfigSource(), "process environment", prefix)
        service = _Layer(
            DotenvConfigSource(dotenv_path) if dotenv_path else MappingConfigSource({}),
            str(dotenv_path.resolve()) if dotenv_path else "service settings",
            prefix,
        )
        layers = [environment]
        # An explicit process disable must also bypass a missing separate file.
        if environment.value("enabled").lower() != "false":
            extra = environment.value("env_file")
            if not extra and (extra := service.value("env_file")):
                extra = str((dotenv_path.parent if dotenv_path else Path.cwd()) / extra)
            if extra:
                path = Path(extra)
                layers.append(
                    _Layer(DotenvConfigSource(path, required=True), str(path.resolve()), prefix)
                )
        layers.append(service)
    else:
        layers = [_Layer(source, "injected source", prefix)]

    values: dict[str, str] = {}
    origins: dict[str, str] = {}
    for label, name in LANGFUSE_VARIABLES.items():
        found = next((found for layer in layers if (found := layer.find(label)) is not None), None)
        values[label], origins[name] = found or (
            "false" if label in {"enabled", "capture_content"} else "",
            "default",
        )

    def value(label: str) -> str:
        return values[label]

    def flag(label: str) -> bool:
        raw = value(label) or "false"
        if raw.lower() not in {"true", "false"}:
            raise ConfigurationError(f"{LANGFUSE_VARIABLES[label]} must be true or false")
        return raw.lower() == "true"

    settings = (
        LangfuseSettings(
            base_url=value("base_url"),
            project_id=value("project_id"),
            public_key=value("public_key"),
            secret_key=value("secret_key"),
            capture_content=flag("capture_content"),
        )
        if flag("enabled")
        else None
    )
    shown = {
        name: "********"
        if label in {"public_key", "secret_key"} and values[label]
        else values[label]
        for label, name in LANGFUSE_VARIABLES.items()
    }
    return LangfuseResolution(settings, MappingProxyType(shown), MappingProxyType(origins))


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

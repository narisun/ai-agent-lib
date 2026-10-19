"""Validated local settings and explicit translation of an agent's environment."""

import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_ui.application.catalog import validate_destination
from ai_agent_lib_ui.contracts.catalog import AgentBinding


class LocalIdentity(BaseModel):
    """The static identity shared by this local UI and its local agents."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: str = "dev-user"
    tenant: str = "dev-tenant"
    roles: tuple[str, ...] = ()

    def principal(self) -> Principal:
        """Build a principal after the local access-code session is verified."""
        return Principal(
            subject=self.subject,
            tenant=self.tenant,
            roles=frozenset(self.roles),
            authenticated_by="ui-local-session",
        )


class Settings(BaseModel):
    """A local-only server configuration stored beside its private SQLite state."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["agentlib.ui-local/v1"] = "agentlib.ui-local/v1"
    agents_path: Path
    tools_path: Path
    identity: LocalIdentity
    bindings: Annotated[tuple[AgentBinding, ...], Field(min_length=1, max_length=200)]
    allowed_urls: frozenset[str]
    port: Annotated[int, Field(ge=1024, le=65535)] = 8765
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 180
    inspector_enabled: bool = True

    @property
    def origin(self) -> str:
        """The single accepted browser origin; serving on other hosts is unsupported."""
        return f"http://127.0.0.1:{self.port}"

    @model_validator(mode="after")
    def validate_local(self) -> "Settings":
        """Keep static identity on loopback and reject ambiguous agent settings."""
        self.identity.principal()
        ids = [binding.manifest.agent_id for binding in self.bindings]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("configure at least one agent, with unique IDs")
        for binding in self.bindings:
            validate_destination(binding.base_url, allowed_urls=self.allowed_urls, local=True)
            if urlsplit(binding.base_url).hostname not in {"127.0.0.1", "::1"}:
                raise ValueError("static local identity requires loopback agent URLs")
        return self

    @classmethod
    def read(cls, path: Path) -> "Settings":
        """Resolve registry paths relative to this configuration, never browser input."""
        values = json.loads(path.read_text(encoding="utf-8"))
        settings = cls.model_validate(values)
        return settings.model_copy(
            update={
                "agents_path": (path.parent / settings.agents_path).resolve(),
                "tools_path": (path.parent / settings.tools_path).resolve(),
            }
        )


# The UI may import only core's contracts, so it reads the agent's settings itself. A deprecated
# spelling is reported on the library's own configuration logger, where an operator already
# looks for the same notice from the agent, and once per process however often it is read.
_LOG = logging.getLogger("ai_agent_lib_core.config")
_REPORTED: set[str] = set()
_LOCK = threading.Lock()
_PREFIX = "AC2_"
_LEGACY_PREFIX = "EAP_"  # Deprecation window: accepted for one minor release, with a warning.


def _setting(values: Mapping[str, str | None], stem: str, default: str | None = None) -> str | None:
    """Read an agent setting by its ``AC2_`` name, or by the deprecated ``EAP_`` one.

    Equal values in both spellings are fine. Different values are an error naming
    both, because guessing which spelling was meant is the hazard.
    """
    name, old_name = _PREFIX + stem, _LEGACY_PREFIX + stem
    value, old_value = values.get(name), values.get(old_name)
    if old_value is None:
        return default if value is None else value
    if value is None:
        message = f"{old_name} is deprecated; use {name} instead"
        with _LOCK:
            first = message not in _REPORTED
            _REPORTED.add(message)
        if first:
            _LOG.warning("%s", message)
        return old_value
    if value != old_value:
        raise ValueError(f"{old_name} is deprecated and conflicts with {name}; set only {name}")
    return value


class StaticEnvironmentOptions(LocalIdentity):
    """Accept the agent's static token options without copying them into UI authority."""

    audience: str | None = None
    token_ttl_seconds: Annotated[int, Field(gt=0, le=3600)] = 300


def agent_environment(path: Path) -> tuple[Path, Path, LocalIdentity]:
    """Read only file registry and static identity settings, without exporting secrets.

    Relative registry paths use the .env directory, matching a service started
    there. Shell overrides and model credentials are deliberately not imported.
    """
    from dotenv import dotenv_values

    if not path.is_file():
        raise ValueError("the --env-file must name an existing agent .env file")
    values = dotenv_values(path, interpolate=False)
    if _setting(values, "PROFILE", "local") != "local":
        raise ValueError("Phase 2 requires AC2_PROFILE=local")
    if _setting(values, "REGISTRY_PROVIDER", "file") != "file":
        raise ValueError("Phase 2 supports AC2_REGISTRY_PROVIDER=file")
    if _setting(values, "IDENTITY_PROVIDER", "static") != "static":
        raise ValueError("Phase 2 supports AC2_IDENTITY_PROVIDER=static on loopback")
    registry = json.loads(_setting(values, "REGISTRY_OPTIONS") or "{}")
    if not isinstance(registry, dict) or set(registry) - {"agents_path", "tools_path"}:
        raise ValueError("use file registry options: agents_path and tools_path")
    options = StaticEnvironmentOptions.model_validate(
        json.loads(_setting(values, "IDENTITY_OPTIONS") or "{}")
    )
    identity = LocalIdentity(subject=options.subject, tenant=options.tenant, roles=options.roles)
    return (
        (path.parent / registry.get("agents_path", "registry/agents.yaml")).resolve(),
        (path.parent / registry.get("tools_path", "registry/mcp-tools.yaml")).resolve(),
        identity,
    )

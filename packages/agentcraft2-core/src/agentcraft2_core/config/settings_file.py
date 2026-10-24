"""The developer's settings file: what a project says about itself, in TOML.

``agentcraft2.settings.toml`` holds the choices a developer makes about their own agents
and servers: the model, the limits, where the registries, the rules and the data are,
and who the local caller is. It is the same file wherever the project runs, it holds no
secret, and it names nothing about the infrastructure: the identity provider, the
secrets store and the rest of what a deployment decides are not keys of it.

It is a layer under the ``.env`` file and the process environment, so a variable still
wins. Each table is another way to write a variable that exists already, which is what
makes the file a convenience and not a second configuration to keep in step:

====================  =========================================================
Table                 The variable it writes
====================  =========================================================
``[model]``           the model provider, its ID, its aliases and options
``[limits]``          the timeouts, retries and budgets
``[registry]``        the options of the file registry
``[policy]``          the options of the rules engine
``[persona]``         the options of the development identity
``[data.<name>]``     one named data source
====================  =========================================================

``agentcraft2 config schema`` prints a JSON Schema of the file, for an editor and for a
coding assistant. A relative path in it is relative to the file.
"""

from __future__ import annotations

import difflib
import json
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from agentcraft2_core.config.bindings import Key, options_key
from agentcraft2_core.config.reference import variable_for
from agentcraft2_core.config.sources import MappingConfigSource
from agentcraft2_core.contracts import ConfigurationError, Section, describe

__all__ = [
    "SETTINGS_FILE",
    "SettingsFile",
    "SettingsFileSource",
    "settings_files",
    "settings_schema",
]

SETTINGS_FILE = "agentcraft2.settings.toml"
"""The name of the file, in the project home or beside a service's ``.env``."""


class _Table(BaseModel):
    """A table of the file. A key it does not know is an error, never ignored."""

    model_config = ConfigDict(extra="forbid")


class AliasTable(_Table):
    """A further model alias: a role such as ``fast``, so code asks for the role."""

    id: str
    provider: str | None = None


class ModelTable(_Table):
    """The model: provider, ID, further aliases, and options by provider."""

    provider: str | None = None
    id: str | None = None
    aliases: dict[str, str | AliasTable] = {}
    options: dict[str, dict[str, Any]] = {}


class RegistryTable(_Table):
    """Where the agent and tool registries are, for the file registry."""

    agents: str | None = None
    tools: str | None = None


class PolicyTable(_Table):
    """Where the rules are, for the in-process rules engine."""

    rules: str | None = None


class PersonaTable(_Table):
    """Who the caller is on a developer's machine, for the development identity."""

    subject: str | None = None
    tenant: str | None = None
    roles: list[str] | None = None
    audience: str | None = None
    token_ttl_seconds: int | None = None


class SettingsFile(_Table):
    """Everything the file can say. The infrastructure is not here."""

    model: ModelTable = ModelTable()
    limits: dict[str, Any] = {}
    registry: RegistryTable = RegistryTable()
    policy: PolicyTable = PolicyTable()
    persona: PersonaTable = PersonaTable()
    data: dict[str, dict[str, Any]] = {}


def settings_schema() -> str:
    """Return a JSON Schema of the settings file, for an editor or a coding assistant."""
    schema = SettingsFile.model_json_schema()
    schema["title"] = SETTINGS_FILE
    return json.dumps(schema, indent=2)


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _variables(settings: SettingsFile) -> dict[str, tuple[str, str]]:
    """Return the variables the file writes, each with the table it came from."""
    written: dict[str, tuple[str, str]] = {}

    def put(key: str, value: object, table: str) -> None:
        text = value if isinstance(value, str) else _json(value)
        written[variable_for(key)] = (text, table)

    model = settings.model
    if model.provider is not None:
        put(Key.MODEL_PROVIDER, model.provider, "[model]")
    if model.id is not None:
        put(Key.MODEL_ID, model.id, "[model]")
    if model.aliases:
        put(
            Key.MODEL_ALIASES,
            {
                name: (
                    alias
                    if isinstance(alias, str)
                    else {
                        "model_id": alias.id,
                        **({"provider": alias.provider} if alias.provider else {}),
                    }
                )
                for name, alias in model.aliases.items()
            },
            "[model.aliases]",
        )
    if model.options:
        put(Key.MODEL_OPTIONS, model.options, "[model.options]")
    if settings.limits:
        put(Key.LIMITS, settings.limits, "[limits]")
    registry = {
        name: value
        for name, value in (
            ("agents_path", settings.registry.agents),
            ("tools_path", settings.registry.tools),
        )
        if value is not None
    }
    if registry:
        put(options_key(Section.REGISTRY), registry, "[registry]")
    if settings.policy.rules is not None:
        put(options_key(Section.POLICY), {"path": settings.policy.rules}, "[policy]")
    persona = settings.persona.model_dump(exclude_none=True)
    if persona:
        put(options_key(Section.IDENTITY), persona, "[persona]")
    if settings.data:
        put(Key.DATA_SOURCES, settings.data, "[data]")
    return written


def _fields_at(path: tuple[str | int, ...]) -> list[str]:
    """Return the keys the table at ``path`` takes, to suggest one for a misspelt key."""
    model: type[BaseModel] = SettingsFile
    for part in path:
        field = model.model_fields.get(str(part))
        annotation = field.annotation if field is not None else None
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            return []
        model = annotation
    return sorted(model.model_fields)


def _problems(error: ValidationError) -> list[str]:
    """Say what is wrong without repeating a value: a setting may be a secret by mistake."""
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in item["loc"]) or "the file"
        if item["type"] == "extra_forbidden":
            close = difflib.get_close_matches(str(item["loc"][-1]), _fields_at(item["loc"][:-1]), 1)
            hint = f"; did you mean {close[0]}?" if close else ""
            problems.append(f"{where}: not a key of the file{hint}")
        else:
            problems.append(f"{where}: {item['type']}")
    return problems


class SettingsFileSource(MappingConfigSource):
    """The variables a settings file writes, as a configuration source.

    Args:
        path: The file.

    Raises:
        ConfigurationError: If the file cannot be read, is not TOML, or has a key
            or a value the file does not take. A value is never repeated.
    """

    def __init__(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigurationError(
                f"the settings file {path} could not be read ({describe(exc)})",
                expected="a readable UTF-8 TOML file",
            ) from exc
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"the settings file {path} is not valid TOML ({exc})",
                expected="TOML: [tables] and key = value lines",
                fix="correct the file at the place named",
            ) from None
        try:
            settings = SettingsFile.model_validate(parsed)
        except ValidationError as exc:
            problems = _problems(exc)
            raise ConfigurationError(
                f"the settings file {path} has {len(problems)} problem"
                f"{'s' if len(problems) != 1 else ''}",
                expected="the keys of agentcraft2.settings.toml (agentcraft2 config schema)",
                actual="; ".join(problems),
                fix="correct each key named",
            ) from None
        written = _variables(settings)
        super().__init__({name: text for name, (text, _) in written.items()}, base_dir=path.parent)
        self._tables = {name: table for name, (_, table) in written.items()}
        self._path = path

    def origin_of(self, name: str) -> str | None:
        """Say where in which file the value of ``name`` was written, when this source holds it."""
        table = self._tables.get(name)
        return f"{self._path.name} {table}" if table is not None else None


def settings_files(*folders: Path) -> list[Path]:
    """Return the settings files that exist in ``folders``, the nearest first, without repeats."""
    found: list[Path] = []
    for folder in folders:
        candidate = folder / SETTINGS_FILE
        if candidate.is_file() and candidate.resolve() not in {path.resolve() for path in found}:
            found.append(candidate)
    return found

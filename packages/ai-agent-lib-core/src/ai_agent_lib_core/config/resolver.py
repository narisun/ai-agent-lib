"""The resolver: raw configuration in, an immutable ``ServiceConfig`` out."""

from __future__ import annotations

import difflib
import json
import re
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import SecretStr, ValidationError

from ai_agent_lib_core.config.bindings import (
    DEFAULT_BINDINGS,
    PREFIX,
    SECRET_KEY_PREFIX,
    Binding,
    Key,
    ValueKind,
    options_key,
    provider_key,
    secret_key,
    secret_name,
    validate_bindings,
)
from ai_agent_lib_core.config.profiles import PROFILE_DEFAULTS, ProfileDefaults
from ai_agent_lib_core.contracts import (
    ConfigSource,
    ConfigurationError,
    DeploymentEnv,
    ExternalSettings,
    Limits,
    ModelRef,
    ModelSection,
    Profile,
    ProviderSelection,
    Section,
    ServiceConfig,
    TelemetryMode,
    kind_of,
    options_error,
    shown_value,
)

__all__ = ["MASKED", "ConfigResolver", "Resolution"]

MASKED = "********"
"""What is shown in place of a sensitive value."""

EnumT = TypeVar("EnumT", Profile, DeploymentEnv, TelemetryMode)

_ALIAS_FIELDS = frozenset({"provider", "model_id"})
_DATA_SOURCE_KIND = "kind"
_DATA_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# What AWS sets its execution-environment variable to on compute it manages.
_MANAGED_RUNTIMES = ("AWS_ECS_", "AWS_Lambda_")


@dataclass(frozen=True, slots=True)
class Resolution:
    """A resolved configuration together with how it was reached.

    Attributes:
        config: The resolved configuration.
        origins: For each logical key, the variable or profile that supplied it.
        warnings: Messages about deprecated variable names that were used.
        shown: For each logical key, its value as it may be displayed. The
            value of a sensitive variable, and every secret, is masked.
    """

    config: ServiceConfig
    origins: Mapping[str, str]
    warnings: tuple[str, ...]
    shown: Mapping[str, str] = field(default_factory=dict)


class ConfigResolver:
    """Turns a :class:`ConfigSource` into a :class:`ServiceConfig`.

    Args:
        source: Where raw values come from.
        bindings: The variable table. Defaults to the library's own table.
        profile_defaults: The adapters each profile selects by default.
        base_dir: The folder a relative path in a value is relative to,
            normally the folder of the ``.env`` file. Without it a relative
            path is relative to the working directory.
    """

    def __init__(
        self,
        source: ConfigSource,
        *,
        bindings: Sequence[Binding] = DEFAULT_BINDINGS,
        profile_defaults: Mapping[Profile, ProfileDefaults] = PROFILE_DEFAULTS,
        base_dir: Path | None = None,
    ) -> None:
        validate_bindings(bindings)
        self._source = source
        self._base_dir = base_dir
        self._bindings = tuple(bindings)
        self._by_key = {binding.key: binding for binding in self._bindings}
        self._profile_defaults = profile_defaults

    def resolve(self) -> ServiceConfig:
        """Return the resolved configuration.

        Raises:
            ConfigurationError: If a value is invalid or an owned variable is unknown.
        """
        return self.explain().config

    def explain(self) -> Resolution:
        """Resolve and also report where each value came from."""
        self._reject_unknown_names()
        values, origins, notes = self._read()

        profile = self._enum(values, Key.PROFILE, Profile, Profile.LOCAL)
        deployment_env = self._enum(values, Key.DEPLOYMENT_ENV, DeploymentEnv, DeploymentEnv.LOCAL)
        telemetry = self._enum(values, Key.TELEMETRY, TelemetryMode, TelemetryMode.OFF)
        self._refuse_local_on_a_managed_runtime(values, deployment_env)
        defaults = self._profile_defaults[profile]
        profile_origin = f"profile {profile.value}"

        model_provider = values.get(Key.MODEL_PROVIDER, defaults.model_provider)
        origins.setdefault(Key.MODEL_PROVIDER, profile_origin)
        model = ModelSection(
            provider=model_provider,
            model_id=values.get(Key.MODEL_ID),
            aliases=self._aliases(values, model_provider),
        )

        sections: dict[Section, ProviderSelection] = {}
        for section in Section:
            selector = provider_key(section)
            origins.setdefault(selector, profile_origin)
            sections[section] = ProviderSelection(
                provider=values.get(selector, defaults.sections[section]),
                options=self._json_object(values, options_key(section)),
                base_dir=self._base_dir,
            )

        config = ServiceConfig(
            profile=profile,
            deployment_env=deployment_env,
            telemetry=telemetry,
            limits=self._limits(values),
            tls_ca_bundle=self._path(values, Key.TLS_CA_BUNDLE),
            model=model,
            sections=sections,
            secrets={
                key.removeprefix(SECRET_KEY_PREFIX): SecretStr(value)
                for key, value in values.items()
                if key.startswith(SECRET_KEY_PREFIX)
            },
            external=ExternalSettings(
                aws_profile=values.get(Key.AWS_PROFILE),
                aws_region=values.get(Key.AWS_REGION),
                aws_ca_bundle=self._path(values, Key.AWS_CA_BUNDLE),
                https_proxy=values.get(Key.HTTPS_PROXY),
                no_proxy=values.get(Key.NO_PROXY),
            ),
            data_sources=self._data_sources(values),
        )
        shown = {key: self._display(key, value) for key, value in values.items()}
        shown.setdefault(Key.PROFILE, profile.value)
        shown.setdefault(Key.DEPLOYMENT_ENV, deployment_env.value)
        shown.setdefault(Key.TELEMETRY, telemetry.value)
        shown.setdefault(Key.MODEL_PROVIDER, model_provider)
        for section, selection in sections.items():
            shown.setdefault(provider_key(section), selection.provider)
        for key in (Key.PROFILE, Key.DEPLOYMENT_ENV, Key.TELEMETRY):
            origins.setdefault(key, "default")
        return Resolution(
            config=config,
            origins=MappingProxyType(origins),
            warnings=tuple(notes),
            shown=MappingProxyType(shown),
        )

    def _display(self, key: str, value: str) -> str:
        """Return a value as it may be shown to a person."""
        binding = self._by_key.get(key)
        if key.startswith(SECRET_KEY_PREFIX) or (binding is not None and binding.sensitive):
            return MASKED
        return value

    # ------------------------------------------------------------------ reading

    def _reject_unknown_names(self) -> None:
        known = {name for binding in self._bindings for name in binding.all_names}
        unknown = sorted(
            name
            for name in self._source.names()
            if name.startswith(PREFIX) and name not in known and secret_name(name) is None
        )
        if not unknown:
            return
        owned = sorted(name for name in known if name.startswith(PREFIX))
        details = []
        for name in unknown:
            close = difflib.get_close_matches(name, owned, n=1)
            details.append(f"{name} (did you mean {close[0]}?)" if close else name)
        raise ConfigurationError(
            f"unknown configuration variable{'s' if len(details) != 1 else ''}: "
            f"{', '.join(details)}",
            expected=f"only the {PREFIX} variables the library defines",
            fix=(
                "rename or remove it; every variable is listed with its default in "
                "docs/variables.md and the developer guide"
            ),
        )

    def _read(self) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """Read every bound variable, applying alternate and deprecated names."""
        values: dict[str, str] = {}
        origins: dict[str, str] = {}
        notes: list[str] = []
        for binding in self._bindings:
            found: tuple[str, str] | None = None
            for name in (binding.name, *binding.alternate_names):
                value = self._clean(self._source.get(name))
                if value is not None:
                    found = (name, value)
                    break
            for old_name in binding.deprecated_names:
                old_value = self._clean(self._source.get(old_name))
                if old_value is None:
                    continue
                if found is not None and found[1] != old_value:
                    raise ConfigurationError(
                        f"{old_name} is deprecated and conflicts with {found[0]}; "
                        f"set only {binding.name}"
                    )
                note = f"{old_name} is deprecated; use {binding.name} instead"
                notes.append(note)
                warnings.warn(note, DeprecationWarning, stacklevel=4)
                if found is None:
                    found = (old_name, old_value)
            if found is not None:
                origins[binding.key] = f"variable {found[0]}"
                values[binding.key] = found[1]
        self._read_named_secrets(values, origins)
        return values, origins, notes

    def _read_named_secrets(self, values: dict[str, str], origins: dict[str, str]) -> None:
        """Read the variables that each supply one named secret."""
        for variable in sorted(self._source.names()):
            name = secret_name(variable)
            value = self._clean(self._source.get(variable)) if name is not None else None
            if name is None or value is None:
                continue
            key = secret_key(name)
            if values.get(key, value) != value:
                other = origins[key].removeprefix("variable ")
                raise ConfigurationError(
                    f"{variable} and {other} both set the secret {name!r}; set only one"
                )
            values[key] = value
            origins.setdefault(key, f"variable {variable}")

    def _refuse_local_on_a_managed_runtime(
        self, values: Mapping[str, str], deployment_env: DeploymentEnv
    ) -> None:
        """Stop a deployed service from starting as a developer's machine.

        ``local`` is what allows the development identity and the other
        local-only adapters. It is also the default, so a deployment that
        forgot to name its environment would get them. The platform marks the
        compute it manages; a process that carries the mark must say it is
        ``dev`` or ``prod``.
        """
        runtime = values.get(Key.AWS_EXECUTION_ENV, "")
        if deployment_env is DeploymentEnv.LOCAL and runtime.startswith(_MANAGED_RUNTIMES):
            raise ConfigurationError(
                f"{self._name(Key.DEPLOYMENT_ENV)} must be dev or prod here: "
                f"{self._name(Key.AWS_EXECUTION_ENV)} shows that this process runs on managed "
                "compute, and only a developer's machine may run as local"
            )

    @staticmethod
    def _clean(value: str | None) -> str | None:
        """Treat an empty or blank value as not set."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def _name(self, key: str) -> str:
        """Return the variable name to show in an error about ``key``."""
        return self._by_key[key].name

    # ---------------------------------------------------------------- converting

    def _enum(
        self, values: Mapping[str, str], key: str, kind: type[EnumT], default: EnumT
    ) -> EnumT:
        raw = values.get(key)
        if raw is None:
            return default
        try:
            return kind(raw.lower())
        except ValueError:
            allowed = ", ".join(member.value for member in kind)
            close = difflib.get_close_matches(raw.lower(), [m.value for m in kind], n=1)
            raise ConfigurationError(
                f"{self._name(key)} is not one of the values it takes",
                expected=f"one of: {allowed}",
                actual=shown_value(raw),
                fix=f"set {self._name(key)}={close[0] if close else next(iter(kind)).value}",
            ) from None

    def _path(self, values: Mapping[str, str], key: str) -> Path | None:
        raw = values.get(key)
        if raw is None:
            return None
        path = Path(raw).expanduser()
        if self._base_dir is not None and not path.is_absolute():
            return self._base_dir / path
        return path

    def _json(self, values: Mapping[str, str], key: str) -> Any:
        binding = self._by_key[key]
        if binding.kind is not ValueKind.JSON:
            raise AssertionError(f"{key} is not a JSON binding")
        raw = values.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # The value is not repeated: it may hold credentials.
            raise ConfigurationError(
                f"{binding.name} is not valid JSON",
                expected="a JSON object, with keys and text in double quotes",
                actual=f"{exc.msg.lower()} at line {exc.lineno}, column {exc.colno}",
                fix=(
                    f'write it as {binding.name}={{"key": "value"}} on one line; in a .env '
                    "file do not put quotes around the whole value"
                ),
            ) from None

    def _json_object(self, values: Mapping[str, str], key: str) -> Mapping[str, object]:
        parsed = self._json(values, key)
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ConfigurationError(
                f"{self._name(key)} is not a JSON object",
                expected='a JSON object such as {"key": "value"}',
                actual=kind_of(parsed),
            )
        return parsed

    def _limits(self, values: Mapping[str, str]) -> Limits:
        try:
            return Limits.model_validate(self._json_object(values, Key.LIMITS))
        except ValidationError as exc:
            raise options_error(self._name(Key.LIMITS), Limits, exc) from None

    def _aliases(self, values: Mapping[str, str], default_provider: str) -> dict[str, ModelRef]:
        name = self._name(Key.MODEL_ALIASES)
        aliases: dict[str, ModelRef] = {}
        for alias, spec in self._json_object(values, Key.MODEL_ALIASES).items():
            if isinstance(spec, str):
                spec = {"model_id": spec}
            if not isinstance(spec, dict) or not isinstance(spec.get("model_id"), str):
                raise ConfigurationError(
                    f"{name}: alias {alias!r} names no model",
                    expected='a model ID, or an object with "model_id" and an optional "provider"',
                    actual=kind_of(spec),
                    fix=f'write {name}={{"{alias}": {{"model_id": "..."}}}}',
                )
            extra = sorted(set(spec) - _ALIAS_FIELDS)
            if extra:
                raise ConfigurationError(
                    f"{name}: alias {alias!r} has fields it does not take",
                    expected='only "model_id" and "provider"',
                    actual=f"also {', '.join(repr(field) for field in extra)}",
                )
            provider = spec.get("provider", default_provider)
            if not isinstance(provider, str):
                raise ConfigurationError(f"{name}: alias {alias!r} has an invalid provider")
            try:
                aliases[alias] = ModelRef(provider=provider, model_id=spec["model_id"])
            except ValueError as exc:
                raise ConfigurationError(f"{name}: alias {alias!r}: {exc}") from None
        return aliases

    def _data_sources(self, values: Mapping[str, str]) -> dict[str, ProviderSelection]:
        name = self._name(Key.DATA_SOURCES)
        sources: dict[str, ProviderSelection] = {}
        for source, spec in self._json_object(values, Key.DATA_SOURCES).items():
            if not _DATA_SOURCE_NAME.match(source):
                raise ConfigurationError(
                    f"{name}: a data source name must start with a lower-case letter and "
                    f"hold only lower-case letters, digits and underscores, unlike {source!r}"
                )
            if not isinstance(spec, dict) or not isinstance(spec.get(_DATA_SOURCE_KIND), str):
                raise ConfigurationError(
                    f"{name}: data source {source!r} does not say which adapter reads it",
                    expected=f'an object with "{_DATA_SOURCE_KIND}", such as '
                    f'{{"{_DATA_SOURCE_KIND}": "duckdb_csv", "data_dir": "data", '
                    '"queries_dir": "queries"}',
                    actual=kind_of(spec)
                    if not isinstance(spec, dict)
                    else f'an object without "{_DATA_SOURCE_KIND}"',
                )
            options = {key: value for key, value in spec.items() if key != _DATA_SOURCE_KIND}
            try:
                sources[source] = ProviderSelection(
                    provider=spec[_DATA_SOURCE_KIND], options=options, base_dir=self._base_dir
                )
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(f"{name}: data source {source!r}: {exc}") from None
        return sources

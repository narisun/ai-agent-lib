"""Typed, immutable configuration values.

Nothing in this module knows a variable name. The ``config`` package turns
variables into these values, and every other component receives only the
section it needs.
"""

from __future__ import annotations

import difflib
import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from ai_agent_lib_core.contracts._validation import require_identifier
from ai_agent_lib_core.contracts.errors import ConfigurationError, shown_value

__all__ = [
    "DEFAULT_MODEL_ALIAS",
    "AuditOptions",
    "BudgetLimits",
    "CallLimits",
    "DeploymentEnv",
    "ExternalSettings",
    "Limits",
    "ModelRef",
    "ModelSection",
    "NoOptions",
    "OptionsModel",
    "Profile",
    "ProviderSelection",
    "Section",
    "ServiceConfig",
    "TelemetryMode",
    "options_error",
]

DEFAULT_MODEL_ALIAS = "default"


class Profile(enum.StrEnum):
    """A named set of default adapters."""

    LOCAL = "local"
    AWS = "aws"


class DeploymentEnv(enum.StrEnum):
    """Where the process is running. Drives the one-way guard."""

    LOCAL = "local"
    DEV = "dev"
    PROD = "prod"


class TelemetryMode(enum.StrEnum):
    """Whether a service sends operational traces and metrics."""

    OFF = "off"
    OPENTELEMETRY = "opentelemetry"


class Section(enum.StrEnum):
    """Configuration sections that select an adapter. Each names a concern."""

    SECRETS = "secrets"
    AUDIT = "audit"
    IDENTITY = "identity"
    CHECKPOINT = "checkpoint"
    REGISTRY = "registry"
    POLICY = "policy"
    GUARDRAILS = "guardrails"


class OptionsModel(BaseModel):
    """Base class for an adapter's own options.

    Subclasses are frozen and reject unknown keys, so a mistyped option stops
    startup instead of being ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class NoOptions(OptionsModel):
    """The options of an adapter that takes none: any option is refused."""


class AuditOptions(OptionsModel):
    """Options every audit adapter accepts.

    Attributes:
        tracing: Also emit metadata-only OpenTelemetry events and metrics.
    """

    tracing: bool = False


OptionsT = TypeVar("OptionsT", bound=OptionsModel)


def _freeze(value: object) -> object:
    """Return a deeply immutable copy of a JSON-like value."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"options must be JSON-like values, not {type(value).__name__}")


def _rebased(options: OptionsT, base_dir: Path) -> OptionsT:
    """Return ``options`` with every relative path made relative to ``base_dir``."""
    changes: dict[str, object] = {}
    for name in type(options).model_fields:
        value = getattr(options, name)
        if isinstance(value, Path) and not value.is_absolute():
            changes[name] = base_dir / value
        elif isinstance(value, OptionsModel):
            changes[name] = _rebased(value, base_dir)
    return options.model_copy(update=changes) if changes else options


def _thaw(value: object) -> Any:
    """Return plain ``dict`` and ``list`` containers for a frozen value."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _needs(message: str) -> str:
    """Pydantic's description of what a value must be, in plain words."""
    text = re.sub(r" for <class '[\w.]+'>", "", message)
    for prefix in ("Input should be ", "Value error, "):
        text = text.removeprefix(prefix)
    if text.startswith("Input is not a valid "):
        kind = text.removeprefix("Input is not a valid ")
        return "a path, written as text" if kind == "path" else f"a valid {kind}"
    return text


def options_error(owner: str, model: type[BaseModel], exc: ValidationError) -> ConfigurationError:
    """Turn a validation failure of an options model into an error a developer can act on.

    For each problem it says where it is, what the option needs and what was
    given; for an unknown option it suggests the closest known one.

    Args:
        owner: What the options belong to, for example ``"provider 'jsonl'"``.
        model: The options model that was validated.
        exc: What validation found.
    """
    known = sorted(field.alias or name for name, field in model.model_fields.items())
    expected: list[str] = []
    actual: list[str] = []
    for error in exc.errors(include_url=False):
        place = ".".join(str(part) for part in error["loc"]) or "the options"
        if error["type"] == "extra_forbidden":
            close = difflib.get_close_matches(str(error["loc"][-1]), known, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            expected.append(f"only known options: {', '.join(known)}")
            actual.append(f"an unknown option {place!r}{hint}")
        elif error["type"] == "missing":
            expected.append(f"{place}: a value; it is required")
            actual.append(f"no {place}")
        else:
            expected.append(f"{place}: {_needs(error['msg'])}")
            actual.append(f"{place} = {shown_value(error.get('input'))}")
    count = len(actual)
    return ConfigurationError(
        f"the options of {owner} are not valid ({count} problem{'s' if count != 1 else ''})",
        expected="; ".join(dict.fromkeys(expected)),
        actual="; ".join(actual),
        fix="correct the options; the developer guide lists every option with its default",
    )


class CallLimits(OptionsModel):
    """How long one kind of call may take, and how often it is tried again.

    Attributes:
        timeout_seconds: How long one attempt may take before it is given up as
            a transient failure. ``null`` for no limit.
        retries: How many more attempts a transient failure gets: throttling,
            a timeout or a server error. A tool call is tried again only when
            the tool only reads. A denial is never tried again.
        backoff_seconds: The wait before the first retry. Each later wait is
            twice the one before, with random jitter.
        max_backoff_seconds: The longest wait between two attempts.
    """

    timeout_seconds: float | None = Field(default=None, gt=0)
    retries: int = Field(default=0, ge=0, le=10)
    backoff_seconds: float = Field(default=0.5, ge=0)
    max_backoff_seconds: float = Field(default=8.0, ge=0)


class BudgetLimits(OptionsModel):
    """How much one request may use before it is stopped. ``null`` for no limit.

    A request is everything done for one ``request_id``. The budget is the loop
    guard of a graph that keeps calling its model or its tools.

    Attributes:
        model_calls: The most model calls one request may make.
        tool_calls: The most tool calls one request may make.
        tokens: The most input and output tokens its model calls may use together.
    """

    model_calls: int | None = Field(default=50, ge=1)
    tool_calls: int | None = Field(default=100, ge=1)
    tokens: int | None = Field(default=None, ge=1)


def _model_limits() -> CallLimits:
    return CallLimits(timeout_seconds=120.0, retries=2)


def _tool_limits() -> CallLimits:
    return CallLimits(timeout_seconds=60.0, retries=1)


class Limits(OptionsModel):
    """Timeouts, retries and budgets: the operational limits of governed calls.

    Attributes:
        model: Limits of a model call.
        tool: Limits of a tool call.
        budget: What one request may use.
    """

    model: CallLimits = Field(default_factory=_model_limits)
    tool: CallLimits = Field(default_factory=_tool_limits)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)

    @model_validator(mode="before")
    @classmethod
    def _over_the_defaults(cls, given: Any) -> Any:
        """Let a key given for ``model`` or ``tool`` change that one limit and keep the rest."""
        if not isinstance(given, Mapping):
            return given
        merged = dict(given)
        for name, defaults in (("model", _model_limits()), ("tool", _tool_limits())):
            if isinstance(given.get(name), Mapping):
                merged[name] = {**defaults.model_dump(), **given[name]}
        return merged


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """Which adapter serves a section, and that adapter's raw options.

    Attributes:
        provider: The registered name of the adapter.
        options: Raw, JSON-like options. The adapter validates them against its
            own :class:`OptionsModel` through :meth:`parse_options`.
        base_dir: The folder a relative path in the options is relative to:
            the folder of the configuration file the options came from. Without
            it a relative path is relative to the working directory.
    """

    provider: str
    options: Mapping[str, object] = field(default_factory=dict)
    base_dir: Path | None = None

    def __post_init__(self) -> None:
        require_identifier("provider", self.provider)
        if not isinstance(self.options, Mapping):
            raise TypeError("options must be a mapping")
        object.__setattr__(self, "options", _freeze(self.options))

    def parse_options(self, model: type[OptionsT]) -> OptionsT:
        """Validate the raw options against ``model``.

        Raises:
            ConfigurationError: If an option is unknown, missing or invalid. The
                message names the fields and never repeats their values.
        """
        try:
            parsed = model.model_validate(_thaw(self.options))
        except ValidationError as exc:
            raise options_error(f"provider {self.provider!r}", model, exc) from None
        return _rebased(parsed, self.base_dir) if self.base_dir is not None else parsed


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A concrete model: which provider serves it and its identifier there."""

    provider: str
    model_id: str

    def __post_init__(self) -> None:
        require_identifier("provider", self.provider)
        require_identifier("model_id", self.model_id)


@dataclass(frozen=True, slots=True)
class ModelSection:
    """Model configuration: a default provider and the alias table.

    Attributes:
        provider: The provider used when an alias does not name one.
        model_id: The model behind the ``default`` alias, if configured.
        aliases: Further aliases such as ``fast`` or ``judge``.
    """

    provider: str
    model_id: str | None = None
    aliases: Mapping[str, ModelRef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier("provider", self.provider)
        if self.model_id is not None:
            require_identifier("model_id", self.model_id)
        for alias, ref in self.aliases.items():
            require_identifier("alias", alias)
            if not isinstance(ref, ModelRef):
                raise TypeError(f"alias {alias!r} must map to a ModelRef")
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))

    def resolve(self, alias: str = DEFAULT_MODEL_ALIAS) -> ModelRef:
        """Return the concrete model behind ``alias``.

        Raises:
            ConfigurationError: If the alias is not configured.
        """
        if alias in self.aliases:
            return self.aliases[alias]
        if alias == DEFAULT_MODEL_ALIAS and self.model_id is not None:
            return ModelRef(provider=self.provider, model_id=self.model_id)
        known = sorted({*self.aliases, *([DEFAULT_MODEL_ALIAS] if self.model_id else [])})
        raise ConfigurationError(
            f"model alias {alias!r} is not configured; configured aliases: {known or 'none'}"
        )


@dataclass(frozen=True, slots=True)
class ExternalSettings:
    """Values read from variables that belong to other tools.

    Attributes:
        aws_profile: The AWS named profile, for example one signed in through SSO.
        aws_region: The AWS region.
        aws_ca_bundle: A CA file for AWS clients.
        https_proxy: The outbound proxy. Hidden from ``repr`` because proxy URLs
            can carry credentials.
        no_proxy: Hosts that bypass the proxy.
    """

    aws_profile: str | None = None
    aws_region: str | None = None
    aws_ca_bundle: Path | None = None
    https_proxy: str | None = field(default=None, repr=False)
    no_proxy: str | None = None


def _default_sections() -> Mapping[Section, ProviderSelection]:
    return {section: ProviderSelection(provider="fake") for section in Section}


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """An immutable snapshot of resolved configuration.

    It holds typed values and adapter names, never variable names. Secrets are
    stored as :class:`pydantic.SecretStr`, which masks them in ``repr`` and logs.

    Attributes:
        profile: The named set of default adapters that was applied.
        deployment_env: Where the process is running.
        telemetry: Whether operational traces and metrics are sent.
        limits: Timeouts, retries and budgets of governed calls.
        tls_ca_bundle: The enterprise CA file, used by the AWS clients.
        model: Model providers and aliases.
        sections: The adapter selected for each section.
        secrets: Named secret values available to the environment secrets adapter.
        external: Values read from third-party variables.
        data_sources: The adapter selected for each named data source. A service
            can have any number of them, so they are named instead of being one
            section.
    """

    profile: Profile = Profile.LOCAL
    deployment_env: DeploymentEnv = DeploymentEnv.LOCAL
    telemetry: TelemetryMode = TelemetryMode.OFF
    limits: Limits = field(default_factory=Limits)
    tls_ca_bundle: Path | None = None
    model: ModelSection = field(
        default_factory=lambda: ModelSection(provider="fake", model_id="fake-model")
    )
    sections: Mapping[Section, ProviderSelection] = field(default_factory=_default_sections)
    secrets: Mapping[str, SecretStr] = field(default_factory=dict)
    external: ExternalSettings = field(default_factory=ExternalSettings)
    data_sources: Mapping[str, ProviderSelection] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile", Profile(self.profile))
        object.__setattr__(self, "deployment_env", DeploymentEnv(self.deployment_env))
        sections = {Section(name): selection for name, selection in self.sections.items()}
        for name, selection in sections.items():
            if not isinstance(selection, ProviderSelection):
                raise TypeError(f"section {name.value!r} must be a ProviderSelection")
        for secret_name, secret in self.secrets.items():
            if not isinstance(secret, SecretStr):
                raise TypeError(f"secret {secret_name!r} must be a SecretStr")
        object.__setattr__(self, "sections", MappingProxyType(sections))
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))
        for source_name, selection in self.data_sources.items():
            require_identifier("data source name", source_name)
            if not isinstance(selection, ProviderSelection):
                raise TypeError(f"data source {source_name!r} must be a ProviderSelection")
        object.__setattr__(self, "data_sources", MappingProxyType(dict(self.data_sources)))

    def section(self, name: Section) -> ProviderSelection:
        """Return the adapter selection for ``name``.

        Raises:
            ConfigurationError: If the section has no selection.
        """
        try:
            return self.sections[Section(name)]
        except KeyError:
            raise ConfigurationError(f"section {name!r} is not configured") from None

    def with_section(self, name: Section, selection: ProviderSelection) -> ServiceConfig:
        """Return a copy with one section replaced."""
        return replace(self, sections={**self.sections, Section(name): selection})

    def with_data_source(self, name: str, selection: ProviderSelection) -> ServiceConfig:
        """Return a copy with one data source added or replaced."""
        return replace(self, data_sources={**self.data_sources, name: selection})

    @classmethod
    def for_testing(cls, **overrides: Any) -> ServiceConfig:
        """Build a config for tests: every section uses the ``fake`` adapter.

        Tests build configuration from values, so they never mention a variable
        name. Keyword arguments replace individual fields. ``sections`` may
        name only the sections a test cares about; the rest stay ``fake``.
        """
        sections = {**_default_sections(), **overrides.pop("sections", {})}
        return cls(sections=sections, **overrides)

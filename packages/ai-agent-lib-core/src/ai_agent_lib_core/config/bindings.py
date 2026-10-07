"""The binding table: the one place where variable names are written.

Each :class:`Binding` maps a variable name to a logical key. The resolver works
with keys only, so renaming a variable, or changing the prefix, is an edit to
this module and nothing else. A binding can list deprecated names, which keep
working with a warning.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ai_agent_lib_core.contracts import Section

__all__ = [
    "DEFAULT_BINDINGS",
    "PREFIX",
    "SECRET_KEY_PREFIX",
    "SECRET_VARIABLE_PREFIX",
    "Binding",
    "Key",
    "ValueKind",
    "options_key",
    "provider_key",
    "secret_key",
    "secret_name",
    "validate_bindings",
]

PREFIX = "EAP_"
"""Prefix of every variable the library owns: Enterprise Agentic Platform."""

_OWNED_NAME = re.compile(rf"^{PREFIX}[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")
_ANY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class ValueKind(enum.Enum):
    """How a raw value is interpreted."""

    TEXT = "text"
    JSON = "json"


class Key(enum.StrEnum):
    """Logical keys for the settings that are not per-section."""

    PROFILE = "platform.profile"
    DEPLOYMENT_ENV = "platform.deployment_env"
    RUNTIME_POLICY = "platform.runtime_policy"
    TLS_CA_BUNDLE = "platform.tls_ca_bundle"
    MODEL_PROVIDER = "model.provider"
    MODEL_ID = "model.model_id"
    MODEL_ALIASES = "model.aliases"
    DATA_SOURCES = "data.sources"
    AWS_PROFILE = "external.aws_profile"
    AWS_REGION = "external.aws_region"
    AWS_CA_BUNDLE = "external.aws_ca_bundle"
    AWS_EXECUTION_ENV = "external.aws_execution_env"
    HTTPS_PROXY = "external.https_proxy"
    NO_PROXY = "external.no_proxy"


def provider_key(section: Section) -> str:
    """Return the logical key of a section's adapter selector."""
    return f"section.{section.value}.provider"


def options_key(section: Section) -> str:
    """Return the logical key of a section's adapter options."""
    return f"section.{section.value}.options"


SECRET_KEY_PREFIX = "secret."  # noqa: S105 - a key namespace, not a credential


def secret_key(name: str) -> str:
    """Return the logical key of a named secret."""
    return f"{SECRET_KEY_PREFIX}{name}"


SECRET_VARIABLE_PREFIX = f"{PREFIX}SECRET_"
"""Prefix of the variables that each supply one named secret.

Adapters ask the secrets port for a secret by name. In local development the
``env`` secrets adapter serves them from these variables: the variable
``<prefix>RATES_TOKEN`` supplies the secret ``rates_token``.
"""

_SECRET_VARIABLE = re.compile(rf"^{SECRET_VARIABLE_PREFIX}(?P<name>[A-Z][A-Z0-9]*(_[A-Z0-9]+)*)$")


def secret_name(variable: str) -> str | None:
    """Return the secret a variable supplies, or ``None`` if it is not a secret variable."""
    match = _SECRET_VARIABLE.match(variable)
    return match.group("name").lower() if match is not None else None


@dataclass(frozen=True, slots=True)
class Binding:
    """One variable and the logical key it feeds.

    Attributes:
        name: The current variable name.
        key: The logical key the resolver reads.
        description: One line for the generated reference.
        kind: How the raw value is interpreted.
        sensitive: Whether the value must be masked wherever it is shown.
        deprecated_names: Older names that still work, with a warning.
        alternate_names: Other accepted names, read without a warning when the
            current name is not set. Used for third-party variables that have
            more than one conventional spelling.
        example: An example value for the generated ``.env.example``.
    """

    name: str
    key: str
    description: str
    kind: ValueKind = ValueKind.TEXT
    sensitive: bool = False
    deprecated_names: tuple[str, ...] = ()
    alternate_names: tuple[str, ...] = ()
    example: str = ""

    @property
    def owned(self) -> bool:
        """Whether the library owns the name, as opposed to another tool."""
        return self.name.startswith(PREFIX)

    @property
    def all_names(self) -> tuple[str, ...]:
        """Every accepted name, the current one first."""
        return (self.name, *self.alternate_names, *self.deprecated_names)


def _section_bindings() -> list[Binding]:
    bindings: list[Binding] = []
    for section in Section:
        stem = f"{PREFIX}{section.name}"
        bindings.append(
            Binding(
                name=f"{stem}_PROVIDER",
                key=provider_key(section),
                description=f"Adapter that serves the {section.value} section.",
            )
        )
        bindings.append(
            Binding(
                name=f"{stem}_OPTIONS",
                key=options_key(section),
                description=f"JSON options for the {section.value} adapter.",
                kind=ValueKind.JSON,
                example="{}",
            )
        )
    return bindings


def _default_bindings() -> tuple[Binding, ...]:
    return (
        Binding(
            name=f"{PREFIX}PROFILE",
            key=Key.PROFILE,
            description="Default adapter set: local or aws.",
            example="local",
        ),
        Binding(
            name=f"{PREFIX}DEPLOYMENT_ENV",
            key=Key.DEPLOYMENT_ENV,
            description="Where the process runs: local, dev or prod. Drives the one-way guard.",
            example="local",
        ),
        Binding(
            name=f"{PREFIX}RUNTIME_POLICY",
            key=Key.RUNTIME_POLICY,
            description="Named bundle of operational controls. Reserved: read but not used yet.",
        ),
        Binding(
            name=f"{PREFIX}TLS_CA_BUNDLE",
            key=Key.TLS_CA_BUNDLE,
            description="Enterprise CA file for the AWS clients, such as the Bedrock model client.",
        ),
        Binding(
            name=f"{PREFIX}MODEL_PROVIDER",
            key=Key.MODEL_PROVIDER,
            description="Provider of the default model alias.",
            example="anthropic",
        ),
        Binding(
            name=f"{PREFIX}MODEL_ID",
            key=Key.MODEL_ID,
            description="Model behind the default alias.",
        ),
        Binding(
            name=f"{PREFIX}MODEL_ALIASES",
            key=Key.MODEL_ALIASES,
            description='JSON map of further aliases, e.g. {"fast": {"model_id": "..."}}.',
            kind=ValueKind.JSON,
        ),
        *_section_bindings(),
        Binding(
            name=f"{PREFIX}DATA_SOURCES",
            key=Key.DATA_SOURCES,
            description=(
                'JSON map of named data sources, e.g. {"accounts": {"kind": "duckdb_csv", '
                '"data_dir": "data", "queries_dir": "queries"}}.'
            ),
            kind=ValueKind.JSON,
        ),
        Binding(
            name="AWS_PROFILE",
            key=Key.AWS_PROFILE,
            description="AWS named profile, for example one signed in through SSO.",
        ),
        Binding(
            name="AWS_REGION",
            key=Key.AWS_REGION,
            description="AWS region.",
            alternate_names=("AWS_DEFAULT_REGION",),
        ),
        Binding(
            name="AWS_CA_BUNDLE",
            key=Key.AWS_CA_BUNDLE,
            description="CA file for AWS clients.",
        ),
        Binding(
            name="AWS_EXECUTION_ENV",
            key=Key.AWS_EXECUTION_ENV,
            description=(
                "Set by AWS inside ECS, Fargate and Lambda. A process that has it must "
                "name its deployment environment as dev or prod."
            ),
        ),
        Binding(
            name="HTTPS_PROXY",
            key=Key.HTTPS_PROXY,
            description="Outbound proxy.",
            sensitive=True,
            alternate_names=("https_proxy",),
        ),
        Binding(
            name="NO_PROXY",
            key=Key.NO_PROXY,
            description="Hosts that bypass the proxy.",
            alternate_names=("no_proxy",),
        ),
        Binding(
            name="ANTHROPIC_API_KEY",
            key=secret_key("anthropic_api_key"),
            description="API key for the anthropic model provider.",
            sensitive=True,
        ),
    )


def validate_bindings(bindings: Sequence[Binding]) -> None:
    """Check that a binding table is well formed.

    Raises:
        ValueError: If a name or key is used twice, or a name is malformed.
    """
    seen_names: set[str] = set()
    seen_keys: set[str] = set()
    for binding in bindings:
        if binding.key in seen_keys:
            raise ValueError(f"key {binding.key!r} is bound more than once")
        seen_keys.add(binding.key)
        pattern = _OWNED_NAME if binding.owned else _ANY_NAME
        for name in binding.all_names:
            if name in seen_names:
                raise ValueError(f"variable {name!r} is bound more than once")
            seen_names.add(name)
        if not pattern.match(binding.name):
            raise ValueError(f"variable name {binding.name!r} is malformed")
        for name in (*binding.alternate_names, *binding.deprecated_names):
            if not _ANY_NAME.match(name):
                raise ValueError(f"variable name {name!r} is malformed")


DEFAULT_BINDINGS: tuple[Binding, ...] = _default_bindings()
validate_bindings(DEFAULT_BINDINGS)

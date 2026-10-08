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
    TELEMETRY = "platform.telemetry"
    LIMITS = "platform.limits"
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
        details: A longer explanation for the reference documents: what the
            value does and when to set it. Text in backticks is code.
        default: What applies when the variable is not set, in words, for a
            setting whose default is not chosen by the profile. Empty means
            nothing is set. Profile defaults are read from the profiles.
    """

    name: str
    key: str
    description: str
    kind: ValueKind = ValueKind.TEXT
    sensitive: bool = False
    deprecated_names: tuple[str, ...] = ()
    alternate_names: tuple[str, ...] = ()
    example: str = ""
    details: str = ""
    default: str = ""

    @property
    def owned(self) -> bool:
        """Whether the library owns the name, as opposed to another tool."""
        return self.name.startswith(PREFIX)

    @property
    def all_names(self) -> tuple[str, ...]:
        """Every accepted name, the current one first."""
        return (self.name, *self.alternate_names, *self.deprecated_names)


_SECTION_DETAILS = {
    Section.SECRETS: "Where adapters get credentials by name.",
    Section.AUDIT: "Where the audit trail of every governed call goes.",
    Section.IDENTITY: (
        "Who the caller is: a development identity locally, OAuth 2.0 JWTs when deployed."
    ),
    Section.CHECKPOINT: "Where LangGraph keeps conversation state between turns.",
    Section.REGISTRY: "The agent and MCP tool registries.",
    Section.POLICY: "Who may do what: the in-process rules engine locally, OPA when deployed.",
    Section.GUARDRAILS: "Content checks on prompts, replies and tool results.",
}


def _section_bindings() -> list[Binding]:
    bindings: list[Binding] = []
    for section in Section:
        stem = f"{PREFIX}{section.name}"
        bindings.append(
            Binding(
                name=f"{stem}_PROVIDER",
                key=provider_key(section),
                description=f"Adapter that serves the {section.value} section.",
                details=_SECTION_DETAILS[section],
            )
        )
        bindings.append(
            Binding(
                name=f"{stem}_OPTIONS",
                key=options_key(section),
                description=f"JSON options for the {section.value} adapter.",
                kind=ValueKind.JSON,
                example="{}",
                details=(
                    f"Options of the selected {section.value} adapter, as one JSON object. "
                    "Unknown keys stop startup. `agentlib config options "
                    f"{section.value}` lists each adapter's options."
                ),
                default="`{}`: the adapter's defaults",
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
            details=(
                "Chooses the whole default adapter set in one word. `local` runs with no cloud "
                "account: files, SQLite, an in-process rules engine and a development identity. "
                "`aws` selects the managed adapters. A profile only supplies defaults; any section"
                " can still name another adapter."
            ),
        ),
        Binding(
            name=f"{PREFIX}DEPLOYMENT_ENV",
            key=Key.DEPLOYMENT_ENV,
            description="Where the process runs: local, dev or prod. Drives the one-way guard.",
            example="local",
            details=(
                "Says where the process runs. Anything other than `local` turns on the one-way "
                "guard: adapters registered as local only (the development identity, the rules "
                "engine, JSONL audit, SQLite, DuckDB over CSV, the fake model) refuse to start. "
                "Inside ECS, Fargate or Lambda the value must be dev or prod."
            ),
        ),
        Binding(
            name=f"{PREFIX}TELEMETRY",
            key=Key.TELEMETRY,
            description="Operational traces and metrics: off or opentelemetry.",
            example="off",
            details=(
                "With `opentelemetry` every governed call is a span named after the GenAI "
                "conventions, and the `agentlib.events` and `agentlib.duration` metrics are "
                "recorded, through the OpenTelemetry API. The service sends them by calling "
                "`configure_telemetry()`; where to is set with the standard `OTEL_` variables. "
                "The audit option `tracing: true` also turns it on."
            ),
        ),
        Binding(
            name=f"{PREFIX}LIMITS",
            key=Key.LIMITS,
            description="JSON timeouts, retries and budgets of governed calls.",
            kind=ValueKind.JSON,
            example='{"budget": {"model_calls": 20}}',
            details=(
                "Keys `model` and `tool`, each with `timeout_seconds`, `retries`, "
                "`backoff_seconds` and `max_backoff_seconds`, and `budget` with `model_calls`, "
                "`tool_calls` and `tokens` per request. Only transient failures are tried "
                "again, and a tool only when it only reads. A request over its budget stops "
                "with BudgetExceeded. `agentlib config options limits` lists every key."
            ),
            default=(
                "model: 120 s, 2 retries; tool: 60 s, 1 retry; budget: 50 model calls and "
                "100 tool calls per request"
            ),
        ),
        Binding(
            name=f"{PREFIX}TLS_CA_BUNDLE",
            key=Key.TLS_CA_BUNDLE,
            description="Enterprise CA file for the AWS clients, such as the Bedrock model client.",
            details=(
                "Path to the enterprise CA bundle that the AWS clients trust, for networks that "
                "inspect TLS. It applies to the Bedrock model client and the other boto3 clients "
                "only; HTTP adapters take their own `ca_file` option."
            ),
        ),
        Binding(
            name=f"{PREFIX}MODEL_PROVIDER",
            key=Key.MODEL_PROVIDER,
            description="Provider of the default model alias.",
            example="anthropic",
            details=(
                "The provider of the model behind the `default` alias, and of any alias that does "
                "not name its own provider."
            ),
        ),
        Binding(
            name=f"{PREFIX}MODEL_ID",
            key=Key.MODEL_ID,
            description="Model behind the default alias.",
            details=(
                "The model ID behind the `default` alias, as the provider spells it. Until it is "
                "set, `services.model()` raises a ConfigurationError that says to set it. A "
                "service with no model, such as an MCP server, leaves it unset."
            ),
            default="not set: required before `services.model()`",
        ),
        Binding(
            name=f"{PREFIX}MODEL_ALIASES",
            key=Key.MODEL_ALIASES,
            description='JSON map of further aliases, e.g. {"fast": {"model_id": "..."}}.',
            kind=ValueKind.JSON,
            details=(
                "Further aliases, so code asks for a role (`fast`, `judge`) rather than a model. A"
                " value is a model ID or an object with `model_id` and an optional `provider`."
            ),
            default="`{}`: only the default alias",
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
            details=(
                "Named data sources for MCP tools. Each entry names its adapter with `kind`; the "
                "other keys are that adapter's options. Code asks for "
                '`services.data_source("ledger")` and never sees the backend.'
            ),
            default="`{}`: no data sources",
        ),
        Binding(
            name="AWS_PROFILE",
            key=Key.AWS_PROFILE,
            description="AWS named profile, for example one signed in through SSO.",
            details=("Read by the AWS session for local runs, for example after `aws sso login`."),
        ),
        Binding(
            name="AWS_REGION",
            key=Key.AWS_REGION,
            description="AWS region.",
            alternate_names=("AWS_DEFAULT_REGION",),
            details=("Region of every AWS client the library builds."),
        ),
        Binding(
            name="AWS_CA_BUNDLE",
            key=Key.AWS_CA_BUNDLE,
            description="CA file for AWS clients.",
            details=("The AWS SDK's own CA setting, honoured as boto3 does."),
        ),
        Binding(
            name="AWS_EXECUTION_ENV",
            key=Key.AWS_EXECUTION_ENV,
            description=(
                "Set by AWS inside ECS, Fargate and Lambda. A process that has it must "
                "name its deployment environment as dev or prod."
            ),
            details=(
                "Set by AWS on its managed runtimes. Its presence with `EAP_DEPLOYMENT_ENV=local` "
                "stops startup, so a container never runs with development adapters by accident."
            ),
        ),
        Binding(
            name="HTTPS_PROXY",
            key=Key.HTTPS_PROXY,
            description="Outbound proxy.",
            sensitive=True,
            alternate_names=("https_proxy",),
            details=(
                "Outbound proxy. The AWS clients and the anthropic model use it; the jwt identity "
                "provider and rest data sources use it only when their `use_proxy` option is set. "
                "Masked wherever it is shown, since it can carry credentials."
            ),
        ),
        Binding(
            name="NO_PROXY",
            key=Key.NO_PROXY,
            description="Hosts that bypass the proxy.",
            alternate_names=("no_proxy",),
            details=("Comma-separated hosts the AWS clients reach without the proxy."),
        ),
        Binding(
            name="ANTHROPIC_API_KEY",
            key=secret_key("anthropic_api_key"),
            description="API key for the anthropic model provider.",
            sensitive=True,
            details=(
                "Supplies the secret `anthropic_api_key`, which the anthropic model provider asks "
                "the secrets port for. Masked wherever it is shown."
            ),
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

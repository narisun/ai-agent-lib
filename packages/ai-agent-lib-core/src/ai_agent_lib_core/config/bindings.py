"""The binding table: the one place where variable names are written.

Each :class:`Binding` maps a variable name to a logical key. The resolver works
with keys only, so renaming a variable, or changing the prefix, is an edit to
this module and nothing else. A binding can list deprecated names, which keep
working with a warning.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from ai_agent_lib_core.contracts import Section

__all__ = [
    "DEFAULT_BINDINGS",
    "LEGACY_PREFIX",
    "LEGACY_SECRET_VARIABLE_PREFIX",
    "LOCAL_ONLY_ADAPTERS",
    "PREFIX",
    "SECRET_KEY_PREFIX",
    "SECRET_VARIABLE_PREFIX",
    "Binding",
    "Key",
    "ValueKind",
    "legacy_spelling",
    "options_key",
    "provider_key",
    "secret_key",
    "secret_name",
    "validate_bindings",
]

PREFIX = "AC2_"
"""Prefix of every variable the library owns."""

LEGACY_PREFIX = "EAP_"
"""The earlier prefix. Its spellings keep working for one minor release, with a warning."""

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
    MODEL_OPTIONS = "model.options"
    MODEL_TRACE_PATH = "model.trace_path"
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

LEGACY_SECRET_VARIABLE_PREFIX = f"{LEGACY_PREFIX}SECRET_"
"""The earlier prefix of the secret variables, accepted with a warning."""

_SECRET_VARIABLE = re.compile(
    rf"^(?:{SECRET_VARIABLE_PREFIX}|{LEGACY_SECRET_VARIABLE_PREFIX})"
    r"(?P<name>[A-Z][A-Z0-9]*(_[A-Z0-9]+)*)$"
)


def secret_name(variable: str) -> str | None:
    """Return the secret a variable supplies, or ``None`` if it is not a secret variable.

    Both the current and the legacy spelling of the variable are recognised.
    """
    match = _SECRET_VARIABLE.match(variable)
    return match.group("name").lower() if match is not None else None


def legacy_spelling(name: str) -> str:
    """Return the legacy spelling of an owned variable name."""
    return LEGACY_PREFIX + name.removeprefix(PREFIX)


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


LOCAL_ONLY_ADAPTERS: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("identity", "static"): "the development identity",
        ("policy", "rules"): "the rules engine",
        ("audit", "jsonl"): "JSONL audit",
        ("checkpoint", "sqlite"): "SQLite",
        ("data", "duckdb_csv"): "DuckDB over CSV",
        ("model", "fake"): "the fake model",
    }
)
"""The adapters registered as local only, by ``(port, name)``, with how the reference says them.

The one-way guard refuses these outside local development. This module cannot
ask the provider registry, because configuration may not import it, so the
list is kept here and an architecture test compares it with the registry.
"""

_LOCAL_ONLY_PHRASES = ", ".join(LOCAL_ONLY_ADAPTERS.values())

_SECTION_DETAILS = {
    Section.SECRETS: (
        "Where adapters get credentials by name: `env` reads `AC2_SECRET_<NAME>` variables "
        "locally, `secrets_manager` is the AWS option."
    ),
    Section.AUDIT: (
        "Where the audit trail of every governed call goes: `jsonl` writes a local file and is "
        "local-only, `firehose` is the AWS option."
    ),
    Section.IDENTITY: (
        "Who the caller is: a development identity (`static`, local-only) locally, "
        "OAuth 2.0 JWTs (`jwt`) when deployed."
    ),
    Section.CHECKPOINT: (
        "Where LangGraph keeps conversation state between turns: `sqlite` is a local file and "
        "is local-only, `postgres` is the AWS option."
    ),
    Section.REGISTRY: (
        "The agent and MCP tool registries: `file` reads them from disk and can be deployed, "
        "`s3_file` is the AWS option."
    ),
    Section.POLICY: (
        "Who may do what: the in-process rules engine (`rules`, local-only) locally, "
        "OPA (`opa`) when deployed."
    ),
    Section.GUARDRAILS: (
        "Content checks on prompts, replies and tool results: `patterns` checks text patterns "
        "in the process and can be deployed, `bedrock` is the AWS option."
    ),
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


def _with_legacy_spelling(binding: Binding) -> Binding:
    """Return an owned binding that also accepts its legacy spelling, with a warning."""
    if not binding.owned:
        return binding
    return replace(
        binding, deprecated_names=(*binding.deprecated_names, legacy_spelling(binding.name))
    )


def _default_bindings() -> tuple[Binding, ...]:
    return tuple(_with_legacy_spelling(binding) for binding in _owned_and_external_bindings())


def _owned_and_external_bindings() -> tuple[Binding, ...]:
    return (
        Binding(
            name=f"{PREFIX}PROFILE",
            key=Key.PROFILE,
            description="Default adapter set: local or aws.",
            example="local",
            details=(
                "`local` selects file storage, SQLite, local rules, a development identity, "
                "and the Anthropic model provider. `aws` selects the AWS deployment defaults. "
                "Override individual providers to match your environment; use "
                "`AC2_MODEL_PROVIDER=fake` for the local tutorial."
            ),
        ),
        Binding(
            name=f"{PREFIX}DEPLOYMENT_ENV",
            key=Key.DEPLOYMENT_ENV,
            description="Where the process runs: local, dev or prod. Drives the one-way guard.",
            example="local",
            details=(
                "Says where the process runs. Anything other than `local` turns on the one-way "
                f"guard: adapters registered as local only ({_LOCAL_ONLY_PHRASES}) refuse to "
                "start. Inside ECS, Fargate or Lambda the value must be dev or prod."
            ),
        ),
        Binding(
            name=f"{PREFIX}TELEMETRY",
            key=Key.TELEMETRY,
            description="Operational traces and metrics: off, opentelemetry or ambient.",
            example="off",
            details=(
                "With `opentelemetry` every governed call is a span named after the GenAI "
                "conventions, and the `agentlib.events` and `agentlib.duration` metrics are "
                "recorded, through the OpenTelemetry API. A service entry point sends them by "
                "calling `start_telemetry()` (`run_agent` and `run_service` do), which installs "
                "the providers through `configure_telemetry()` when this is on; where to is set "
                "with the standard `OTEL_` variables. With `ambient` the same signals go to the "
                "providers the host installed, as `opentelemetry-instrument` does, and the "
                "service installs none; it refuses to start when no provider is found. The audit "
                "option `tracing: true` also turns it on."
            ),
        ),
        Binding(
            name=f"{PREFIX}MODEL_TRACE_PATH",
            key=Key.MODEL_TRACE_PATH,
            description="Optional JSONL file for diagnostic model requests and responses.",
            example=".agentlib/model-trace.jsonl",
            details=(
                "Set a path such as `.agentlib/model-trace.jsonl` to inspect complete "
                "LangChain messages, tool definitions, and replies at the provider boundary. "
                "Relative paths resolve from the service's `.env` folder. Unset or blank "
                "disables capture. Credential patterns are masked, but conversation content "
                "is retained: use test data and restrict access to the file. Each provider "
                "attempt is recorded separately; SDK HTTP bodies are outside this trace."
            ),
            default="disabled",
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
            description="Enterprise CA file for the Bedrock clients.",
            details=(
                "Path to the enterprise CA bundle for Bedrock clients, for networks that "
                "inspect TLS. It overrides `AWS_CA_BUNDLE` for Bedrock only; other AWS clients "
                "use `AWS_CA_BUNDLE`. HTTP adapters take `ca_file`, checkpoint PostgreSQL "
                "takes `ca_bundle`, and pip uses its own certificate configuration."
            ),
        ),
        Binding(
            name=f"{PREFIX}MODEL_PROVIDER",
            key=Key.MODEL_PROVIDER,
            description="Provider of the default model alias.",
            example="anthropic",
            details=(
                "The provider of the model behind the `default` alias, and of any alias that does "
                "not name its own provider. `anthropic` calls the Anthropic API, `bedrock` is the "
                "AWS option, and `fake` replies from the process without a network call and is "
                "local-only."
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
            name=f"{PREFIX}MODEL_OPTIONS",
            key=Key.MODEL_OPTIONS,
            description="JSON options keyed by model provider name.",
            kind=ValueKind.JSON,
            details=(
                'For Anthropic prompt caching, use {"anthropic": {"prompt_caching": true, '
                '"cache_ttl": "5m"}}. Options apply to every alias using that provider. '
                "Unknown options stop startup. `agentlib config options model` lists options."
            ),
            default="`{}`: each provider's defaults; Anthropic prompt caching is off",
        ),
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
                "other keys are that adapter's options. `duckdb_csv` reads local CSV files and is "
                "the local-only kind; `rest` and `redshift_data` can be deployed. Code asks for "
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
                "Set by AWS on its managed runtimes. Its presence with `AC2_DEPLOYMENT_ENV=local` "
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

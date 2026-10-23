"""Local and AWS examples for the guide's configuration table.

Names and profile defaults come from the library. The examples describe this
team's deployment choices; they are not additional runtime defaults. Tests
validate their JSON against the registered adapters without contacting AWS.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from ai_agent_lib_core.config import Key
from ai_agent_lib_core.config.bindings import options_key, provider_key, secret_key
from ai_agent_lib_core.config.profiles import PROFILE_DEFAULTS
from ai_agent_lib_core.contracts import Profile, Section


@dataclass(frozen=True)
class EnvironmentExample:
    """Suggested local and AWS values, with deployment guidance in the notes."""

    local: str
    aws: str
    notes: str


LOCAL_OPTIONS: Mapping[Section, dict[str, object]] = {
    Section.SECRETS: {},
    Section.AUDIT: {"path": ".agentcraft2/audit.jsonl"},
    Section.IDENTITY: {"subject": "dev-user", "tenant": "dev-tenant", "roles": ["analyst"]},
    Section.CHECKPOINT: {"path": ".agentcraft2/checkpoints.sqlite"},
    Section.REGISTRY: {
        "agents_path": "../../registry/agents.yaml",
        "tools_path": "../../registry/mcp-tools.yaml",
    },
    Section.POLICY: {"path": "../../policies/agentcraft2/rules/data.yaml"},
    Section.GUARDRAILS: {},
}

AWS_OPTIONS: Mapping[Section, dict[str, object]] = {
    Section.SECRETS: {"prefix": "eap/helper/"},
    Section.AUDIT: {"stream": "team-agent-audit"},
    Section.IDENTITY: {
        "preset": "entra",
        "tenant_id": "<tenant-id>",
        "audience": ["<agent-client-id>", "api://<agent-client-id>"],
        "exchange": {"client_id": "<agent-client-id>", "client_secret": "entra_client_secret"},
    },
    Section.CHECKPOINT: {
        "host": "<database-endpoint>",
        "database": "agent_operations",
        "user": "helper",
        "ca_bundle": "/app/certs/rds-ca-bundle.pem",
    },
    Section.REGISTRY: {
        "bucket": "team-agent-registry",
        "agents_key": "registry/agents.yaml",
        "tools_key": "registry/mcp-tools.yaml",
    },
    Section.POLICY: {
        "url": "http://localhost:8181",
        "package": "agentcraft2/authz",
        "rule": "decision",
    },
    Section.GUARDRAILS: {"guardrail_id": "abc123def456", "guardrail_version": "1"},
}

LOCAL_DATA: dict[str, object] = {
    "ledger": {"kind": "duckdb_csv", "data_dir": "data", "queries_dir": "queries"},
}
AWS_DATA: dict[str, object] = {
    "ledger": {
        "kind": "redshift_data",
        "database": "curated",
        "cluster_id": "<existing-cluster-id>",
        "db_user": "agent_reader",
        "queries_dir": "queries",
    },
}

_PROVIDER_NOTES: Mapping[Section, str] = {
    Section.SECRETS: (
        "Adapters request a secret by name. Scope the task role to this service's secrets."
    ),
    Section.AUDIT: (
        "Audit records describe governed calls. Configure Firehose delivery to S3 separately; "
        "acceptance by Firehose does not mean the record has reached S3 yet."
    ),
    Section.IDENTITY: (
        "Local identity comes from the configured subject, tenant and roles. AWS verifies "
        "Entra access tokens. The frontend performs sign-in; the agent and MCP verify their "
        "own token audiences. Static identity is local-only."
    ),
    Section.CHECKPOINT: (
        "Use Amazon RDS for PostgreSQL or Aurora PostgreSQL with IAM authentication and TLS. "
        "An IAM-enabled RDS Proxy is an optional connection-pooling layer. MCP services that "
        "keep no graph state select `none` with `{}` options."
    ),
    Section.REGISTRY: (
        "Current AWS adapter: versioned YAML/JSON objects in S3. A PostgreSQL registry "
        "adapter is not implemented. Keeping registry tables in PostgreSQL needs a custom "
        "RegistrySource or a publisher that exports the supported S3 documents."
    ),
    Section.POLICY: (
        "Run OPA on Fargate beside each agent/MCP as a sidecar, or as a separate service. "
        "Rules and OPA make authorization decisions from the authenticated context and "
        "resource metadata. PostgreSQL role/resource mappings need an explicit integration."
    ),
    Section.GUARDRAILS: (
        "The AWS profile uses Bedrock Guardrails for content checks, separately from the "
        "model provider. `patterns` is the in-process alternative; choose it explicitly "
        "only if its checks meet the team's requirements."
    ),
}

_OPTIONS_NOTES: Mapping[Section, str] = {
    Section.SECRETS: (
        "The example resolves `entra_client_secret` to the Secrets Manager secret "
        "`eap/helper/entra_client_secret`. Set a prefix per service. `cache_seconds` "
        "defaults to 300; rotated values are read after expiry."
    ),
    Section.AUDIT: (
        "Replace the stream name with the provisioned stream. The S3 bucket, prefixes, "
        "buffering, retention and delivery alarms belong to infrastructure configuration. "
        "`tracing: true` also enables telemetry; it does not enable raw model tracing."
    ),
    Section.IDENTITY: (
        "Local options also accept `audience` and `token_ttl_seconds`; they do not accept "
        "arbitrary context properties. Match roles to the policy being tested. The AWS "
        "example is an agent calling MCP using Entra on-behalf-of exchange. For an MCP "
        "that only receives calls, omit `exchange` and use that MCP's audience. "
        "`client_secret` names a secret, not its value. `required_scopes` and `claims` "
        "configure scope checks and claim mapping; JWT does not read role tables."
    ),
    Section.CHECKPOINT: (
        "Use the real database or proxy endpoint, not a custom DNS alias. Provision IAM "
        "database access and checkpoint tables before starting the service. Supply the "
        "RDS CA file inside the image for a direct database connection. A separate DB "
        "migration role owns schema changes. Pool sizes are per process. This setting "
        "configures checkpoints only, not registry or business tables."
    ),
    Section.REGISTRY: (
        "Enable S3 bucket versioning and publish both registry documents before startup. "
        "The provider loads a snapshot at startup; restart services to adopt an update. "
        "`expected_bucket_owner` can restrict access to the intended AWS account."
    ),
    Section.POLICY: (
        "Localhost reaches an OPA sidecar in the same Fargate task. For a separate OPA "
        "service, use its HTTPS endpoint and, when needed, `ca_file` and `auth_secret`. "
        "Publish policy and mapping data as OPA bundles, or build a data integration; "
        "setting a PostgreSQL URL here will not load mappings."
    ),
    Section.GUARDRAILS: (
        "Replace the example guardrail ID and published version. `DRAFT` is accepted "
        "only outside production. Additional options choose check points and size limits."
    ),
}


def _json(value: object) -> str:
    return f"`{json.dumps(value, separators=(',', ':'))}`"


def configuration_examples() -> dict[str, EnvironmentExample]:
    """Return one example per configuration key, including every provider and options pair."""
    examples = {
        Key.PROFILE: EnvironmentExample(
            "`local`",
            "`aws`",
            "Selects provider defaults; explicit provider variables override them.",
        ),
        Key.DEPLOYMENT_ENV: EnvironmentExample(
            "`local`",
            "`dev` or `prod`",
            "Both deployed environments reject local-only providers. "
            "Fargate must set this explicitly.",
        ),
        Key.TELEMETRY: EnvironmentExample(
            "`off`; use `opentelemetry` with a collector",
            "`opentelemetry`",
            "Needs the core `otel` extra. Generated services start the exporter automatically. "
            "The Fargate starter sends operational traces to X-Ray and metrics to CloudWatch "
            "through its collector; S3 archival needs additional infrastructure.",
        ),
        Key.MODEL_TRACE_PATH: EnvironmentExample(
            "Optional: `.agentcraft2/model-trace.jsonl`",
            "Unset normally; diagnostic file: `/tmp/model-trace.jsonl`",
            "Captures LangChain model messages and replies, including content. This is a local "
            "JSONL file even in AWS; `s3://` is not supported. To retain it in S3, configure "
            "a file collector on a shared task volume. Task storage is temporary. "
            "Use restricted access and an agreed retention period for diagnostic content.",
        ),
        Key.LIMITS: EnvironmentExample(
            "Unset: built-in limits",
            "Set the team's request limits, or use the defaults",
            "JSON keys: `model`, `tool`, `budget`. Timeouts, retries and call/token budgets "
            'are per invocation, not a shared AWS quota. Example: `{"budget":{"model_calls":20}}`.',
        ),
        Key.TLS_CA_BUNDLE: EnvironmentExample(
            "Team CA file when needed for AWS access",
            "Unset with standard AWS trust; otherwise an image path",
            "Overrides AWS_CA_BUNDLE for Bedrock clients only. Other AWS clients use "
            "AWS_CA_BUNDLE. HTTP adapters have `ca_file`; checkpoints have `ca_bundle`; "
            "pip has its own CA setting.",
        ),
        Key.MODEL_PROVIDER: EnvironmentExample(
            "`fake` for the tutorial; `anthropic` for the hosted example",
            "`bedrock`",
            "The local profile defaults to `anthropic`; the tutorial explicitly selects `fake`. "
            "Bedrock uses the task role. Direct Anthropic is also supported with its extra, "
            "approved network access, and an API key in Secrets Manager.",
        ),
        Key.MODEL_ID: EnvironmentExample(
            "`fake-model`, or the approved Anthropic model ID",
            "The enabled Bedrock model or inference profile ID",
            "Required when the service calls a model. MCP-only services can leave it unset. "
            "Use the ID for the selected provider and AWS region.",
        ),
        Key.MODEL_ALIASES: EnvironmentExample(
            "`{}`",
            'Optional: `{"fast":{"model_id":"<approved-model-id>"}}`',
            "Each alias accepts a model ID string or an object with `model_id` and optional "
            '`provider`. Code can request `services.model("fast")`.',
        ),
        Key.MODEL_OPTIONS: EnvironmentExample(
            '`{"anthropic":{"prompt_caching":true,"cache_ttl":"5m"}}` for Anthropic',
            "`{}` for Bedrock; use the local value only for direct Anthropic",
            "Options are keyed by provider, and apply to all its aliases. Anthropic caching "
            "defaults to off; TTL accepts `5m` or `1h`. Keep static instructions before "
            "request context. This option does not configure Bedrock caching.",
        ),
        Key.DATA_SOURCES: EnvironmentExample(
            _json(LOCAL_DATA),
            _json(AWS_DATA),
            "Set on the MCP that owns the queries. Query existing curated Redshift tables; "
            "the upstream data lake is outside this library's scope. For Serverless, replace "
            "`cluster_id` and `db_user` with `workgroup`. `secret_arn` is an alternative "
            "credential source. Keep named SQL files in the image's `queries_dir`; this is "
            "a directory path, not an S3 URI. The task role needs Data API permissions and "
            "the database user needs table grants. Entra users are authorized by policy; "
            "the adapter connects as the configured database identity.",
        ),
        Key.AWS_PROFILE: EnvironmentExample(
            "Unset for the tutorial; team profile for optional AWS work",
            "Unset: use the ECS task role",
            "A local profile requires your team's approved AWS credential setup. Do not copy "
            "desktop credentials into a container. The task execution role pulls images; "
            "the task role grants application access to AWS services.",
        ),
        Key.AWS_REGION: EnvironmentExample(
            "Unset for the tutorial; target region for AWS work",
            "The deployment region, e.g. `us-east-1`",
            "Use the region of the provisioned services. The Fargate template supplies it.",
        ),
        Key.AWS_CA_BUNDLE: EnvironmentExample(
            "Team CA file if the AWS SDK needs it",
            "Unset with standard AWS trust",
            "AWS SDK CA setting. `AC2_TLS_CA_BUNDLE` takes precedence for Bedrock clients. "
            "Neither setting configures pip or the checkpoint database.",
        ),
        Key.AWS_EXECUTION_ENV: EnvironmentExample(
            "Unset",
            "Set by AWS, e.g. `AWS_ECS_FARGATE`",
            "Runtime marker, not a user-selected provider. Its presence prevents accidental "
            "startup with `AC2_DEPLOYMENT_ENV=local`.",
        ),
        Key.HTTPS_PROXY: EnvironmentExample(
            "Team-supplied proxy, if required",
            "Unset or the approved outbound proxy",
            "AWS and Anthropic use it. JWT and REST require their `use_proxy` option. "
            "Proxy URLs can contain credentials; supply them through approved secret handling.",
        ),
        Key.NO_PROXY: EnvironmentExample(
            "`localhost,127.0.0.1` when using a proxy",
            "Team bypass list including local services and task credential endpoints",
            "For the SDK, include `169.254.170.2` when a proxy would intercept ECS task "
            "credentials. Add internal hosts according to network policy.",
        ),
        secret_key("anthropic_api_key"): EnvironmentExample(
            "API key for the optional Anthropic example",
            "Unset with Bedrock",
            "The env secrets provider accepts this name or `AC2_SECRET_ANTHROPIC_API_KEY`; "
            "set only one. "
            "With Secrets Manager, store the value as `<prefix>anthropic_api_key` instead.",
        ),
    }
    for section in Section:
        local = PROFILE_DEFAULTS[Profile.LOCAL].sections[section]
        aws = PROFILE_DEFAULTS[Profile.AWS].sections[section]
        examples[provider_key(section)] = EnvironmentExample(
            f"`{local}`", f"`{aws}`", _PROVIDER_NOTES[section]
        )
        examples[options_key(section)] = EnvironmentExample(
            _json(LOCAL_OPTIONS[section]), _json(AWS_OPTIONS[section]), _OPTIONS_NOTES[section]
        )
    return examples

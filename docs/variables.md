<!-- Generated from the binding table. Do not edit by hand. -->

# Configuration variables

## Variables owned by the library

| Variable | Purpose | Default | Notes |
| --- | --- | --- | --- |
| `EAP_PROFILE` | Default adapter set: local or aws.<br>Chooses the whole default adapter set in one word. `local` runs with no cloud account: files, SQLite, an in-process rules engine and a development identity. `aws` selects the managed adapters. A profile only supplies defaults; any section can still name another adapter. | `local` |  |
| `EAP_DEPLOYMENT_ENV` | Where the process runs: local, dev or prod. Drives the one-way guard.<br>Says where the process runs. Anything other than `local` turns on the one-way guard: adapters registered as local only (the development identity, the rules engine, JSONL audit, SQLite, DuckDB over CSV, the fake model) refuse to start. Inside ECS, Fargate or Lambda the value must be dev or prod. | `local` |  |
| `EAP_TELEMETRY` | Operational traces and metrics: off or opentelemetry.<br>With `opentelemetry` every governed call is a span named after the GenAI conventions, and the `agentlib.events` and `agentlib.duration` metrics are recorded, through the OpenTelemetry API. The service sends them by calling `configure_telemetry()`; where to is set with the standard `OTEL_` variables. The audit option `tracing: true` also turns it on. | `off` |  |
| `EAP_LIMITS` | JSON timeouts, retries and budgets of governed calls.<br>Keys `model` and `tool`, each with `timeout_seconds`, `retries`, `backoff_seconds` and `max_backoff_seconds`, and `budget` with `model_calls`, `tool_calls` and `tokens` per request. Only transient failures are tried again, and a tool only when it only reads. A request over its budget stops with BudgetExceeded. `agentlib config options limits` lists every key. | model: 120 s, 2 retries; tool: 60 s, 1 retry; budget: 50 model calls and 100 tool calls per request | JSON |
| `EAP_TLS_CA_BUNDLE` | Enterprise CA file for the AWS clients, such as the Bedrock model client.<br>Path to the enterprise CA bundle that the AWS clients trust, for networks that inspect TLS. It applies to the Bedrock model client and the other boto3 clients only; HTTP adapters take their own `ca_file` option. | not set |  |
| `EAP_MODEL_PROVIDER` | Provider of the default model alias.<br>The provider of the model behind the `default` alias, and of any alias that does not name its own provider. | local: `anthropic`, aws: `bedrock` |  |
| `EAP_MODEL_ID` | Model behind the default alias.<br>The model ID behind the `default` alias, as the provider spells it. Until it is set, `services.model()` raises a ConfigurationError that says to set it. A service with no model, such as an MCP server, leaves it unset. | not set: required before `services.model()` |  |
| `EAP_MODEL_ALIASES` | JSON map of further aliases, e.g. {"fast": {"model_id": "..."}}.<br>Further aliases, so code asks for a role (`fast`, `judge`) rather than a model. A value is a model ID or an object with `model_id` and an optional `provider`. | `{}`: only the default alias | JSON |
| `EAP_SECRETS_PROVIDER` | Adapter that serves the secrets section.<br>Where adapters get credentials by name. | local: `env`, aws: `secrets_manager` |  |
| `EAP_SECRETS_OPTIONS` | JSON options for the secrets adapter.<br>Options of the selected secrets adapter, as one JSON object. Unknown keys stop startup. `agentlib config options secrets` lists each adapter's options. | `{}`: the adapter's defaults | JSON |
| `EAP_AUDIT_PROVIDER` | Adapter that serves the audit section.<br>Where the audit trail of every governed call goes. | local: `jsonl`, aws: `firehose` |  |
| `EAP_AUDIT_OPTIONS` | JSON options for the audit adapter.<br>Options of the selected audit adapter, as one JSON object. Unknown keys stop startup. `agentlib config options audit` lists each adapter's options. | `{}`: the adapter's defaults | JSON |
| `EAP_IDENTITY_PROVIDER` | Adapter that serves the identity section.<br>Who the caller is: a development identity locally, OAuth 2.0 JWTs when deployed. | local: `static`, aws: `jwt` |  |
| `EAP_IDENTITY_OPTIONS` | JSON options for the identity adapter.<br>Options of the selected identity adapter, as one JSON object. Unknown keys stop startup. `agentlib config options identity` lists each adapter's options. | `{}`: the adapter's defaults | JSON |
| `EAP_CHECKPOINT_PROVIDER` | Adapter that serves the checkpoint section.<br>Where LangGraph keeps conversation state between turns. | local: `sqlite`, aws: `postgres` |  |
| `EAP_CHECKPOINT_OPTIONS` | JSON options for the checkpoint adapter.<br>Options of the selected checkpoint adapter, as one JSON object. Unknown keys stop startup. `agentlib config options checkpoint` lists each adapter's options. | `{}`: the adapter's defaults | JSON |
| `EAP_REGISTRY_PROVIDER` | Adapter that serves the registry section.<br>The agent and MCP tool registries. | local: `file`, aws: `s3_file` |  |
| `EAP_REGISTRY_OPTIONS` | JSON options for the registry adapter.<br>Options of the selected registry adapter, as one JSON object. Unknown keys stop startup. `agentlib config options registry` lists each adapter's options. | `{}`: the adapter's defaults | JSON |
| `EAP_POLICY_PROVIDER` | Adapter that serves the policy section.<br>Who may do what: the in-process rules engine locally, OPA when deployed. | local: `rules`, aws: `opa` |  |
| `EAP_POLICY_OPTIONS` | JSON options for the policy adapter.<br>Options of the selected policy adapter, as one JSON object. Unknown keys stop startup. `agentlib config options policy` lists each adapter's options. | `{}`: the adapter's defaults | JSON |
| `EAP_GUARDRAILS_PROVIDER` | Adapter that serves the guardrails section.<br>Content checks on prompts, replies and tool results. | local: `patterns`, aws: `bedrock` |  |
| `EAP_GUARDRAILS_OPTIONS` | JSON options for the guardrails adapter.<br>Options of the selected guardrails adapter, as one JSON object. Unknown keys stop startup. `agentlib config options guardrails` lists each adapter's options. | `{}`: the adapter's defaults | JSON |
| `EAP_DATA_SOURCES` | JSON map of named data sources, e.g. {"accounts": {"kind": "duckdb_csv", "data_dir": "data", "queries_dir": "queries"}}.<br>Named data sources for MCP tools. Each entry names its adapter with `kind`; the other keys are that adapter's options. Code asks for `services.data_source("ledger")` and never sees the backend. | `{}`: no data sources | JSON |
| `EAP_SECRET_<NAME>` | A named secret for the env secrets adapter. `EAP_SECRET_RATES_TOKEN` supplies the secret `rates_token`. | not set | sensitive |

## Variables that belong to other tools

These keep their own names. The library reads them and never renames them.

| Variable | Purpose | Default | Notes |
| --- | --- | --- | --- |
| `AWS_PROFILE` | AWS named profile, for example one signed in through SSO.<br>Read by the AWS session for local runs, for example after `aws sso login`. | not set |  |
| `AWS_REGION` | AWS region.<br>Region of every AWS client the library builds. | not set | also read from `AWS_DEFAULT_REGION` |
| `AWS_CA_BUNDLE` | CA file for AWS clients.<br>The AWS SDK's own CA setting, honoured as boto3 does. | not set |  |
| `AWS_EXECUTION_ENV` | Set by AWS inside ECS, Fargate and Lambda. A process that has it must name its deployment environment as dev or prod.<br>Set by AWS on its managed runtimes. Its presence with `EAP_DEPLOYMENT_ENV=local` stops startup, so a container never runs with development adapters by accident. | not set |  |
| `HTTPS_PROXY` | Outbound proxy.<br>Outbound proxy. The AWS clients and the anthropic model use it; the jwt identity provider and rest data sources use it only when their `use_proxy` option is set. Masked wherever it is shown, since it can carry credentials. | not set | sensitive; also read from `https_proxy` |
| `NO_PROXY` | Hosts that bypass the proxy.<br>Comma-separated hosts the AWS clients reach without the proxy. | not set | also read from `no_proxy` |
| `ANTHROPIC_API_KEY` | API key for the anthropic model provider.<br>Supplies the secret `anthropic_api_key`, which the anthropic model provider asks the secrets port for. Masked wherever it is shown. | not set | sensitive |

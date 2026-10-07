<!-- Generated from the binding table. Do not edit by hand. -->

# Configuration variables

## Variables owned by the library

| Variable | Purpose | Notes |
| --- | --- | --- |
| `EAP_PROFILE` | Default adapter set: local or aws. |  |
| `EAP_DEPLOYMENT_ENV` | Where the process runs: local, dev or prod. Drives the one-way guard. |  |
| `EAP_RUNTIME_POLICY` | Named bundle of operational controls. Reserved: read but not used yet. |  |
| `EAP_TLS_CA_BUNDLE` | Enterprise CA file for the AWS clients, such as the Bedrock model client. |  |
| `EAP_MODEL_PROVIDER` | Provider of the default model alias. |  |
| `EAP_MODEL_ID` | Model behind the default alias. |  |
| `EAP_MODEL_ALIASES` | JSON map of further aliases, e.g. {"fast": {"model_id": "..."}}. | JSON |
| `EAP_SECRETS_PROVIDER` | Adapter that serves the secrets section. |  |
| `EAP_SECRETS_OPTIONS` | JSON options for the secrets adapter. | JSON |
| `EAP_AUDIT_PROVIDER` | Adapter that serves the audit section. |  |
| `EAP_AUDIT_OPTIONS` | JSON options for the audit adapter. | JSON |
| `EAP_IDENTITY_PROVIDER` | Adapter that serves the identity section. |  |
| `EAP_IDENTITY_OPTIONS` | JSON options for the identity adapter. | JSON |
| `EAP_CHECKPOINT_PROVIDER` | Adapter that serves the checkpoint section. |  |
| `EAP_CHECKPOINT_OPTIONS` | JSON options for the checkpoint adapter. | JSON |
| `EAP_REGISTRY_PROVIDER` | Adapter that serves the registry section. |  |
| `EAP_REGISTRY_OPTIONS` | JSON options for the registry adapter. | JSON |
| `EAP_POLICY_PROVIDER` | Adapter that serves the policy section. |  |
| `EAP_POLICY_OPTIONS` | JSON options for the policy adapter. | JSON |
| `EAP_GUARDRAILS_PROVIDER` | Adapter that serves the guardrails section. |  |
| `EAP_GUARDRAILS_OPTIONS` | JSON options for the guardrails adapter. | JSON |
| `EAP_DATA_SOURCES` | JSON map of named data sources, e.g. {"accounts": {"kind": "duckdb_csv", "data_dir": "data", "queries_dir": "queries"}}. | JSON |
| `EAP_SECRET_<NAME>` | A named secret for the env secrets adapter. `EAP_SECRET_RATES_TOKEN` supplies the secret `rates_token`. | sensitive |

## Variables that belong to other tools

These keep their own names. The library reads them and never renames them.

| Variable | Purpose | Notes |
| --- | --- | --- |
| `AWS_PROFILE` | AWS named profile, for example one signed in through SSO. |  |
| `AWS_REGION` | AWS region. | also read from `AWS_DEFAULT_REGION` |
| `AWS_CA_BUNDLE` | CA file for AWS clients. |  |
| `AWS_EXECUTION_ENV` | Set by AWS inside ECS, Fargate and Lambda. A process that has it must name its deployment environment as dev or prod. |  |
| `HTTPS_PROXY` | Outbound proxy. | sensitive; also read from `https_proxy` |
| `NO_PROXY` | Hosts that bypass the proxy. | also read from `no_proxy` |
| `ANTHROPIC_API_KEY` | API key for the anthropic model provider. | sensitive |

# ai-agent-lib-aws

AWS adapters for the Enterprise Agentic Platform library. Installing this
package also installs `ai-agent-lib-core`. Core discovers the adapters when
the package is installed; some adapters need an extra listed below. A
service moves from a local adapter to an AWS one by changing configuration,
never code.

From the repository root in **Command Prompt**, use your enterprise-approved
Python and its pip to install the checkout:

```bat
python -m pip install -e packages/ai-agent-lib-aws
```

For the rmprep/Agent Lab stack, `setup-local.bat --model bedrock` installs the
Bedrock dependencies, including `langchain-aws`. Set `EAP_MODEL_PROVIDER=bedrock`
and your model ID in the root `.env` before starting. All services use the same
Python; the launcher isolates UI/Phoenix packages. See [local setup](../../docs/local-startup.md).

For a manual Bedrock installation, use `-e "packages/ai-agent-lib-aws[bedrock]"` instead; for PostgreSQL
use `-e "packages/ai-agent-lib-aws[postgres]"`; for checkpoints in AgentCore Memory
use `-e "packages/ai-agent-lib-aws[agentcore]"`. Combine them as `[bedrock,postgres]`
or `[bedrock,agentcore]` when needed. Pip uses your team's configured package index. The selected AWS
services also need provisioned resources, network access, and credentials.
See the [configuration table](../../docs/developer-guide.html#variables).

## The adapters

Each adapter is selected by name in its section and takes its settings as JSON
in the section's options variable. The variables are listed in
[`docs/variables.md`](../../docs/variables.md).

| Section | Name | What it uses | Options |
| --- | --- | --- | --- |
| model | `bedrock` | Amazon Bedrock, through the Converse API | none; the model ID is the model setting |
| secrets | `secrets_manager` | AWS Secrets Manager | `prefix`, `cache_seconds` |
| audit | `firehose` | Amazon Data Firehose, one acknowledged put per record | `stream` |
| data source | `redshift_data` | Amazon Redshift, through the Data API | `queries_dir`, `database`, and `workgroup` or `cluster_id` (with `db_user` or `secret_arn`), `timeout_seconds` |
| registry | `s3_file` | Two objects in an S3 bucket | `bucket`, `agents_key`, `tools_key`, `expected_bucket_owner` |
| checkpoint | `postgres` | PostgreSQL on Amazon RDS or Aurora, signed in with IAM | `host`, `port`, `database`, `user`, `ca_bundle`, `pool_min_size`, `pool_max_size`, `connect_timeout_seconds` |
| checkpoint | `agentcore_memory` | Amazon Bedrock AgentCore Memory, through `langgraph-checkpoint-aws` | `memory_id`, `max_results`, `checkpoint_format` |
| identity | `agentcore_runtime` | The caller as an AgentCore runtime in IAM mode reports them in its headers; AgentCore Identity for outbound tokens | `tenant`, `roles`, `user_header`, `workload_token_headers`, `exchange` (`credential_provider`, `scopes`, `workload_name`, `return_url`, `cache_seconds`) |
| guardrails | `bedrock` | One version of a Bedrock guardrail | `guardrail_id`, `guardrail_version`, `points`, the four size limits, `frame_tool_results` |

Local and AWS adapters mix freely. A developer who wants a Bedrock model and
nothing else from AWS uses the credentials or profile supplied by their enterprise
setup and selects only that provider. If the approved setup includes AWS CLI and
SSO, renew that profile with `aws sso login --profile dev-sso`. The library itself
uses boto3; setup does not install or require the AWS CLI.

```dotenv
AWS_PROFILE=dev-sso
AWS_REGION=us-east-1
EAP_MODEL_PROVIDER=bedrock
EAP_MODEL_ID=<model or inference-profile ID>
# Only behind a TLS-inspecting proxy, and only Bedrock uses it:
EAP_TLS_CA_BUNDLE=/path/to/enterprise-ca.pem
```

## What every adapter has in common

- **One session.** All adapters of a service share one AWS session, built from
  resolved configuration. No adapter reads the process environment.
- **The service's own role.** On Fargate the task role is the identity for
  every call. There are no stored AWS keys.
- **Errors without content.** An AWS error becomes one of the library's error
  types and keeps only the error code. The service's message, which can
  repeat a statement or a value, stays out of messages and audit records. An
  expired sign-in names the command that renews it.
- **Fail closed.** An audit record that is not acknowledged fails the call it
  describes. A guardrail check without an answer stops the call.

## Permissions each adapter needs

Each adapter declares what it needs, from its own options, in
`ai_agent_lib_aws.access`; `agentlib deploy SERVICE --plan` lists it for a
service's settings, narrowed to the resources they name, and writes it as the
task's policy. A test checks that every SDK call an adapter makes, startup
checks included, is declared. In short:

| Adapter | IAM actions |
| --- | --- |
| `bedrock` model | `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`; `bedrock:GetInferenceProfile` for an inference profile |
| `secrets_manager` | `secretsmanager:GetSecretValue` |
| `firehose` | `firehose:PutRecord`, and `firehose:DescribeDeliveryStream` for the startup check |
| `redshift_data` | `redshift-data:ExecuteStatement`, `DescribeStatement`, `GetStatementResult`, `CancelStatement`; and `redshift-serverless:GetCredentials` for a workgroup, `redshift:GetClusterCredentials` for a cluster with `db_user`, `redshift:GetClusterCredentialsWithIAM` for a cluster with neither, or `secretsmanager:GetSecretValue` on `secret_arn` |
| `s3_file` | `s3:GetObject` on the two objects |
| `postgres` | `rds-db:connect` for the database user |
| `agentcore_memory` | `bedrock-agentcore:CreateEvent`, `ListEvents`, `GetEvent`, `DeleteEvent` on the memory; `ListSessions` too when `checkpoint_format` is `snapshot` |
| `agentcore_runtime` | nothing to verify a caller; with `exchange`, `bedrock-agentcore:GetResourceOauth2Token`, `GetWorkloadAccessToken`, `GetWorkloadAccessTokenForUserId` on the default workload identity directory and the service's workload identity |
| `bedrock` guardrails | `bedrock:ApplyGuardrail` |
| calling an MCP server on an AgentCore runtime (`runtime_arn` in the tool registry, `auth: sigv4`) | `bedrock-agentcore:InvokeAgentRuntime` on the runtime ARN. Granted by hand per registered runtime: the registry is not part of the deploy settings, so the deploy plan cannot derive it. With `auth: bearer` the runtime's JWT authorizer decides, and no IAM action is needed |
| reading the catalogue (`agentlib new mcp --from-redshift`) | `redshift-data:ListTables`, `redshift-data:DescribeTable`, and the same credentials action as `redshift_data`. A developer needs these, not the deployed service |

## Notes on single adapters

**`redshift_data`.** The queries are the same SQL files the local `duckdb_csv`
data source runs over CSV files. One role per MCP server: the server's IAM
role is the database user, granted only the tables its queries read. Row
filters and the row cap run inside the database. Every parameter is sent as a
bound value with an explicit type.

**Reading the Redshift catalogue.** `ai_agent_lib_aws.catalog_redshift` lists
the tables of a schema and describes their columns through the Data API. It
runs no statement and reads no row. `agentlib new mcp --from-redshift` uses it
to propose a server's queries. The queries name tables without a schema,
because the local CSV engine has none: set the search path of the server's
database user to the schema.

**`s3_file`.** The documents are the same YAML or JSON files the local `file`
provider reads. In a bucket with versioning, the object's version ID becomes
the registry revision in every audit record. A missing object stops startup:
an empty registry is a document with no entries. Documents are read once, at
startup.

**`postgres`.** Every new connection signs in with a fresh IAM token over TLS
with the server's certificate checked. Amazon RDS signs its certificates with
its own authorities, so `ca_bundle` must point at the RDS certificate bundle;
RDS Proxy works with the system's authorities. The service never creates
tables. An administrator creates them once, with a database role that may:

```python
await PostgresCheckpointBackend(admin_options, sessions).create_tables()
```

The service's role needs `SELECT` on `checkpoint_migrations` and `SELECT`,
`INSERT`, `UPDATE`, `DELETE` on `checkpoints`, `checkpoint_blobs` and
`checkpoint_writes`. A service whose tables are missing or older than the
library needs does not start.

**`agentcore_memory`.** The native store for a service hosted on Bedrock
AgentCore; PostgreSQL works from there too. AgentCore Memory keys events by an
actor and a session. The library's scoped thread key becomes both: the tenant
and subject become the actor, the application and the thread's name become
the session, each encoded so that two scopes can never meet at one memory key
and cut to the memory's length limits with a digest when they must be. A
thread ID that is not a scoped key is refused. Every service that shares a
memory must use the same `checkpoint_format`. The memory client is the one
`langgraph-checkpoint-aws` ships and builds its own boto3 client: the
service's region, CA file, proxy and timeouts reach it, a named AWS profile
does not, so on a developer's machine it uses the SDK's default credentials.
`validate` reads one empty probe session and writes nothing.

**`agentcore_runtime`.** For an agent or MCP server on an AgentCore runtime
with IAM (SigV4) authentication, where no bearer token reaches the container.
The runtime authenticates the caller and passes them on as an opaque user ID
header; the adapter trusts that perimeter, builds the principal in the
configured `tenant` with the configured `roles`, and marks it
`authenticated_by: agentcore-runtime`, so a policy can tell it from a
verified token. A request without the header, or a bare bearer token, is
refused with `perimeter_identity_missing`. The adapter cannot be selected in
local development, where there is no runtime to trust. With `exchange` set,
the agent obtains tokens for the MCP servers it calls through AgentCore
Identity: the workload access token the runtime passes on is exchanged with
`USER_FEDERATION` for a person, `M2M` for the service itself, and a person who
has not yet authorised the service is refused with `user_consent_required`.
See [identity](../../docs/identity.md#on-aws-bedrock-agentcore).

**Calling MCP servers on AgentCore runtimes.** A tool registry entry with a
`runtime_arn` instead of a `url` is reached through the platform's invocation
endpoint, which the library derives from the ARN; `qualifier` selects the
runtime endpoint (`DEFAULT` unless one was created). `auth: bearer` presents
the token the identity provider's exchange obtains, for a runtime with a JWT
authorizer; `auth: sigv4` signs each request with the service's own AWS
credentials, for a runtime in IAM mode. The connector keeps the
`Mcp-Session-Id` the platform returns and sends it back on later calls to the
same server, so a stateful runtime keeps serving the same microVM. The
platform's JSON-RPC error codes become the library's errors: a refused token
or a denied call is a `PolicyDenied`, throttling and a retryable session
conflict are `TransientError`, an unknown runtime a `ConfigurationError`.
Tools a gateway lists under a prefix are registered without it, with
`tool_prefix` on the server entry. Needs the `agentcore` extra.

**`bedrock` guardrails.** Text a model will read, which is model input and
tool results, is checked as input, where Bedrock looks for attacks on the
prompt. Text a model wrote is checked as output. Any intervention stops the
call, including masking. Each check is a paid call, and model input includes
the whole conversation; `points` limits the checks to the points a service
needs. Production needs a published guardrail version, not `DRAFT`.

## Testing

`ai_agent_lib_aws.testing` has stand-ins for the AWS clients, so every adapter
passes its port's contract suite offline. Request and reply shapes are checked
against the SDK's own service models with `botocore.stub.Stubber`. The
PostgreSQL store also runs against a real PostgreSQL server, and the AgentCore
Memory store against a real memory when `AGENTCORE_MEMORY_ID` names one:

```bash
python -m pytest packages/ai-agent-lib-aws                  # offline
python -m pytest -m integration packages/ai-agent-lib-aws   # real PostgreSQL; Bedrock and AgentCore Memory if configured
```

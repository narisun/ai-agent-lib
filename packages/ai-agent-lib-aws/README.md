# ai-agent-lib-aws

AWS adapters for the Enterprise Agentic Platform library. Installing this
package also installs `ai-agent-lib-core`, and is all it takes to make the
adapters below selectable: core loads them when it finds the package. A
service moves from a local adapter to an AWS one by changing configuration,
never code.

```bash
pip install ai-agent-lib-aws                 # secrets, audit, registry, Redshift
pip install "ai-agent-lib-aws[bedrock]"      # plus the Bedrock model provider
pip install "ai-agent-lib-aws[postgres]"     # plus the PostgreSQL checkpoint store
```

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
| guardrails | `bedrock` | One version of a Bedrock guardrail | `guardrail_id`, `guardrail_version`, `points`, the four size limits, `frame_tool_results` |

Local and AWS adapters mix freely. A developer who wants a Bedrock model and
nothing else from AWS signs in and selects only that provider:

```bash
aws sso login --profile dev-sso
```

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

| Adapter | IAM actions |
| --- | --- |
| `bedrock` model | `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` |
| `secrets_manager` | `secretsmanager:GetSecretValue` |
| `firehose` | `firehose:PutRecord`, `firehose:DescribeDeliveryStream` |
| `redshift_data` | `redshift-data:ExecuteStatement`, `DescribeStatement`, `GetStatementResult`, `CancelStatement`; and `redshift-serverless:GetCredentials` for a workgroup or `redshift:GetClusterCredentials` for a cluster with `db_user` |
| `s3_file` | `s3:GetObject` on the two objects |
| `postgres` | `rds-db:connect` for the database user |
| `bedrock` guardrails | `bedrock:ApplyGuardrail` |
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
PostgreSQL store also runs against a real PostgreSQL server:

```bash
uv run pytest packages/ai-agent-lib-aws                  # offline
uv run pytest -m integration packages/ai-agent-lib-aws   # real PostgreSQL; Bedrock if configured
```

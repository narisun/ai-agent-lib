# Deploying rmprep to AgentCore Runtime

`agentcraft2 deploy SERVICE --target agentcore` writes what runs a service as an
Amazon Bedrock AgentCore Runtime: a `deploy.env` to fill in, a plan that refuses
what the platform cannot run, and a Terraform root module with an ARM64
Dockerfile. This trial takes the `examples/rmprep` workspace through it twice:
the **rmprep** agent (HTTP, JWT from Entra ID, conversations in AgentCore
Memory) and the **rmprep-data** MCP server (MCP, called by the agent with
SigV4-signed requests).

The trial is a recorded dry run. On 23 October 2026, with the library checkout
at commit `df4c98e`, everything up to `terraform apply` ran on a scratch copy of
the example: the starter settings, the plans, the generated files,
`terraform init`, `terraform validate` and `terraform fmt -check`, and the
example's own tests. No AWS account was available, so the account-bound steps
are written as the operator's steps and marked as such: `terraform apply`, the
Entra ID app registration, the ECR push, the live chat and the CloudWatch view.
Nothing in this page claims a result from those steps.

For the deploy command's options and the Fargate target, use the
[developer guide](developer-guide.html). For what AgentCore Runtime changes and
why, read [ADR 0005](adr/0005-agentcore-target.md).

## 1. Prerequisites

- The library's three distributions (`agentcraft2-core`, `agentcraft2-aws`,
  `agentcraft2-cli`) on a package index the image build can reach. The example
  workspace installs the library from the local checkout (`[library] path` in
  `agentcraft2.toml`), and the plan warns that an image build cannot reach it.
- Python 3.11 or newer with the CLI importable; the deployed image uses Python
  3.13, which the services' `requires-python = ">=3.11"` allows.
- Terraform 1.5 or later. The trial used 1.14.7 and the AWS provider 6.70.0,
  which has the `aws_bedrockagentcore_*` resources (the generated module asks
  for `>= 6.20`).
- Docker with `buildx` for the `linux/arm64` image. Not used in the dry run.
- For the operator's steps: an AWS account with permission to create IAM roles,
  ECR repositories, AgentCore runtimes and memories, CloudWatch log groups and
  a Redshift Serverless workgroup; an Entra ID tenant where an application can
  be registered with the Application ID URI `api://rmprep`.

Windows examples use **Command Prompt (cmd.exe)** from the workspace root, with
`PYTHON` set as in [Select Python](../README.md#select-python). The dry run
used a scratch Python with the three packages on `PYTHONPATH`, which is the same
command line.

## 2. Write the deployment settings

With no `deploy.env` in a service's folder, the command writes a starting one
and stops:

```bat
"%PYTHON%" -m agentcraft2_cli deploy rmprep --target agentcore
"%PYTHON%" -m agentcraft2_cli deploy rmprep-data --target agentcore
```

Recorded output:

```text
  created  agents/rmprep/deploy.env

Next:
  add "agentcraft2-aws[bedrock,agentcore]" to the dependencies in agents/rmprep/pyproject.toml, then run 'python -m agentcraft2_cli install'
  fill in agents/rmprep/deploy.env: its values are examples
  run 'agentcraft2 deploy rmprep --target agentcore --plan'

  created  mcp-servers/rmprep-data/deploy.env

Next:
  add "agentcraft2-aws" to the dependencies in mcp-servers/rmprep-data/pyproject.toml, then run 'python -m agentcraft2_cli install'
  fill in mcp-servers/rmprep-data/deploy.env: its values are examples
  run 'agentcraft2 deploy rmprep-data --target agentcore --plan'
```

The starting file already selects the `aws` profile, `dev`, `ambient` telemetry,
a remote OPA and, for the agent, the `agentcore_memory` checkpoint with the
`created_by_terraform` placeholder and `AGENTCORE_MEMORY=create`. The trial
added the dependency lines to both `pyproject.toml` files as the next steps
say. No `install` ran: the plan reads the dependency list, not the installed
packages, so the plan passes without it (the image build installs them).

### The agent: agents/rmprep/deploy.env

The file as it ended up, comments removed. `<placeholder>` marks a value the
operator replaces; the other values are the trial's choices.

```dotenv
AC2_PROFILE=aws
AC2_DEPLOYMENT_ENV=dev
AC2_TELEMETRY=ambient
AC2_POLICY_PROVIDER=opa
AC2_POLICY_OPTIONS={"url": "https://opa.internal:8181"}                    # <placeholder>: an OPA the runtime can reach
AC2_MODEL_ID=eu.anthropic.claude-sonnet-4-5-20250929-v1:0
AC2_CHECKPOINT_PROVIDER=agentcore_memory
AC2_CHECKPOINT_OPTIONS={"memory_id": "created_by_terraform"}               # placeholder by design: Terraform fills it in
AC2_SECRETS_OPTIONS={"prefix": "eap/rmprep/"}
AC2_AUDIT_OPTIONS={"stream": "eap-audit"}                                  # <placeholder>: your Firehose stream
AC2_IDENTITY_OPTIONS={"preset": "entra", "tenant_id": "00000000-0000-0000-0000-000000000000", "audience": "api://rmprep"}   # <placeholder>: tenant GUID
AC2_REGISTRY_OPTIONS={"bucket": "my-eap-registry"}                         # <placeholder>: your registry bucket
AC2_GUARDRAILS_OPTIONS={"guardrail_id": "abc123def456", "guardrail_version": "1"}   # <placeholder>: your guardrail

AGENTCORE_AUTH=jwt
AGENTCORE_DISCOVERY_URL=https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0/.well-known/openid-configuration   # <placeholder>: tenant GUID
AGENTCORE_ALLOWED_AUDIENCE=api://rmprep
AGENTCORE_NETWORK=PUBLIC
AGENTCORE_IDLE_TIMEOUT_SECONDS=900
AGENTCORE_MAX_LIFETIME_SECONDS=28800
AGENTCORE_ARTIFACT=container
AGENTCORE_GATEWAY=none
AGENTCORE_MEMORY=create
```

Two things differ from the plan document's wording of this pilot. The plan says
"no OPA (in-process `rules`)": the `rules` policy adapter is registered
`local_only`, so the plan refuses it in `dev` (see [Troubleshooting](#troubleshooting)),
and the trial keeps the remote OPA the starter proposes. And
`AC2_IDENTITY_PROVIDER` is not written: `jwt` is the `aws` profile's default
identity provider, and the `entra` preset fills in the issuer and key location
from `tenant_id`.

### The MCP server: mcp-servers/rmprep-data/deploy.env

```dotenv
AC2_PROFILE=aws
AC2_DEPLOYMENT_ENV=dev
AC2_TELEMETRY=ambient
AC2_POLICY_PROVIDER=opa
AC2_POLICY_OPTIONS={"url": "https://opa.internal:8181"}                    # <placeholder>
AC2_CHECKPOINT_PROVIDER=none
AC2_DATA_SOURCES={"rmprep": {"kind": "redshift_data", "queries_dir": "queries", "database": "rmprep", "workgroup": "rmprep-dev"}}   # <placeholder>: database and workgroup
AC2_SECRETS_OPTIONS={"prefix": "eap/rmprep-data/"}
AC2_AUDIT_OPTIONS={"stream": "eap-audit"}                                  # <placeholder>
AC2_IDENTITY_PROVIDER=agentcore_runtime
AC2_IDENTITY_OPTIONS={"tenant": "commercial-banking"}
AC2_REGISTRY_OPTIONS={"bucket": "my-eap-registry"}                         # <placeholder>
AC2_GUARDRAILS_OPTIONS={"guardrail_id": "abc123def456", "guardrail_version": "1"}   # <placeholder>

AGENTCORE_AUTH=iam
AGENTCORE_NETWORK=PUBLIC
AGENTCORE_IDLE_TIMEOUT_SECONDS=900
AGENTCORE_MAX_LIFETIME_SECONDS=28800
AGENTCORE_ARTIFACT=container
AGENTCORE_GATEWAY=none
AGENTCORE_MEMORY=none
```

Three settings were changed from the starter, each for a reason the trial met:

- **`AGENTCORE_AUTH=iam`, identity `agentcore_runtime`.** The agent reaches the
  server through a registry entry with `auth: sigv4` (section 3). A runtime in
  JWT mode refuses a signed request, so the server runtime takes IAM, and no
  bearer token reaches the container. The `agentcore_runtime` identity adapter
  trusts that perimeter and assigns every caller the configured tenant. The
  starter's `jwt` identity with the Entra audience `api://rmprep-data` would
  fit the bearer path (`auth: bearer` with a token exchange on the agent),
  which the trial did not take.
- **`AC2_DATA_SOURCES` with `redshift_data`.** The server reads its evidence
  from CSV files through `duckdb_csv` at home. The plan warned that
  `deploy.env` did not configure the data source `rmprep`; copying the local
  value was refused, because `duckdb_csv` is `local_only`. `redshift_data` is
  the AWS data adapter; it runs the server's `queries/` folder, which the
  Dockerfile admits into the image, against a Redshift Serverless workgroup.
  Loading `data/*.csv` into that workgroup is the operator's step (section 7).

## 3. Register the MCP runtime in the tool registry

The agent finds its MCP servers in `registry/mcp-tools.yaml`. At home the
entry has a loopback `url`. On AgentCore the entry names the server's runtime
instead, and the connector derives the invocation URL from the ARN. The trial
replaced the `url` line of the copy's registry, keeping the four pinned tools:

```yaml
schema: agentcraft2.registry/v1
servers:
- id: rmprep-data
  owner: commercial-banking
  runtime_arn: arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/rmprep_data-PLACEHOLDER
  auth: sigv4
  audience: rmprep-data
  tools:
  - name: rmprep-data.find_clients
    version: 0.1.0
    schema_sha256: 3ffc2f464cdc40f33447273bf9f38dd4f76e65a3b5f6631736c174007ee1cb56
    classification: confidential
    read_only: true
    description: Resolve client names or UENs and clarify ambiguous matches.
  # get_memo_evidence, get_client_records and get_reference_data: unchanged
```

`runtime_arn` is the placeholder until the server's `terraform apply` prints
`agent_runtime_arn`; `auth: sigv4` means the agent signs each request with its
execution role's credentials, so that role needs
`bedrock-agentcore:InvokeAgentRuntime` on this ARN. The deploy plan does not
read the registry, so `permissions.tf` cannot derive that grant; it is written
by hand (section 7). The agent's `AC2_REGISTRY_OPTIONS` selects `s3_file`, so
the registry files are uploaded to `s3://<bucket>/registry/agents.yaml` and
`s3://<bucket>/registry/mcp-tools.yaml` after the placeholder is replaced.

## 4. Plan and generate

```bat
"%PYTHON%" -m agentcraft2_cli deploy rmprep --target agentcore --plan
"%PYTHON%" -m agentcraft2_cli deploy rmprep-data --target agentcore --plan
```

### Verified dry run: October 23, 2026

The agent's plan, with the settings of section 2 (the Bedrock, Secrets Manager
and Firehose statements are shown in full in the console; trimmed here):

```text
Deploying rmprep to AgentCore Runtime with the settings in agents/rmprep/deploy.env.

Runtime:
  protocol      HTTP on port 8080
  inbound auth  jwt (tokens from https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0/.well-known/openid-configuration)
  network       PUBLIC
  artifact      container
  session       idle 900s, at most 28800s
  memory        created by Terraform
  gateway       none
  telemetry     ambient (ADOT, installed by the image, owns the providers)

Adapters and what each needs from AWS:
  model bedrock:
    bedrock:InvokeModel, bedrock:InvokeModelWithResponseStream  (call the configured models)
      on arn:aws:bedrock:{region}:{account}:inference-profile/eu.anthropic.claude-sonnet-4-5-20250929-v1:0
      on arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0
    bedrock:GetInferenceProfile  (find the model behind an inference profile)
  secrets secrets_manager:
    secretsmanager:GetSecretValue  (read the service's secrets)
      on arn:aws:secretsmanager:{region}:{account}:secret:eap/rmprep/*
  audit firehose:
    firehose:PutRecord, firehose:DescribeDeliveryStream  (write audit records, and check at startup that the stream is active)
  identity jwt: nothing
  checkpoint agentcore_memory:
    bedrock-agentcore:CreateEvent, bedrock-agentcore:ListEvents, bedrock-agentcore:GetEvent, bedrock-agentcore:DeleteEvent  (store graph thread state in the configured memory, and check at startup that the memory can be read)
      on arn:aws:bedrock-agentcore:{region}:{account}:memory/created_by_terraform
  registry s3_file:
    s3:GetObject  (read the agent and MCP tool registries)
      on arn:aws:s3:::my-eap-registry/registry/agents.yaml
      on arn:aws:s3:::my-eap-registry/registry/mcp-tools.yaml
  policy opa: nothing
  guardrails bedrock:
    bedrock:ApplyGuardrail  (check content with the configured guardrail)
      on arn:aws:bedrock:{region}:{account}:guardrail/abc123def456

Container:
  service: opentelemetry-instrument python -m rmprep.service --profile agentcore

Environment: 13 variables from agents/rmprep/deploy.env.
  AC2_PROFILE ... AC2_GUARDRAILS_OPTIONS

Warnings:
  - AGENTCORE_MEMORY=create: Terraform creates the memory, puts its ID into the checkpoint option memory_id of the runtime's environment and grants the role access to it; memory_id in agents/rmprep/deploy.env ('created_by_terraform') is a placeholder, and the statement in permissions.tf names that placeholder
  - this workspace installs the library from a local checkout, which an image build cannot reach; publish the library to a package index before building the image
```

The server's plan, with the settings of section 2:

```text
Deploying rmprep-data to AgentCore Runtime with the settings in mcp-servers/rmprep-data/deploy.env.

Runtime:
  protocol      MCP on port 8000
  inbound auth  iam (signed requests)
  network       PUBLIC
  artifact      container
  session       idle 900s, at most 28800s
  memory        none
  gateway       none
  telemetry     ambient (ADOT, installed by the image, owns the providers)

Adapters and what each needs from AWS:
  secrets secrets_manager: ...
  audit firehose: ...
  identity agentcore_runtime: nothing
  checkpoint none: nothing
  registry s3_file: ...
  policy opa: nothing
  guardrails bedrock: ...
  data source rmprep (redshift_data):
    redshift-data:ExecuteStatement  (run the queries of data source rmprep)
      on arn:aws:redshift-serverless:{region}:{account}:workgroup/*
      check: a workgroup's ARN holds its ID, not its name; replace * with the ID of workgroup rmprep-dev
    redshift-serverless:GetCredentials  (sign in to the workgroup as the service's role)
      on arn:aws:redshift-serverless:{region}:{account}:workgroup/*
      check: a workgroup's ARN holds its ID, not its name; replace * with the ID of workgroup rmprep-dev
    redshift-data:DescribeStatement, redshift-data:GetStatementResult, redshift-data:CancelStatement  (wait for a query, read its rows, and stop it at its deadline)
      on *
      check: these actions take no resource; AWS lets a caller see only its own statements

Container:
  service: opentelemetry-instrument python -m rmprep_data --profile agentcore

Environment: 13 variables from mcp-servers/rmprep-data/deploy.env.

Warnings:
  - this workspace installs the library from a local checkout, which an image build cannot reach; publish the library to a package index before building the image
```

Both plans ended with exit code 0. The `local checkout` warning is the example
workspace's `[library] path = "../.."`; a workspace that installs the library
from an index does not get it. The two `check:` notes carry into
`permissions.tf` as comments: the workgroup's ARN holds an ID the plan cannot
know, so the operator narrows `workgroup/*` after the workgroup exists.

### The refusals met, and how each was fixed

The settings of section 2 passed at the first attempt because the starter
already chose the platform's values. The refusals below were met while
carrying values over from the example's local `.env.example`, from the Fargate
starter, and while probing the `AGENTCORE_` block. Each is the message as the
command printed it; the fix is what changed in `deploy.env`.

| Setting tried | Refusal (recorded) | Fix |
| --- | --- | --- |
| `AC2_POLICY_PROVIDER=rules` (the plan's "no OPA") | `policy rules runs only on a developer's machine; select another in agents/rmprep/deploy.env` | Keep `opa` with a remote `url`. Open item below. |
| `AC2_POLICY_OPTIONS={"url": "http://localhost:8181"}` (the Fargate value) | `policy opa decides on the loopback address, where its sidecar would run; nothing runs beside a container on AgentCore Runtime. Point url in AC2_POLICY_OPTIONS at an OPA the runtime can reach, or set AC2_POLICY_PROVIDER=rules in agents/rmprep/deploy.env` | `{"url": "https://opa.internal:8181"}`. The message's second option, `rules`, is itself refused in `dev`. |
| `AC2_TELEMETRY=opentelemetry` (the Fargate value) | `AC2_TELEMETRY=opentelemetry installs the service's own tracer and meter providers, which collide with the ones ADOT installs on AgentCore Runtime; set AC2_TELEMETRY=ambient (or off) in agents/rmprep/deploy.env` | `AC2_TELEMETRY=ambient` |
| `AC2_CHECKPOINT_PROVIDER=sqlite` (the local value) | Two problems: `checkpoint sqlite runs only on a developer's machine; select another ...` and `checkpoint sqlite has nowhere durable to write on AgentCore Runtime, whose disk ends with the session; set AC2_CHECKPOINT_PROVIDER to one of postgres, agentcore_memory, none ...`; plus the warning `AGENTCORE_MEMORY=create while checkpoint sqlite does not use AgentCore Memory` | `AC2_CHECKPOINT_PROVIDER=agentcore_memory` with `{"memory_id": "created_by_terraform"}` |
| `AC2_DATA_SOURCES` copied from the server's `.env.example` (`duckdb_csv`) | `data source rmprep (duckdb_csv) runs only on a developer's machine; select another in mcp-servers/rmprep-data/deploy.env` | `redshift_data` with the server's `queries` folder (section 2) |
| `agentcraft2-aws` missing from `agents/rmprep/pyproject.toml` | `audit firehose, checkpoint agentcore_memory, guardrails bedrock, model bedrock, registry s3_file, secrets secrets_manager need agentcraft2-aws, which rmprep does not install; list "agentcraft2-aws[agentcore,bedrock]" in the dependencies in agents/rmprep/pyproject.toml and run 'python -m agentcraft2_cli install'` | Add the dependency line. The plan checks the file, not the installed packages. |
| `AGENTCORE_MEMORY=rmprep_memory-abc123` with the placeholder still in the options | `AGENTCORE_MEMORY names the memory rmprep_memory-abc123, but the checkpoint option memory_id is 'created_by_terraform'; make them agree in agents/rmprep/deploy.env (AGENTCORE_MEMORY and AC2_CHECKPOINT_OPTIONS)` | Either `create` with the placeholder, or the same ID in both places once the memory exists. |
| `AGENTCORE_AUTH=jwt` with neither clients nor audience | `AGENTCORE_ALLOWED_CLIENTS in agents/rmprep/deploy.env is not valid` / `expected: at least one client ID in AGENTCORE_ALLOWED_CLIENTS or one audience in AGENTCORE_ALLOWED_AUDIENCE` / `got: neither is set` | `AGENTCORE_ALLOWED_AUDIENCE=api://rmprep` |
| `AGENTCORE_DISCOVERY_URL=https://login.microsoftonline.com/<tenant>/v2.0` | `AGENTCORE_DISCOVERY_URL in agents/rmprep/deploy.env is not valid` / `expected: an https URL ending in .well-known/openid-configuration` | Append `/.well-known/openid-configuration` |
| `AC2_DEPLOYMENT_ENV=local` | `agents/rmprep/deploy.env describes a developer's machine, not a deployment` / `expected: AC2_DEPLOYMENT_ENV=dev or AC2_DEPLOYMENT_ENV=prod` | `AC2_DEPLOYMENT_ENV=dev` |
| An `AC2_SECRET_API_KEY=...` line | `agents/rmprep/deploy.env holds secrets, and its values become the task's environment` / `fix: remove them from agents/rmprep/deploy.env and store each in Secrets Manager` | Remove the line; the service reads secrets under `AC2_SECRETS_OPTIONS`'s prefix. |

The `AGENTCORE_` refusals and the `local` and secret refusals stop the command
before the plan is printed; the adapter refusals are listed under `Problems`
after the plan, and nothing is written while one remains.

### Generate

```bat
"%PYTHON%" -m agentcraft2_cli deploy rmprep --target agentcore
"%PYTHON%" -m agentcraft2_cli deploy rmprep-data --target agentcore
```

Recorded output, for each service in turn:

```text
  created  deploy/rmprep/Dockerfile.dockerignore
  created  deploy/rmprep/Dockerfile
  created  deploy/rmprep/README.md
  created  deploy/rmprep/main.tf
  created  deploy/rmprep/outputs.tf
  created  deploy/rmprep/terraform.tfvars.example
  created  deploy/rmprep/variables.tf
  created  deploy/rmprep/settings.tf
  created  deploy/rmprep/permissions.tf

Next: read deploy/rmprep/README.md, build and push the ARM64 image, then run terraform in deploy/rmprep.
```

`deploy/rmprep-data/` received the same nine files. No `build_zip.py` is
written for a container artifact. `settings.tf` and `permissions.tf` are
rewritten on every run from `deploy.env`; the other seven are written once and
are yours. In the agent's `settings.tf`, `memory_option_variable =
"AC2_CHECKPOINT_OPTIONS"` tells `main.tf` which variable to rewrite with the
created memory's ID; `request_header_configuration` passes `Authorization`
through because `auth = "jwt"`. The Dockerfile starts
`FROM --platform=linux/arm64 python:3.13-slim`, installs the lock with
`--require-hashes`, then `aws-opentelemetry-distro>=0.18.0`, copies
`agents/rmprep`, `registry` and `policies`, runs as uid 10001, exposes 8080
(8000 for the server) and ends with the plan's command.

The code artifact path exists but was not exercised. Its builder's help:

```text
usage: build_zip.py [-h] --service SERVICE --lock LOCK --entry ENTRY --output OUTPUT
                    [--build BUILD] [--python-version PYTHON_VERSION] [--upload] [--bucket BUCKET] [--key KEY]

Build the code artifact of an AgentCore deployment.
```

## 5. Validate the Terraform

```bat
terraform -chdir=deploy\rmprep init -backend=false
terraform -chdir=deploy\rmprep validate
terraform -chdir=deploy\rmprep fmt -check
terraform -chdir=deploy\rmprep-data init -backend=false
terraform -chdir=deploy\rmprep-data validate
terraform -chdir=deploy\rmprep-data fmt -check
```

### Verified dry run: October 23, 2026

`init -backend=false` fetched `hashicorp/aws v6.70.0` (constraint `>= 6.20.0`)
over the network and wrote `.terraform.lock.hcl` in each folder; commit that
file with the deployment. Both `validate` runs printed
`Success! The configuration is valid.` and both `fmt -check` runs exited 0 with
no file to rewrite. `terraform plan` was not run: it needs credentials and the
`image` variable (`main.tf` has a precondition that refuses a container
deployment without one).

The example's own tests ran against the copy, with the two service `src`
folders on `PYTHONPATH`: `agents/rmprep/tests` 316 passed,
`mcp-servers/rmprep-data/tests` 98 passed. The deployment files do not change
the services' code, so this only shows the copy was intact.

## 6. Build and push

Not run in the dry run: there was no registry to push to, and an ARM64 build
on a Windows desktop takes long. These are the generated README's commands,
from the workspace root, for each service. The lock first, on a Linux host with
Python 3.13 (the runtime's):

```bash
python3.13 -m venv .venv-release
PYTHON="$PWD/.venv-release/bin/python"
"$PYTHON" -m pip install pip-tools
"$PYTHON" -m piptools compile --generate-hashes --output-file=deploy/rmprep/requirements.lock agents/rmprep/pyproject.toml
"$PYTHON" -m piptools compile --generate-hashes --output-file=deploy/rmprep-data/requirements.lock mcp-servers/rmprep-data/pyproject.toml
```

Then the repository, the image and the push. Terraform creates the ECR
repository, and the runtime needs the image to exist when it is created, so the
repository is applied first:

```bat
terraform -chdir=deploy\rmprep-data init
terraform -chdir=deploy\rmprep-data apply -target=aws_ecr_repository.this
aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.eu-west-1.amazonaws.com
docker buildx build --platform linux/arm64 -f deploy\rmprep-data\Dockerfile -t <account>.dkr.ecr.eu-west-1.amazonaws.com/rmprep-data:<tag> --push .
```

Record the pushed digest and set `image` in `deploy\rmprep-data\terraform.tfvars`
(copied from `terraform.tfvars.example`) to
`<account>.dkr.ecr.eu-west-1.amazonaws.com/rmprep-data@sha256:<digest>`. Repeat
for `rmprep`. `Dockerfile.dockerignore` admits only `pyproject.toml`,
`README.md`, `src/**` and `queries/**` of the service, plus `registry/**`,
`policies/**` and the lock; `.env` files, `.agentcraft2/`, databases and keys are
excluded even inside those folders. The server's `data/` folder is not in the
image, which is why its data source is Redshift.

## 7. Apply and verify

The operator's steps. None of them ran in the dry run.

1. **Redshift Serverless.** Create a workgroup (`rmprep-dev` in the settings)
   with a database `rmprep`, and load `data/*.csv` as tables named as the query
   files expect (`accounts`, `activities`, `financial_facts`, `facilities` and
   the rest; the file names in `data/` match). Put the workgroup's ID in place
   of `workgroup/*` in `deploy/rmprep-data/permissions.tf`'s two statements, as
   their `check:` comments say, or accept the wildcard for the pilot. Whether
   the query files run unchanged on Redshift was not checked (open item).
2. **Entra ID.** Register an application with the Application ID URI
   `api://rmprep` and expose a scope, and a client application allowed to
   request it. Put the directory (tenant) ID in both `tenant_id` and
   `AGENTCORE_DISCOVERY_URL` of `agents/rmprep/deploy.env` and run the deploy
   command again, so that `settings.tf` carries it.
3. **The server.** `terraform -chdir=deploy\rmprep-data apply`. Note the
   outputs `agent_runtime_arn`, `role_arn` and `log_group_name`.
4. **The registry.** Replace the placeholder `runtime_arn` in
   `registry/mcp-tools.yaml` with the server's `agent_runtime_arn` and upload
   both registry files:

   ```bat
   aws s3 cp registry\agents.yaml s3://<bucket>/registry/agents.yaml
   aws s3 cp registry\mcp-tools.yaml s3://<bucket>/registry/mcp-tools.yaml
   ```

5. **The agent.** `terraform -chdir=deploy\rmprep apply`. The outputs give
   the runtime's ARN and ID, `role_arn`, `log_group_name` and `memory_id`
   (the memory Terraform created). Then grant the agent's role the call to the
   server, which `permissions.tf` cannot derive. Add a file of your own in
   `deploy\rmprep`, for example `mcp.tf`, and apply again:

   ```hcl
   resource "aws_iam_role_policy" "mcp_servers" {
     name = "mcp-servers"
     role = aws_iam_role.runtime.id
     policy = jsonencode({
       Version = "2012-10-17"
       Statement = [{
         Effect   = "Allow"
         Action   = "bedrock-agentcore:InvokeAgentRuntime"
         Resource = "<the server's agent_runtime_arn>"
       }]
     })
   }
   ```

6. **A token.** For a signed-in user with the Azure CLI:
   `az account get-access-token --resource api://rmprep --query accessToken -o tsv`.
   For a daemon, the client-credentials grant against
   `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` with
   `scope=api://rmprep/.default`. The token's `aud` must be `api://rmprep`:
   the runtime's authorizer checks it against `AGENTCORE_ALLOWED_AUDIENCE`, and
   the service's `jwt` identity checks it again against `audience`.
7. **An invocation.** The runtime's endpoint is the ARN, URL-encoded, under the
   regional `bedrock-agentcore` host; the session header names the
   conversation, which becomes the agent's `thread_id`. The platform requires
   a session ID of at least 33 characters; the service accepts letters,
   digits, `.`, `_`, `:` and `-`.

   ```bat
   set ARN=arn%%3Aaws%%3Abedrock-agentcore%%3Aeu-west-1%%3A<account>%%3Aruntime%%2F<runtime id>
   curl -sS -X POST "https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/%ARN%/invocations?qualifier=DEFAULT" ^
     -H "Authorization: Bearer %TOKEN%" ^
     -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: trial-20261023-000000000000000000000001" ^
     -H "Content-Type: application/json" ^
     -d "{\"input\": {\"question\": \"Find clients containing the exact synthetic test name DISTINCT-FIX-NO-MATCH-20261016. Use find_clients once and report the number of matches.\"}}"
   ```

   The answer is a JSON object with `output`, `thread_id` (the session ID) and
   `request_id`. A second question with the same session header continues the
   conversation from AgentCore Memory. The question above exercises the
   agent-to-server hop through `find_clients` without client records; the
   server's log group shows the call.
8. **Health.** The runtime polls `GET /ping` on the container itself; it is not
   exposed to callers. The service answers `{"status": "Healthy"}`, or
   `{"status": "HealthyBusy"}` while a turn is in progress, and 503 with the
   lifecycle state before it is ready. Read the result in the runtime's log
   group (`/aws/bedrock-agentcore/runtimes/<runtime id>-DEFAULT`).
9. **Traces.** With `AC2_TELEMETRY=ambient` the ADOT distribution the image
   installs owns the providers, and the session ID travels in OpenTelemetry
   baggage as `session.id`. In the CloudWatch console, open **GenAI
   Observability** and pick the agent runtime; sessions are listed by that ID.
   The AWS guide "Add observability to your AgentCore resources" explains the
   one-time enabling of Transaction Search the view depends on.
10. **Agent Lab.** Point the local UI at the runtime with
    `--agentcore-runtime rmprep=<runtime ARN>` on `init`; the desktop's AWS
    credentials sign the calls, or `--agentcore-auth bearer` with a token file
    presents the Entra token. The UI derives the session header from the
    conversation and links each answer to the CloudWatch view. See
    [Chat with an agent on AgentCore Runtime](../packages/agentcraft2-ui/README.md#chat-with-an-agent-on-agentcore-runtime).
    This was not tried in the dry run: it needs the deployed runtime.

## Settings

The `AGENTCORE_` block of `deploy.env` describes the deployment and never
reaches the container; `settings.tf` carries it to Terraform. The plan prints
the resolved values under `Runtime:`.

| Variable | Meaning | Trial value |
| --- | --- | --- |
| `AGENTCORE_AUTH` | Who may invoke the runtime: `iam` (SigV4-signed callers with `bedrock-agentcore:InvokeAgentRuntime`) or `jwt` (bearer tokens from the issuer below) | agent `jwt`, server `iam` |
| `AGENTCORE_DISCOVERY_URL` | The issuer's OpenID discovery document; `https`, ending in `.well-known/openid-configuration`; required with `jwt` | Entra tenant's `v2.0` document |
| `AGENTCORE_ALLOWED_AUDIENCE` | Audiences a token may carry; with `jwt`, this or `AGENTCORE_ALLOWED_CLIENTS` is required | `api://rmprep` |
| `AGENTCORE_ALLOWED_CLIENTS` | Client IDs a token may carry, comma-separated | not set |
| `AGENTCORE_ALLOWED_SCOPES` | Scopes a token must carry | not set |
| `AGENTCORE_NETWORK` | `PUBLIC`, or `VPC` with `AGENTCORE_SUBNETS` and `AGENTCORE_SECURITY_GROUPS` | `PUBLIC` |
| `AGENTCORE_IDLE_TIMEOUT_SECONDS` | A session ends after this long idle (60 to 28800) | `900` |
| `AGENTCORE_MAX_LIFETIME_SECONDS` | A session ends after this long in any case; at least the idle timeout | `28800` |
| `AGENTCORE_ARTIFACT` | `container` (an ARM64 image in the ECR repository Terraform creates) or `code` (a zip from `build_zip.py` in the S3 bucket it creates) | `container` |
| `AGENTCORE_GATEWAY` | `none`, `create` (a gateway in front of the runtime with the workspace's MCP servers as targets) or a gateway ARN | `none` |
| `AGENTCORE_MEMORY` | `none`, `create` (Terraform creates the memory the checkpoints go to) or a memory ID, which must then equal the checkpoint option `memory_id` | agent `create`, server `none` |

The service's own variables are the `AC2_` ones of section 2; run
`"%PYTHON%" -m agentcraft2_cli config explain rmprep` with `deploy.env` as the
settings file to see how each resolves. The generated `deploy/<service>/README.md`
repeats the build and apply steps for that service.

## Open items

- **In-process policy.** The plan document asked for "no OPA (in-process
  `rules`)". The `rules` policy adapter is registered `local_only`, so the plan
  refuses it in `dev` and `prod`. The pilot therefore needs an OPA the runtime
  can reach at `AC2_POLICY_OPTIONS`'s `url`: from a `PUBLIC` runtime, one
  reachable over the internet with TLS; otherwise `AGENTCORE_NETWORK=VPC` with
  the subnets that reach it. Lifting `local_only` from `rules`, with the Rego
  bundle's decisions mirrored, is the way to the plan's intent.
- **The agent's grant to call the server.** The registry entry's `auth: sigv4`
  needs `bedrock-agentcore:InvokeAgentRuntime` on the server's runtime ARN for
  the agent's role. The deploy plan does not read the registry, so the grant is
  a hand-written statement (section 7, step 5), and a new server means another.
- **Inbound identity on the server.** With SigV4 inbound, the Entra token stops
  at the agent; the server knows the caller as the agent's role, through the
  `agentcore_runtime` identity, with one configured tenant. The bearer path
  (`auth: bearer`, the server in `jwt` mode, a token exchange on the agent) was
  not tried.
- **The server's data.** `duckdb_csv` is `local_only`, so the pilot moves the
  evidence to Redshift Serverless through `redshift_data`. The query files are
  written for DuckDB (`STRPOS`, `DATEDIFF(day, ...)`), and whether they run
  unchanged on Redshift was not checked.
- **A named AWS profile.** The Memory checkpoint saver builds its own boto3
  client and takes the region, CA file, proxy and timeouts, but not a named
  profile. On the runtime the role is the identity, so this only matters when
  the saver is tried from a developer's machine.
- **Gateway runtime target.** With `AGENTCORE_GATEWAY=create`, the MCP targets
  come from the `mcp_targets` variable; registering the agent runtime itself
  as a target with an allowed-workload configuration is not expressed by the
  Terraform provider, and the generated README points to the AWS CLI or boto3.
  Not used in this pilot.
- **The library from an index.** The example installs the library from the
  local checkout, and the plan warns on every run; the image build needs the
  three distributions published.
- **Agent Lab.** The `agentcore` binding was not exercised against a runtime;
  the CloudWatch link opens the GenAI Observability view, not one session,
  because AWS publishes no per-session console route.

## Troubleshooting

Every message below was printed during the dry run, except the last two, which
are Terraform's. The plan refuses a setting with the variable to change;
nothing is written while a problem remains.

| Message | What to do |
| --- | --- |
| `policy rules runs only on a developer's machine; select another in <service>/deploy.env` | `rules` is `local_only`. Set `AC2_POLICY_PROVIDER=opa` and point `url` in `AC2_POLICY_OPTIONS` at a remote OPA. |
| `policy opa decides on the loopback address, where its sidecar would run; nothing runs beside a container on AgentCore Runtime. Point url in AC2_POLICY_OPTIONS at an OPA the runtime can reach, or set AC2_POLICY_PROVIDER=rules ...` | Replace the Fargate sidecar's `http://localhost:8181` with the remote OPA's `https` URL. |
| `AC2_TELEMETRY=opentelemetry installs the service's own tracer and meter providers, which collide with the ones ADOT installs on AgentCore Runtime; set AC2_TELEMETRY=ambient (or off) ...` | `AC2_TELEMETRY=ambient`. The Fargate starter's `opentelemetry` is for the collector sidecar, which the runtime does not have. |
| `checkpoint sqlite has nowhere durable to write on AgentCore Runtime, whose disk ends with the session; set AC2_CHECKPOINT_PROVIDER to one of postgres, agentcore_memory, none ...` | `AC2_CHECKPOINT_PROVIDER=agentcore_memory` (with `AGENTCORE_MEMORY=create` and the `memory_id` placeholder), `postgres`, or `none` for an MCP server. |
| `AGENTCORE_MEMORY=create while checkpoint <provider> does not use AgentCore Memory; ...` (warning) | Either select `agentcore_memory`, or set `AGENTCORE_MEMORY=none` so that Terraform creates no memory. |
| `AGENTCORE_MEMORY names the memory <id>, but the checkpoint option memory_id is '<other>'; make them agree ...` | Put the same ID in `AGENTCORE_MEMORY` and in `memory_id` of `AC2_CHECKPOINT_OPTIONS`, or go back to `create` with the placeholder. |
| `data source rmprep (duckdb_csv) runs only on a developer's machine; select another ...` | Select `redshift_data` in `AC2_DATA_SOURCES` with `queries_dir`, `database` and `workgroup`, and load the CSV files into that workgroup. |
| `... need agentcraft2-aws, which <service> does not install; list "agentcraft2-aws[agentcore,bedrock]" in the dependencies in <service>/pyproject.toml ...` | Add the dependency line to the service's `pyproject.toml`. The plan reads the file; no install is needed for it to pass. |
| `AGENTCORE_ALLOWED_CLIENTS in <service>/deploy.env is not valid` / `expected: at least one client ID in AGENTCORE_ALLOWED_CLIENTS or one audience in AGENTCORE_ALLOWED_AUDIENCE` | With `AGENTCORE_AUTH=jwt`, uncomment `AGENTCORE_ALLOWED_AUDIENCE` (or list clients). |
| `AGENTCORE_DISCOVERY_URL in <service>/deploy.env is not valid` / `expected: an https URL ending in .well-known/openid-configuration` | Use the full discovery document URL, not the issuer. |
| `<service>/deploy.env describes a developer's machine, not a deployment` | `AC2_DEPLOYMENT_ENV=dev` or `prod`. `local` is for `.env`. |
| `<service>/deploy.env holds secrets, and its values become the task's environment` | Remove every `AC2_SECRET_*` line; the service reads its secrets from Secrets Manager under `AC2_SECRETS_OPTIONS`'s prefix. |
| `the service reads the data sources rmprep locally, but <service>/deploy.env does not configure them, so the deployed service cannot find them; set AC2_DATA_SOURCES there` (warning) | The server's `.env.example` names a data source the deployment does not. Configure it with an AWS adapter, as above. |
| `this workspace installs the library from a local checkout, which an image build cannot reach; publish the library to a package index before building the image` (warning) | Expected for the example workspace. Publish the distributions, or change `[library]` in `agentcraft2.toml` to the index. |
| `terraform validate` reports a missing provider | Run `terraform -chdir=deploy\<service> init -backend=false` first; it needs network access to fetch `hashicorp/aws`. |
| `The settings deploy a container: set image to the ARM64 image pushed to the repository, pinned by digest.` (at `terraform plan`) | Copy `terraform.tfvars.example` to `terraform.tfvars` and set `image` to the pushed digest (section 6). |

References: [ADR 0005](adr/0005-agentcore-target.md),
[implementation plan, Part B](design/implementation-plan-2026-10-22.md),
the generated `deploy/<service>/README.md`,
[Add observability to your AgentCore resources](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html).

# 0006: Developer settings, closed profiles, AC2_HOME and remote MCP from a local agent

Proposed, 24 October 2026.

## Context

Developers work on restricted enterprise desktops: they cannot install
software, and Python and pip are the preinstalled ones. They reach Bedrock with
the AWS CLI and two-factor sign-in, and some teams still use an Anthropic API
key. They need the whole loop on one machine (agent, MCP servers, Agent Lab,
Phoenix, sample data, a production-quality model) and then the same project on
AWS: Fargate today, Bedrock AgentCore Runtime as an experiment. A coding
assistant should be able to read one place and understand the project.

The configuration surface does not separate who owns what.

- Seven sections each take `AC2_<SECTION>_PROVIDER` and `AC2_<SECTION>_OPTIONS`,
  and any combination is accepted. A developer's own choices (the data folder,
  the policy file, the registries) travel as JSON inside environment variables,
  next to infrastructure options such as `allow_http` and the identity issuer.
  The AgentCore deploy step adds 13 `AGENTCORE_*` variables outside the binding
  table.
- `AC2_DEPLOYMENT_ENV` defaults to `local`. The guard against a deployed
  service running as local recognises ECS and Lambda only, and a blank value in
  the process environment hides the value in `.env`.
- Paths are anchored in four ways: the folder of a service's `.env`, the current
  directory (CLI, Agent Lab, launchers), the folder of `pyproject.toml`, and
  `../../` in generated files. State lands in `.agentcraft2/` relative to
  whichever anchor applied.
- A local agent cannot call an MCP server running on the server side. The local
  identity is the static development one, which sends no token, and a deployed
  server requires a token and a registered calling agent.
- The security and configuration review of 24 October 2026 found that most
  server-side findings come from this freedom to mix: a service that starts as
  local, `agentcore_runtime` identity on ECS, `allow_http` and content capture
  accepted in dev and prod, and third-party secrets accepted in `deploy.env`.

## Decision

1. **Three owners of configuration.**

   | Owner | What it holds | Where it lives |
   | --- | --- | --- |
   | Developer | The model, the policy rules, data sources and their folders, memory and context settings, guardrail rules, limits, the local persona, the registries, tracing switches. | `agentcraft2.settings.toml`, committed, the same in every profile. |
   | Profile | Identity provider, secrets store, audit sink, checkpoint backend, registry location, policy and guardrail engines, TLS rules, bind address, networking, IAM, AgentCore settings. | Code (the profile) and values the deploy step generates. Developers do not edit them. |
   | Other tools and secrets | Credentials and tool settings. | The variables the tools define: `AWS_PROFILE`, `AWS_REGION`, `ANTHROPIC_API_KEY`, `PHOENIX_*`, `OTEL_*`, `HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`. |

   Only variables the library defines use `AC2_`. A secret that has a standard
   name is never also accepted as `AC2_SECRET_<NAME>`; that family is for
   secrets that have no standard name.

2. **The developer file is `agentcraft2.settings.toml`.** It is not
   `agentcraft2.toml`: that file records the answers a workspace was generated
   from, and `init`, `new` and `update` rewrite it whole. The settings file is
   written once and then belongs to the developer; the CLI adds to it and does
   not regenerate it.
   - It holds no secrets and no infrastructure values, and is validated at
     startup against a JSON Schema that `agentcraft2 schema` prints.
   - It is a configuration layer. Precedence is process environment, then
     `.env`, then the settings file, then the profile defaults. Blank values
     never hide a lower layer.
   - Where a knob differs between local and server (a CSV folder locally, a
     Redshift workgroup deployed), the same entry carries both forms. The
     file is never edited per environment.
   - `agentcraft2 config explain --json` lists every effective setting with its
     owner and the layer it came from. Coding tools read that and the schema.
   - Illustration only; the schema is fixed in the implementation change:

     ```toml
     [model]
     provider = "bedrock"            # or "anthropic": a developer's choice
     id = "<model-id>"

     [policy]
     rules = "policies/agentcraft2/rules/data.yaml"

     [limits]
     model = { timeout_seconds = 120 }

     [mcp-servers.accounts.data.accounts]
     kind = "duckdb_csv"
     data_dir = "data"
     queries_dir = "queries"
     ```

3. **Profiles are closed outside local.** The profiles are `local`, `aws`
   (Fargate and other ECS) and `agentcore` (experimental).
   - Each adapter declares the profiles it may run in. This replaces the
     boolean `local_only`. A pairing the adapter does not declare stops startup
     with a message naming the adapter, the profile and the allowed profiles.
     For example, `agentcore_runtime` identity runs only in `agentcore`.
   - Infrastructure options (`allow_http`, issuer and endpoint URLs, bind
     address) cannot come from the settings file or from `AC2_<SECTION>_OPTIONS`
     outside `local`. They arrive from the deploy step. `allow_http` is refused
     outside `local`.
   - `AC2_DEPLOYMENT_ENV` has no default under `aws` or `agentcore`: it must be
     `dev` or `prod`, and `local` is refused. The managed-runtime guard stays as
     a second check and recognises AgentCore, EKS and EC2.
   - Content capture (`AC2_MODEL_TRACE_PATH`, `AC2_PHOENIX_CAPTURE_CONTENT`) is
     refused outside `local`.
   - In `local` a developer may still mix adapters, for example a real Bedrock
     model with in-process everything else. The per-section variables remain as
     advanced overrides there; the settings file does not accept them.
   - The AgentCore deploy variables move to `AC2_AGENTCORE_*`, are read by the
     deploy step only, and an unknown name in the block is an error. The
     `AGENTCORE_*` spellings are accepted for one release with a warning.

4. **The model provider is the developer's choice on every profile.** Bedrock
   uses the AWS credential chain (`AWS_PROFILE`, `AWS_REGION`). The Anthropic
   API uses `ANTHROPIC_API_KEY` locally and Secrets Manager when deployed; the
   deploy plan keeps refusing the key in `deploy.env`. MCP connectors read the
   standard CA and proxy variables, as the AWS and Anthropic clients do, so a
   TLS-inspecting enterprise proxy works the same for every outbound call.

5. **`AC2_HOME` is the root of the project's files.**
   - It is read from the process environment only, because it locates `.env`.
   - When unset it is the nearest folder at or above the current directory that
     holds `agentcraft2.toml`, and otherwise the current directory. Services
     find it from their own location, so starting one from another folder works.
   - `.env`, `agentcraft2.settings.toml`, `registry/`, `policies/`, data folders
     and all state under `.agentcraft2/` (audit, checkpoints, outcomes, traces,
     tokens, Agent Lab, Phoenix, local runs) live under it.
   - A relative path resolves against the file that holds it. Values in the
     settings file, the home `.env` and the process environment resolve against
     `AC2_HOME`. A service-level `.env` keeps resolving against its own folder,
     as today, so existing workspaces still work. The generator stops writing
     path settings into service `.env` files once the settings file holds them.
     Absolute paths always work.
   - Server profiles read no `.env` and write no `.agentcraft2/` state, and the
     deploy step does not pass `AC2_HOME`.

6. **A local agent reaches a server-side MCP server with a token file.**
   - In `local`, a registry entry whose URL is remote (https, not loopback) gets
     its token from `$AC2_HOME/.agentcraft2/tokens/<server-id>.token`. Nothing
     machine-specific is committed. A missing file is an error that names the
     path and says what writes it.
   - The developer's own sign-in tooling writes the file: a short-lived token
     for the development client application of the calling agent, with the
     server's audience. The SDK reads it on every call, sends it only to that
     server and only over https, and never logs it. The folder is created
     owner-only on systems that support it.
   - The server sees the real developer and the registered agent, so nothing
     changes on the server: the same checks as in production run. The local
     agent's own policy still uses the local persona; the server applies the
     developer's real roles.
   - Plain http is allowed only to loopback, even in `local`. Remote servers
     need schema pins even in `local`, so a changed tool description cannot
     reach the model unnoticed.
   - Production servers must refuse tokens of the development client by issuer
     or audience. That is infrastructure and is documented, not code.
   - A built-in `agentcraft2 login` that writes the file with the device-code
     flow is a possible later change and is not decided here.

## Consequences

By construction a server profile can no longer start as local, accept the
perimeter identity off AgentCore, accept `allow_http` or content capture, or
take third-party secrets as plain deploy variables. The IAM wildcards, the
policy and registry gaps, redaction and request limits from the review are
separate work.

A developer sets nothing but `AWS_PROFILE`/`AWS_REGION` or an API key, and
optionally `AC2_HOME`. Moving a project is copying a folder. The generated
`.env.example` files shrink to secrets and third-party names.

Costs: one more file to learn; an adapter-by-profile test matrix replaces the
`local_only` flag; `docs/variables.md` and the developer guide are generated
from the new tables; precedence between layers must stay visible through
`config explain`. Existing deployments keep working for one release: the
per-section variables, `EAP_*` and `AGENTCORE_*` spellings are accepted with a
warning, except where this record refuses a combination as unsafe.

Windows ignores the owner-only file modes, so a home on a shared drive can
expose `.env`, tokens and state to other users; the CLI warns where it can
tell. Remote MCP from a local agent depends on the identity provider issuing
the development client's tokens, which is not verified for any provider here.

Not decided: the exact schema of the settings file; `agentcraft2 login`; whether
`rules` may run in `aws` (open since ADR 0005); AgentCore settings beyond the
move to `AC2_AGENTCORE_*`, which stay experimental.

Suggested order: the fixes that do not depend on this record (blank-value
layering, pinned ADOT install, MCP proxy and CA handling), then profiles and
the explicit deployment environment, then `AC2_HOME`, then the settings file and
its schema, then the remote MCP token file.

## Owner

Library maintainers.

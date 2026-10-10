# ai-agent-lib: Specification

Oct 7, 2026 · @Sundar

Release note: this is the target design. See [release 0.1 scope and traceability](release-scope.md)
for implemented behavior, deferred features, and decisions resolving differences from this document.

## 1. Purpose and scope

`ai-agent-lib` gives every LangGraph agent and MCP server the same governed plumbing, so teams write only graph and tool logic.

**Goals**

- **Native LangGraph authoring.** No custom graph DSL. The library hands back ordinary LangGraph and LangChain objects that happen to be governed.
- **One code path from laptop to Fargate.** Environment variables select a provider for each port, and local and server-side providers can be mixed freely during development.
- **Testable by injection.** Every dependency is injected, so unit tests need no network, cloud account or wall-clock time.
- **AWS first, portable later.** Other clouds and model vendors arrive as separate packages that pass the same contract tests.
- **Fail closed.** A policy, audit or guardrail failure stops the request; it never degrades to a fallback.

**Non-goals**

- Not an agent framework or hosting platform. The application owns topology, prompts and domain schemas.
- No authentication. The host authenticates; the library carries the verified identity.
- No universal lineage or freshness enforcement (concern 30). The library offers a source-metadata type only.
- No infrastructure provisioning inside the Python packages. Terraform modules ship beside them as reference artifacts.
- No code loading from configuration. Environment values name registered providers; they never name import paths.

## 2. Design principles

Eleven rules apply to every module; a change that breaks one needs a recorded decision.

1. **Ports and adapters.** Each capability is a `typing.Protocol` in `contracts`. Adapters implement it. `contracts` imports no adapter, no LangGraph and no cloud SDK.
2. **Depend on interfaces.** A component names the ports it needs in its constructor. It never imports another component's implementation.
3. **Constructor injection only.** No global singletons, no service-locator calls inside nodes, no import-time side effects.
4. **Change stays local.** Each thing that may change has one owning module, listed in section 4 and enforced by a test.
5. **Configuration names live in one place.** Only `config` knows a variable name. Every other module receives a typed options object.
6. **Configuration selects names, never code.** A trusted registry maps a provider name to a factory.
7. **Fixed interceptor order.** Options switch behaviour on or off. They cannot reorder budget, policy, guardrail and audit steps.
8. **Typed failures.** A policy denial is never retried and never becomes a fallback.
9. **Scope is a type.** Stores accept a `Scope` value, never a raw key string.
10. **Async first.** The library is written async, with a thin sync facade for `invoke` callers.
11. **Determinism by injection.** Time, identifiers and file paths come from an injected `Clock`, `IdGenerator` and options.

## 3. Architecture and distributions

One monorepo publishes three Python distributions; only the first is needed to run an agent locally.

&#91;embedded content: library layers · five levels, dependencies point at contracts\]

`ConfigResolver` reads the environment once, and `ServiceContainer` binds one adapter to each port. Graph code never sees which adapter sits behind a port.

| Distribution | Import package | Contents | Key dependencies |
| --- | --- | --- | --- |
| `ai-agent-lib-core` | `ai_agent_lib_core` | Contracts, config, container, pipelines, local adapters, LangGraph and MCP bindings, testing kit | `langgraph`, `langchain-core`, `pydantic`, `opentelemetry-api` |
| `ai-agent-lib-aws` | `ai_agent_lib_aws` | Bedrock, Redshift, Secrets Manager, shared state, audit and guardrail adapters; the AWS session factory | `ai-agent-lib-core`, `boto3`, `langchain-aws` |
| `ai-agent-lib-cli` | `ai_agent_lib_cli` | `agentlib new`, `doctor`, `run`, `graph`, `update`; the templates | `ai-agent-lib-core`, `copier` |

The repository keeps the name `ai-agent-lib`, and no distribution carries that name. `ai-agent-lib-core` offers the extras `[anthropic]`, `[duckdb]`, `[mcp]` and `[opa]`. There is no `[aws]` extra: installing `ai-agent-lib-aws` pulls in core, which avoids a circular reference.

**Repository layout**

```text
ai-agent-lib/                 # repository and uv workspace root; not a distribution
  packages/
    ai-agent-lib-core/src/ai_agent_lib_core/
      contracts/       # ports, value types, options models, errors
      config/          # ConfigSource, binding table, resolver, profiles
      di/              # ServiceProviders, ServiceContainer
      pipeline/        # Interceptor contract and the interceptors
      adapters/        # JSONL, SQLite stores, DuckDB, file registry, in-process policy, model providers
      integrations/
        langgraph/     # the only module that imports langgraph; holds the scoped checkpointer
        mcp/           # the only module that imports the MCP SDK
      testing/         # fakes and contract test suites
      evaluation/      # datasets and scorers; off the request path
      release/         # descriptors, hashes, release registry, admission
    ai-agent-lib-aws/src/ai_agent_lib_aws/
    ai-agent-lib-cli/src/ai_agent_lib_cli/
  templates/           # agent, mcp
  policies/            # Rego starter bundle and tests
  registry/            # sample agents.yaml and mcp-tools.yaml
  deploy/              # base image, Terraform modules
  examples/            # accounts-mcp, accounts-agent
  docs/adr/            # recorded decisions
```

**Import rules, enforced in CI**

- `contracts` imports nothing else from the library.
- `config`, `pipeline` and `adapters` import `contracts` only.
- `config` is the only module that reads the environment.
- `integrations.langgraph` is the only module in core that imports `langgraph`. `ai_agent_lib_aws` keeps its LangGraph code, the Postgres checkpointer, in one matching module.
- `ai_agent_lib_core` never imports `boto3` or `ai_agent_lib_aws`.
- Request-path modules (`pipeline`, `integrations`) never import `evaluation`, `release` or `ai_agent_lib_cli`.

## 4. Change containment

Every likely change has one owning module and a test that fails when the change leaks. This is how interfaces and injection limit the impact of a change.

| What may change | The only place that changes | Enforced by |
| --- | --- | --- |
| Variable names or the prefix | The binding table in `config` | Architecture test: no environment access and no `EAP_` literal outside `config` |
| Where configuration comes from | A `ConfigSource` adapter | `ConfigSource` contract suite |
| LangGraph API | `integrations.langgraph`, in each package that has one | Import rule; version matrix |
| MCP SDK | `integrations.mcp` | Import rule |
| AWS SDK | `ai_agent_lib_aws` | Import rule: core never imports `boto3` |
| Model vendor or model ID | A provider adapter and the alias configuration | Capability-descriptor contract suite |
| Data backend or SQL dialect | A `DataSource` adapter and the named queries | `DataSource` contract suite; query validation in CI |
| Policy engine | A `PolicyDecisionPoint` adapter | Contract suite; versioned decision schema |
| Storage backend | Store adapters | Store contract suites, including leakage |
| Registry source: file, S3 object or database | `AgentRegistry` and `ToolRegistry` adapters | Registry contract suite |
| Audit record format | Versioned schema in `contracts` | Schema tests |
| Interceptor order | Chain assembly in `pipeline` | Golden order test |

**What a component may know**

- Its own options object and the ports named in its constructor.
- Nothing about variable names, other adapters or the active profile.

**Names held outside the code**

Task definitions, Terraform and `.env` files also hold variable names, and code containment cannot reach them. The binding table therefore supports deprecated aliases: an old name keeps working, with a warning, for one minor release.

## 5. Configuration and provider resolution

All configuration enters through one resolver in `config`, which turns `EAP_`-prefixed variables into typed, frozen options. No other module knows a variable name.

**How a value reaches a component**

1. A `ConfigSource` supplies raw name and value pairs. Sources exist for the process environment, a `.env` file, and an in-memory mapping for tests.
2. The binding table maps each variable name to a section and a field. It is the only place a variable name is written.
3. `ConfigResolver` applies the profile defaults, validates every value and returns an immutable `ServiceConfig`.
4. Each component receives only its own section, such as `AuditOptions`, through its constructor.

**Naming**

- **Pattern.** Library variables are `EAP_<SECTION>_<SETTING>`. EAP stands for Enterprise Agentic Platform.
- **Sections are concerns.** A section names a concern such as `MODEL`, `AUDIT` or `POLICY`, not a Python path, so refactoring code never renames a variable.
- **Selector and options.** Most sections have a selector, `EAP_<SECTION>_PROVIDER`, and a JSON value, `EAP_<SECTION>_OPTIONS`, validated against a frozen model that rejects unknown keys.
- **Unknown names fail.** An `EAP_` variable that is not in the binding table stops startup, which catches typing mistakes.
- **Third-party names are kept.** `AWS_PROFILE`, `AWS_REGION`, `AWS_CA_BUNDLE`, `HTTPS_PROXY`, `NO_PROXY`, `ANTHROPIC_API_KEY` and `OTEL_*` keep their own names. The binding table lists the ones the library reads.
- **Aliases.** A binding can carry deprecated names, which keep working with a warning for one minor release.
- **Generated outputs.** The variable reference below, `.env.example` and the `agentlib doctor` output are generated from the binding table.

**Provider resolution**

Each section resolves its provider independently.

1. The section's selector, for example `EAP_MODEL_PROVIDER=bedrock`.
2. The `EAP_PROFILE` default, `local` or `aws`. Unset means `local`.
3. Nothing else. The library never guesses from the surrounding environment.

**Mixing local and server-side providers**

A developer who wants Bedrock models with everything else local changes one section:

```bash
EAP_PROFILE=local                  # SQLite, JSONL, DuckDB, file registry, in-process policy
EAP_MODEL_PROVIDER=bedrock         # override one section
EAP_MODEL_ID=<Bedrock model or inference-profile ID>
AWS_PROFILE=dev-sso                # after: aws sso login --profile dev-sso
AWS_REGION=us-east-1
AWS_CA_BUNDLE=/etc/ssl/enterprise-ca.pem
```

The same pattern points a local agent at a real policy server or at dev Redshift:

```bash
EAP_POLICY_PROVIDER=opa
EAP_POLICY_OPTIONS={"url":"http://localhost:8181","package":"agentlib/authz"}
EAP_DATA_SOURCES={"accounts":{"kind":"redshift_data","workgroup":"dev","database":"analytics"}}
```

**Rules**

- **Profile versus runtime policy.** A profile chooses adapters and any section can override it. `EAP_RUNTIME_POLICY` names a trusted bundle of control values and rejects overlapping individual settings (concern 27).
- **One-way guard.** Server-side providers may be used anywhere. Adapters flagged `local_only` fail startup unless `EAP_DEPLOYMENT_ENV=local`. Examples are the SQLite stores, DuckDB over CSV, the in-process policy rules, the fake model and the static dev principal. The default environment is local, so a process that AWS marks as running on ECS, Fargate or Lambda refuses to start until it names its environment as dev or prod. A service that keeps no graph state, such as an MCP server, selects the none checkpoint provider and needs no store.
- **Missing package.** Selecting a provider whose package is absent fails at startup with the install command.
- **Visibility.** `agentlib doctor` prints each section, its provider, whether the choice came from a variable or the profile, and a reachability check. Secrets are masked.

**Variable reference**

Platform-wide variables:

| Variable | Purpose |
| --- | --- |
| `EAP_PROFILE` | Default adapter set: `local` or `aws` |
| `EAP_DEPLOYMENT_ENV` | `local`, `dev` or `prod`; drives the one-way guard |
| `EAP_RUNTIME_POLICY` | Named bundle of operational controls |
| `EAP_TLS_CA_BUNDLE` | Enterprise CA file for the AWS clients, which is how Bedrock is reached |

Variables by section:

| Section | Serves | Variables |
| --- | --- | --- |
| `MODEL` | `ChatModelProvider` | `EAP_MODEL_PROVIDER`, `EAP_MODEL_ID`, `EAP_MODEL_ALIASES` |
| `SECRETS` | `SecretsProvider` | `EAP_SECRETS_PROVIDER`, `EAP_SECRETS_OPTIONS` |
| `AUDIT` | `AuditSink`, `Telemetry` | `EAP_AUDIT_PROVIDER`, `EAP_AUDIT_OPTIONS` |
| `GUARDRAILS` | `GuardrailCheck` | `EAP_GUARDRAILS_PROVIDER`, `EAP_GUARDRAILS_OPTIONS` |
| `POLICY` | `PolicyDecisionPoint` | `EAP_POLICY_PROVIDER`, `EAP_POLICY_OPTIONS` |
| `IDENTITY` | `IdentityVerifier` | `EAP_IDENTITY_PROVIDER`, `EAP_IDENTITY_OPTIONS` |
| `DATA` | `DataSource` | `EAP_DATA_SOURCES` |
| `REGISTRY` | `AgentRegistry`, `ToolRegistry` | `EAP_REGISTRY_PROVIDER`, `EAP_REGISTRY_OPTIONS` |
| `CHECKPOINT`, `MEMORY`, `CACHE`, `BUDGET`, `APPROVAL` | The state stores | A selector and an options value for each section |
| `CONTEXT` | Context bounds, `TokenCounter` | `EAP_CONTEXT_PROVIDER`, `EAP_CONTEXT_OPTIONS` |
| `RESILIENCE` | Model and tool resilience | `EAP_RESILIENCE_MODEL`, `EAP_RESILIENCE_TOOL` |
| `JUDGE` | `ToolJudge` | `EAP_JUDGE_PROVIDER`, `EAP_JUDGE_TOOL_CRITERIA` |
| `SKILLS` | `SkillActivationService` | `EAP_SKILLS_PROVIDER`, `EAP_SKILLS_OPTIONS` |
| `RELEASE` | `ReleaseRegistry` | `EAP_RELEASE_PROVIDER`, `EAP_RELEASE_OPTIONS` |

Named secrets use one more family of variables. `EAP_SECRET_<NAME>` supplies the secret `<name>` to the `env` secrets adapter in local development; for example `EAP_SECRET_RATES_TOKEN` supplies `rates_token`.

Names from the original concern table map as follows:

| Concern table | Variable |
| --- | --- |
| `AUDIT_OPTIONS` | `EAP_AUDIT_OPTIONS` |
| `GUARDRAILS_OPTIONS` | `EAP_GUARDRAILS_OPTIONS` |
| `TOOL_JUDGE_CRITERIA` | `EAP_JUDGE_TOOL_CRITERIA` |
| `CONTEXT` | `EAP_CONTEXT_OPTIONS` |
| `RESILIENCE` | `EAP_RESILIENCE_MODEL` |
| `TOOL_RESILIENCE` | `EAP_RESILIENCE_TOOL` |
| `RUNTIME_POLICY` | `EAP_RUNTIME_POLICY` |

## 6. Dependency injection and lifecycle

Four small classes do all the wiring, with no DI framework: a config resolver, a frozen config, a provider registry and a container that owns lifecycle.

| Class | Responsibility |
| --- | --- |
| `ConfigResolver` | Reads a `ConfigSource` through the binding table and returns a `ServiceConfig`. Lives in `config`. |
| `ServiceConfig` | Immutable snapshot of typed option sections and resolved provider names. Holds no variable names. Secrets are `SecretStr` and never appear in `repr`, logs or audit. |
| `ServiceProviders` | Registry from (port, name) to factory. Frozen once the container starts. |
| `ServiceContainer` | Composition root and async context manager. Builds each service once, hands it only its own options section, validates at startup, closes in reverse order. |

**Registration**

- `ServiceProviders.default()` registers the local adapters.
- `.with_installed(allow=("ai_agent_lib_aws",))` adds provider packs found through entry points, limited to the distributions named in code. An installed package that is not on the allowlist is ignored.
- This lets a variable switch a section to Bedrock with no code change, while keeping the registry auditable.

**Usage rules**

- A factory receives its options section and the ports it declares. It never receives the environment or the whole config.
- Nodes and tools receive dependencies through factories or constructors. They never reach into the container at call time.
- The container is never placed in graph state or in runtime context.
- `await services.validate()` checks every selected provider before the first request.

```python
async with ServiceContainer.from_env() as services:
    await services.validate()
    model = services.model("default")
    tools = services.tools([lookup_account])
    graph = build_graph(model, tools).compile(**services.compile_kwargs())
```

`from_env()` is shorthand for a `ConfigResolver` over the process environment and a `.env` file.

A unit test builds the config from option objects and swaps the registry, so it never mentions a variable name:

```python
services = ServiceContainer(
    ServiceConfig.for_testing(),
    providers=fake_providers(model=FakeChatModel(["ok"]), clock=FrozenClock()),
)
```

## 7. Ports and adapters catalogue

The ports below cover the runtime concerns. Each has a local adapter in `ai-agent-lib-core` and a server-side adapter in `ai-agent-lib-aws` or supplied by the host. The Section column is the configuration section from section 5.

| Port | Section | Responsibility | Local adapter | Server-side adapter | Concerns |
| --- | --- | --- | --- | --- | --- |
| `ChatModelProvider` | `MODEL` | Build governed chat models by alias | `anthropic`, `fake` | `bedrock` | 23 |
| `SecretsProvider` | `SECRETS` | Resolve named credentials | Environment snapshot | Secrets Manager, Parameter Store | 24 |
| `AuditSink` | `AUDIT` | Durable, fail-closed evidence | fsync JSONL | Firehose to S3 Object Lock | 1, 21 |
| `Telemetry` | `AUDIT` | Metadata-only spans and metrics | OpenTelemetry API, no exporter | Same API; ADOT collector exports | 1 |
| `PolicyDecisionPoint` | `POLICY` | Allow or deny, with obligations | `rules`, `opa` | `opa` sidecar | 29 |
| `GuardrailCheck` | `GUARDRAILS` | Inspect inputs and results | PII, injection, size, tool checks | Adds Bedrock Guardrails | 2 |
| `IdentityVerifier` | `IDENTITY` | Verified `Principal` from claims | Static dev principal | jwt: OAuth 2.0 access tokens checked against the issuer's published keys, with a Microsoft Entra ID preset. In core, not the AWS package | 14 |
| `DataSource` | `DATA` | Named, parameterized reads | `duckdb_csv`, `rest` | `redshift_data`, `rest` | A |
| `AgentRegistry` | `REGISTRY` | Look up registered agents | `file`: YAML or JSON | `s3_file` first; enterprise registry database later | M |
| `ToolRegistry` | `REGISTRY` | Look up MCP servers and tools, with schema pins | `file`: YAML or JSON | `s3_file` first; enterprise registry database later | 5, 20, M |
| `Checkpointer` | `CHECKPOINT` | LangGraph thread state | SQLite | Postgres on Aurora | 4, 22 |
| `MemoryStore` | `MEMORY` | Scoped long-term memory | SQLite | Postgres on Aurora | 4 |
| `Cache` | `CACHE` | Scoped TTL reads | SQLite | ElastiCache or DynamoDB | 12 |
| `BudgetLedger`, `PauseControl` | `BUDGET` | Call and token limits; live pause | SQLite, marker file | DynamoDB, AppConfig | 16 |
| `ApprovalLedger` | `APPROVAL` | Verified approval bindings | SQLite | Postgres on Aurora | 17 |
| `ToolJudge` | `JUDGE` | Assess untrusted results | Governed model with criteria | Host adapter | 6 |
| `TokenCounter` | `CONTEXT` | Token bounds for context | Character estimate | Provider counter | 11 |
| `SkillActivationService` | `SKILLS` | Disclose authorized instructions | Pinned instruction-only packages | Host adapter | 26 |
| `ReleaseRegistry` | `RELEASE` | Activate and roll back releases | POSIX directory | Versioned S3 | 19 |

`ConfigSource`, `Clock` and `IdGenerator` are ports as well. They have no configuration section because the composition root supplies them directly, and tests supply fakes.

## 8. Request pipelines and error taxonomy

Every model call and every tool call passes through one interceptor chain whose order is fixed by the library. Audit and telemetry wrap the whole chain, so denials and failures are always recorded.

```python
class Interceptor(Protocol[RequestT, ResponseT]):
    async def __call__(
        self, request: RequestT, call_next: Handler[RequestT, ResponseT]
    ) -> ResponseT: ...
```

**Model call order**

1. Execution pause check and budget reservation.
2. Identity present and scope resolved.
3. Policy decision on the model route for the data classification in play.
4. Context bounding: message, character and token limits.
5. Input guardrails.
6. Resilience: retry transient errors, then the configured fallback model.
7. Provider call.
8. Output guardrails, before anything is released to a stream.
9. Structured output validation, when requested.
10. Budget settlement with actual token usage.

**Tool call order**

1. Execution pause check and budget reservation.
2. Registry check: the tool is registered, its schema pin matches and its arguments validate.
3. Policy decision: entitlement plus obligations.
4. Human approval when required, verified against its binding hash.
5. Input guardrails.
6. Idempotency key attached, for any tool that writes.
7. Resilience: deadline always; retry and circuit breaker for read-only tools only.
8. Tool or data-source call, with obligations applied in the data layer.
9. Result size and guardrail checks.
10. Untrusted-result framing.
11. Optional judge, with a fail-closed verdict.

The same tool chain runs in two places: around LangChain tools inside an agent, and around tool handlers inside an MCP server.

**Error taxonomy**

All exceptions derive from `AgentLibError`.

| Exception | Meaning | Retried | Falls back |
| --- | --- | --- | --- |
| `ConfigurationError` | Invalid or missing configuration, raised at startup | No | No |
| `CredentialsExpiredError` | SSO session or token expired; message carries the fix command | No | No |
| `PolicyDenied` | Policy, entitlement or guardrail refusal | Never | Never |
| `BudgetExceeded`, `ExecutionPaused` | Limit reached or execution pause set | No | No |
| `TransientError` | Throttling, timeout or server error | Models and read-only tools | Models only |
| `IntegrityError` | Audit sink failure or hash mismatch | No | No |
| `ValidationFailed` | Structured output or schema violation | Bounded re-ask, opt-in | No |

## 9. Identity, scope and tenant isolation

Authority comes only from a verified, immutable `Principal` passed at the boundary; nothing in a prompt or in graph state can grant a permission.

| Type | Fields | Notes |
| --- | --- | --- |
| `Principal` | Subject, tenant, roles, authenticating party, delegation chain | Frozen. Built only by an `IdentityVerifier`. |
| `RequestContext` | Principal, application, request ID, thread ID, classification ceiling, deadline | Frozen. Created once per request. |
| `Scope` | Tenant, subject, application | Derived from `RequestContext`. The only key type stores accept. |

- **Where it travels.** `RequestContext` rides in LangGraph runtime context through `context_schema`. It is never written to graph state, because state is checkpointed and visible to the model.
- **One key builder.** A single `ScopedKey` function produces every storage key. The checkpointer wrapper namespaces thread IDs by scope, and store namespaces carry the scope as a prefix.
- **Agent to MCP.** The enterprise identity provider is Microsoft Entra ID, reached through OAuth 2.0 standards so that a change of provider is a change of options. The agent verifies the signed-in user's token, then exchanges it for one bound to the MCP server: the on-behalf-of flow with Entra, or RFC 8693 with another provider. It never forwards the user's original token. The new token names the user, the app roles they hold at that server and, as the application it was issued to, the agent's own non-human identity. The MCP server verifies it, requires that agent to be registered and listed for the server, passes the whole context to policy and rebuilds the `Principal`.
- **Local development.** A static dev principal stands in for the identity provider. It also issues short-lived signed development tokens, so a local agent and a local MCP server agree on who the caller is. It is flagged `local_only`.
- **Erasure.** `erase(scope)` removes a scope from memory, cache, checkpoints and ledgers in one call. Audit records follow retention rules instead.
- **Proof.** A cross-tenant leakage contract test runs against every store adapter.

## 10. Models

Graph code asks for a logical alias such as `default`, `fast` or `judge`; configuration maps each alias to a provider and a model ID, so no vendor ID appears in application code.

| Provider | Built on | Credentials | Available in |
| --- | --- | --- | --- |
| `anthropic` | `langchain-anthropic` | API key through the secrets port | `ai-agent-lib-core`, `[anthropic]` extra |
| `bedrock` | `langchain-aws`, Converse API | Standard AWS credential chain | `ai-agent-lib-aws` |
| `fake` | Scripted responses | None | `ai-agent-lib-core`, `local_only` |

Each provider publishes a capability descriptor: prompt caching, structured output, token counting, context window, and which errors count as throttling. The pipelines read the descriptor and never branch on vendor name.

**Bedrock from a laptop through SSO and an enterprise CA**

1. Sign in: `aws sso login --profile dev-sso`.
2. In `.env`, set `EAP_MODEL_PROVIDER=bedrock` and `EAP_MODEL_ID`, plus the standard `AWS_PROFILE`, `AWS_REGION` and `AWS_CA_BUNDLE`.
3. Run `agentlib doctor`. It confirms the caller identity, the CA file and Bedrock reachability.

No API key is involved, and the same code uses the task role on Fargate with no change.

**How it is built**

- **One session factory.** `AwsSessionFactory` receives the resolved profile, region and CA file as options and builds the `boto3` session and every client. With none set, the standard credential chain applies, which on Fargate is the task role.
- **Injected client.** The Bedrock client is injected into the chat model, so tests inject a stub.
- **TLS trust.** `EAP_TLS_CA_BUNDLE` is needed only to reach Bedrock, so it is passed explicitly to the AWS clients. The Anthropic API provider does not use it. AWS clients fall back to `AWS_CA_BUNDLE`. There is no setting that disables certificate verification.
- **Expired sign-in.** An expired SSO session raises `CredentialsExpiredError` naming the login command. It is not transient, so it never triggers a retry loop.
- **Proxy.** `HTTPS_PROXY` and `NO_PROXY` are honoured.
- **Model IDs.** Bedrock IDs differ from Anthropic API IDs and may be inference-profile IDs. Cross-region profiles affect data residency, so the `model.route` policy action can restrict regions.
- **Throttling.** Provider throttling maps to `TransientError` and follows the `EAP_RESILIENCE_MODEL` settings.

## 11. MCP and data access

MCP tools read through a `DataSource` port using named, parameterized queries; the model supplies parameter values and never SQL text.

| Kind | Backing | Where used | Notes |
| --- | --- | --- | --- |
| `duckdb_csv` | DuckDB over a folder; each CSV is a table | Local | Zero setup; `local_only` |
| `rest` | `httpx` client | Local and AWS | Timeouts always; retry on GET only; record and replay fixtures |
| `redshift_data` | Redshift Data API | AWS, or local with SSO | IAM authentication, no connection pool; adds polling latency and a result-size cap |
| `redshift_driver` | Native driver, optional | AWS | Lower latency; needs a network path and pooling |

**Named queries**

- Each query lives in `queries/<name>.sql` with declared parameter types and a row cap.
- Redshift SQL is the source of truth. `sqlglot` transpiles it to DuckDB for local runs.
- Redshift has no trustworthy local emulator, so CI validates every named query against a dev workgroup.

**Limits the library enforces**

- Read-only access, statement timeout, row cap, pagination and a result-byte ceiling.
- Each statement carries the application and request ID for cost attribution.
- Write tools are out of scope for the first release. A later write tool needs an idempotency key and an approval.

**Policy obligations**

- A policy decision can return a row filter, masked columns and a lower row cap. The data layer applies them before results leave.
- Redshift row-level security and dynamic masking act as a second layer where they are available.

**MCP bindings**

- **Server side.** `integrations.mcp` wraps each tool handler in the tool pipeline. Servers run stateless over HTTP: on localhost in development and behind the internal load balancer on AWS. Tests use an in-memory transport.
- **Agent side.** `services.mcp_tools("accounts")` looks the server up in the `ToolRegistry`, connects, and returns ordinary LangChain tools. They pass through the same pipeline, including untrusted-result framing.

## 12. Agent and tool registries

Two read-only registries say which agents and which MCP tools exist. Locally they are plain YAML or JSON files; later an enterprise registry database serves them through the same two ports.

| Registry | Port | Answers | Used by |
| --- | --- | --- | --- |
| Agents | `AgentRegistry` | Which agents exist, who owns them, where they run and which MCP servers they may call | `GovernedRouter` allowlists, discovery by other agents, `agentlib doctor` |
| MCP tools | `ToolRegistry` | Which MCP servers and tools exist, with schema pin, classification and read-only flag | `services.mcp_tools()`, step 2 of the tool pipeline, policy input |

**Local files**

`registry/mcp-tools.yaml`:

```yaml
schema: agentlib.registry/v1
servers:
  - id: accounts
    owner: treasury-data
    url: http://localhost:8001/mcp
    audience: accounts-mcp
    tools:
      - name: accounts.lookup
        version: 1.2.0
        schema_sha256: <hash of the tool's input schema>
        classification: restricted
        read_only: true
```

`registry/agents.yaml`:

```yaml
schema: agentlib.registry/v1
agents:
  - id: accounts-agent
    owner: treasury-data
    version: 0.3.0
    url: http://localhost:8000
    description: Answers account balance questions
    mcp_servers: [accounts]
    model_aliases: [default]
    classification_ceiling: restricted
    status: active
```

The `file` provider is the default. `EAP_REGISTRY_OPTIONS` can point it at other paths.

**Rules**

- **File format.** The `file` provider reads one YAML or JSON file per registry, chosen by extension. YAML is parsed with the safe loader only.
- **Validated snapshot.** Each file is validated against a frozen schema at startup and held as an immutable snapshot. A duplicate ID or an unknown field fails startup.
- **Data only.** An entry never holds a command, an import path or a credential.
- **Unregistered means unusable.** An agent or tool that is absent from the registry cannot be called.
- **Narrowing only.** Being registered grants nothing by itself. Policy still decides every call.
- **Schema pins.** When an agent connects to a server, each tool's live input schema is hashed and compared with its pin. A mismatch raises `IntegrityError`.
- **Evidence.** The audit record stores the registry revision in force for each call.

**From files to the enterprise registry**

- **Same ports.** Every provider implements `AgentRegistry` and `ToolRegistry` and passes the same contract suite. No calling code changes.
- **Until the database exists.** `ai-agent-lib-aws` adds an `s3_file` provider that reads the same YAML or JSON from a versioned S3 object. Each environment gets its own registry without rebuilding the image, and the object version is the registry revision.
- **Database adapter.** The enterprise adapter serves a snapshot with a revision and refreshes it on an interval. A failed refresh keeps the last good snapshot for a bounded time, then fails closed.

**Relation to other concerns**

- `ToolRegistry` is the catalog that concern 20 pins against.
- `ReleaseRegistry` (concern 19) activates exact release artifacts and is a separate port.

## 13. Policy decisions with OPA

One `PolicyDecisionPoint` port answers every authorization question, and anything other than a well-formed allow is a deny.

**Decision input, versioned**

```json
{
  "schema": "agentlib.decision/v1",
  "principal": {"subject": "u-123", "tenant": "t-9", "roles": ["analyst"]},
  "action": "data.query",
  "resource": {"kind": "query", "name": "accounts.by_region", "classification": "restricted"},
  "context": {"application": "accounts-mcp", "environment": "prod", "model_provider": "bedrock"}
}
```

| Element | Values |
| --- | --- |
| Actions | `tool.call`, `data.query`, `model.route`, `agent.call`, `skill.activate`, `memory.read`, `memory.write` |
| Decision | `allow`, reason code, obligations, bundle revision, decision ID |
| Obligations | `row_filter`, `mask_columns`, `max_rows`, `require_approval` |

**Behaviour**

- **Fail closed.** A timeout, a transport error, a malformed reply or a missing `allow` field is a deny. An obligation the library does not recognise is also a deny.
- **Narrowing only.** A tool must be registered, be listed for the calling agent and be allowed by policy. Policy can restrict access; it cannot add it.
- **Evidence.** The audit record stores the decision ID and bundle revision for every decision.
- **Caching.** A short-lived decision cache keyed by an input hash exists and is off by default.

**Providers**

| Name | What it is | Where |
| --- | --- | --- |
| `rules` | In-process evaluator of the rules document | First run only; `local_only` |
| `opa` | HTTP call to an OPA server at a configured URL | Local through compose; Fargate as a sidecar |

What is allowed is written once, in a rules document with the schema `agentlib.rules/v1`. A service keeps it at `policies/agentlib/rules/data.yaml`, so its `policies/` folder is a valid OPA bundle. The `rules` provider reads that file in process. OPA loads the same file as data, next to the platform's Rego bundle in `policies/bundle`, which is a small fixed evaluator checked by `opa test`. Both engines pass one contract test suite, so they cannot drift apart. The first rule that covers a request allows it and supplies its obligations; a request that no rule covers is denied.

## 14. LangGraph integration

A developer writes a normal `StateGraph`; the library appears at nine touchpoints and each one returns a native object.

```python
from langgraph.graph import StateGraph, START, END
from ai_agent_lib_core import RequestContext, ServiceContainer


async def main(request_context: RequestContext, inputs: dict) -> dict:
    async with ServiceContainer.from_env() as services:
        model = services.model("default")  # a governed BaseChatModel
        tools = services.tools([summarise])  # governed local tools
        tools += await services.mcp_tools("accounts")  # registered MCP tools

        builder = StateGraph(State, context_schema=RequestContext)
        builder.add_node("agent", make_agent_node(model, tools))
        builder.add_edge(START, "agent")
        builder.add_edge("agent", END)

        graph = builder.compile(**services.compile_kwargs())
        return await graph.ainvoke(inputs, **services.invocation(request_context))
```

| Touchpoint | Library call | What comes back |
| --- | --- | --- |
| Model | `services.model(alias)` | Chat model wrapped in the model pipeline |
| Tools | `services.tools([...])` | Tools wrapped in the tool pipeline |
| MCP tools | `services.mcp_tools(server)` | Registered MCP tools as LangChain tools, wrapped in the tool pipeline |
| Compile | `services.compile_kwargs()` | Scoped checkpointer and store |
| Invoke | `services.invocation(ctx)` | Config and runtime context for the call |
| Routing | `GovernedRouter` | A function for conditional edges, with an allowlist and a deterministic fallback |
| Approval | `require_approval(...)` | A helper around the native `interrupt()` that records the binding |
| Streaming | `services.stream(graph, ...)` | The native update stream, with model output checked before release |
| Output | `StructuredOutput(schema)` | Strict JSON validated against Draft 2020-12 |

**Compatibility**

- The supported LangGraph version range is pinned and tested as a CI matrix.
- In `ai-agent-lib-core`, only `integrations.langgraph` imports LangGraph, so an upstream change is absorbed in one module. `ai-agent-lib-aws` follows the same rule.
- The library uses public extension points only. It does not subclass internals or patch at runtime.

## 15. Scaffolding CLI

`agentlib new` asks a short series of questions and writes a project that runs and passes its tests before the developer edits a line.

| Command | Purpose |
| --- | --- |
| `agentlib new agent` | Generate an agent project |
| `agentlib new mcp` | Generate an MCP server over CSV, REST or a database |
| `agentlib doctor` | Show resolved providers, masked config and reachability checks |
| `agentlib run` | Start the agent or server locally with the test client |
| `agentlib graph` | Render the compiled graph |
| `agentlib update` | Pull template fixes into an existing project |

**Questions for an MCP server**

1. Name and owning team.
2. Source kind: local CSV, remote REST, or database.
3. Source details, followed by introspection:
   - CSV: the folder. DuckDB reads column names and types.
   - REST: an OpenAPI file or URL. The developer picks operations.
   - Database: a Redshift workgroup and schema, or a DDL file. The developer picks tables.
4. Which tools to expose, and the data classification of each.
5. Which roles may call each tool.

The wizard then adds the server and its tools, with schema pins, to `registry/mcp-tools.yaml`.

**Questions for an agent**

1. Name and owning team.
2. Graph pattern: single tool-calling agent, router with subgraphs, or an empty graph.
3. Model alias and provider for local runs.
4. Which registered MCP servers it calls, picked from the tool registry.
5. Memory, human approval and structured output: yes or no for each.

The wizard then adds the agent to `registry/agents.yaml`.

**What every generated project contains**

- `src/` with the graph or tool handlers, and `tests/` that pass using fakes.
- `.env.example`, generated from the binding table, and sample data.
- `queries/` with starter named queries, and `policies/` with Rego rules and tests.
- `registry/` with the project's own registry entries.
- A Dockerfile, a compose file that starts OPA, and a deploy stub that calls the Terraform module.
- A CI workflow and a README.

**Rules for the generator**

- **Answers file.** Answers are saved to `agentlib.answers.yaml`. `--answers` replays them without prompts, for CI and for regeneration.
- **Thin output.** Generated code holds business logic and configuration only. Cross-cutting behaviour stays in the library, so an upgrade needs no regeneration.
- **No template rot.** CI generates every template variant and runs its tests on each change.
- **Dev tool only.** No runtime module depends on the CLI.

## 16. AWS Fargate deployment

Each agent and each MCP server runs as its own ECS service on Fargate, in a task of three containers: the application, an OPA sidecar and a telemetry collector.

| Element | Choice |
| --- | --- |
| Compute | One ECS service per agent or MCP server; tasks scale horizontally |
| Policy | OPA sidecar on `localhost:8181`; signed bundle pulled from S3 |
| Telemetry | ADOT collector sidecar; JSON logs to stdout |
| AWS access | Task role for Bedrock, Redshift Data API, Secrets Manager and S3; no static keys |
| Secrets | Injected into the task from Secrets Manager |
| State | Aurora Postgres for checkpoints, memory and approvals; DynamoDB or ElastiCache for budgets and cache |
| Registries | A versioned S3 object per environment at first; the enterprise registry database when it is available |
| Audit | Firehose to S3 with Object Lock |
| Network | Internal load balancer; agents reach MCP servers over stateless HTTP |
| Image | Shared base image, non-root, hash-locked install of the admitted wheel |

**What the library contributes**

- **No local state.** Fargate storage is ephemeral, so the one-way guard rejects `local_only` adapters at startup.
- **Health.** Readiness and liveness helpers report container validation and dependency reachability.
- **Graceful stop.** On SIGTERM the container stops accepting work, lets in-flight runs checkpoint, and closes within the task's stop timeout.
- **Twelve-factor configuration.** Everything comes from environment variables, so the task definition is the only deployment-specific file.

**Other targets**

- **Virtual machines.** The same image runs under systemd, or the same wheel runs with an env file.
- **Other clouds.** A new package implements the ports and must pass the shared contract tests. A new deployment module sits beside the AWS one under `deploy/`.

## 17. Testing and quality strategy

Every port ships with a contract test suite, and the same suite runs against the fake, the local adapter and each server-side adapter.

| Layer | What runs | Needs |
| --- | --- | --- |
| Unit | Interceptors, config resolution, key building, error mapping | Nothing external; fakes and a frozen clock |
| Architecture | Import rules; no environment access or `EAP_` literal outside `config`; golden interceptor order | Nothing external |
| Contract | One suite per port, including cross-tenant leakage | Fake and local adapters on every commit |
| Container | Contract suites against Postgres and OPA running in containers | A container runtime in CI |
| Integration | Contract suites against Bedrock, Redshift, Secrets Manager and Firehose | A dev AWS account; runs nightly and before release |
| Template | Generate every variant, then run its tests | Nothing external |
| Compatibility | Full suite across the supported LangGraph and Python versions | Nothing external |
| Policy | `opa test` on the Rego bundle | OPA binary |

**Rules**

- **Fakes over mocks.** `ai_agent_lib_core.testing` ships a fake model, clock, audit sink, policy point, registries and stores. Application teams use the same fakes.
- **No network in unit tests.** CI blocks sockets for the unit layer.
- **Stubbed cloud clients.** AWS adapters are unit-tested with stubbed `boto3` clients, so most of `ai-agent-lib-aws` is tested without an account.
- **Property tests** cover scoped-key building and config resolution.
- **Static checks.** `ruff`, strict `mypy` and the architecture tests gate every merge.
- **Hosts can verify adapters.** The contract suites are importable, so a platform team can prove its own adapter before registering it.
- **Public API is explicit.** Each package declares `__all__`. Anything else is private and may change in a minor release.
- **Versioning.** Semantic versions per distribution, with a deprecation window of one minor release for public API and for variable names.

## 18. Concern coverage map

All 31 original concerns and 13 added ones map to a named mechanism and a delivery phase. Phase numbers refer to the Delivery plan tab.

| # | Concern | Mechanism | Phase |
| --- | --- | --- | --- |
| 1 | Observability | `AuditSink` and `Telemetry` wrapping both pipelines | 2 |
| 2 | Guardrails | `GuardrailCheck` interceptors | 3, Bedrock Guardrails in 4 |
| 3 | Serialization and integrity | `release`: descriptors and hashes | 7 |
| 4 | Memory | `MemoryStore` and scoped checkpointer | Checkpointer in 2 and 4, memory in 6 |
| 5 | MCP | `integrations.mcp`, `ToolRegistry` and the tool pipeline | 3 |
| 6 | Runtime judges | `ToolJudge` interceptor | 6 |
| 7 | Structured output | `StructuredOutput` validator | 3 |
| 8 | Teams and routing | `GovernedRouter` | 6 |
| 9 | Dependency injection | `ConfigResolver`, `ServiceConfig`, `ServiceProviders`, `ServiceContainer` | 2 |
| 10 | Offline and online evaluation | `evaluation`, off the request path | 7 |
| 11 | Context | Context-bounding interceptor, `TokenCounter` | 6 |
| 12 | Caching | `Cache` port | 6 |
| 13 | Scoring | `evaluation` | 7 |
| 14 | Identity | `Principal`, `RequestContext`, `IdentityVerifier` | 2, JWT verifier and token exchange built after 3 |
| 15 | Resilience | Resilience interceptor | 6 |
| 16 | Budgets and execution pause (kill switch in the original table) | `BudgetLedger`, `PauseControl` | 6 |
| 17 | Human approval | `ApprovalLedger` and the interrupt helper | 6 |
| 18 | Streaming | Governed update stream | 6 |
| 19 | Registry of releases | `ReleaseRegistry` | 7 |
| 20 | Tool catalog | `ToolRegistry` schema pins, checked in the tool pipeline | 3, wheel binding in 7 |
| 21 | Compliance audit | Fail-closed `AuditSink` | 2, Firehose in 4 |
| 22 | Tenant isolation | `Scope` and the scoped-key builder | 2 |
| 23 | Model binding | `ChatModelProvider` and aliases | 2, Bedrock in 4 |
| 24 | Secrets | `SecretsProvider` | 2, Secrets Manager in 4 |
| 25 | Developer tooling | `ai-agent-lib-cli` | 5 |
| 26 | Agent Skills | `SkillActivationService` port | 6 |
| 27 | Policy bundles | `RuntimePolicy` | 6 |
| 28 | Provenance and admission | `release`: admission checks | 7 |
| 29 | Institution-wide policy | `PolicyDecisionPoint` with OPA | 3 |
| 30 | Evidence and citations | Source-metadata type only; enforcement is a non-goal | 3 |
| 31 | Dependency and release assurance | Hash-locked CI | 1, completed in 7 |
| A | Governed data access | `DataSource` port and named queries | 3, Redshift in 4 |
| B | Data egress by model | `model.route` policy action | 6 |
| C | Delegated identity | Token exchange between agent and MCP server | 4 |
| D | Idempotency keys | Step 6 of the tool pipeline | 6 |
| E | Rate limits and loop guards | Limiter interceptor and recursion cap | 6 |
| F | State schema versioning | Version stamp on checkpoints | 6 |
| G | Lifecycle and health | Container lifecycle and health helpers | 4 |
| H | Erasure and retention | `erase(scope)` across stores | 6 |
| I | Cost attribution | Usage metrics by tenant, application and model | 6 |
| J | Mixed-provider configuration | Per-section resolution and the one-way guard | 2 |
| K | SSO credentials and TLS trust | `AwsSessionFactory`, `EAP_TLS_CA_BUNDLE` | 4 |
| L | Change containment | Binding table, owning-module map and architecture tests | 2 |
| M | Agent and tool registries | `AgentRegistry`, `ToolRegistry` | `file` in 3, `s3_file` in 4, database in 6 |

## 19. Open decisions

Fourteen decisions need an owner before phase 2 starts; each has a recommended default that the rest of this spec assumes.

Already decided: the distribution names, the import packages, the `contracts` layer and the `EAP_` prefix with concern-based sections.

| Decision | Recommended default | Why it matters |
| --- | --- | --- |
| Minimum Python version | 3.11 | Sets typing and concurrency features available to the library |
| How agents are served | Thin ASGI app from the template | Decides health, shutdown and streaming surfaces |
| Redshift access | Data API by default, driver optional | Trades latency against pooling and network setup |
| Store for checkpoints and memory | Postgres on Aurora | Uses the maintained LangGraph Postgres checkpointer |
| OPA topology | Sidecar per task | Latency and failure isolation versus one central service |
| Agent-to-MCP authentication | Decided: Microsoft Entra ID, on-behalf-of exchange, agents authenticate with a client secret, roles are app roles | Built and tested against a stand-in provider; a run against the real tenant is still needed |
| Infrastructure tooling | Terraform | Shapes the reference modules under `deploy/` |
| Template engine | Copier | Gives an update path for existing projects |
| Write tools in the first release | No | Removes idempotency and approval from the critical path |
| Package index and CI system | Not yet known | Phase 1 cannot finish without them |
| Enterprise registry database | Not yet known: technology, API and owner | Decides when the database adapter can be built; files cover the gap |
| Shape of option values | One JSON value per section | Flat variables are easier in task definitions; the binding table keeps a later switch local |
| Coverage gate | 90% branch coverage on `contracts`, `config`, `di` and `pipeline` | Sets the bar the CI pipeline enforces from phase 2 |
| CLI command name | `agentlib` | Could become `eap` to match the prefix; a later rename touches scripts and guides |

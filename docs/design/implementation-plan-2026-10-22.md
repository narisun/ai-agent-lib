# Implementation plan: remediation and Amazon Bedrock AgentCore deployment

This plan follows the [22 October code review](code-review-2026-10-22.md). It has
two parts that share one roadmap:

- **Part A** fixes the review's findings in the library, the CLI, Agent Lab and
  the repository tooling. The examples (`examples/rmprep`, `examples/accounts-*`)
  are out of scope for fixes; rmprep is used only as the pilot workload in Part B.
- **Part B** reviews the library against what Amazon Bedrock AgentCore requires of
  a hosted agent or MCP server, and plans the changes that make an agent-lib
  service deployable there with one command.

Nothing in this document has been implemented. Line numbers refer to the working
tree on 22 October 2026. AgentCore facts come from the AWS documentation read on
the same date and are cited at the end; the service changes often, so each
workstream re-checks its contract page before it starts.

## Guiding decisions

1. **Keep the architecture.** Ports stay framework-free, adapters stay thin, the
   container stays the only composition root. Every change below is either a
   split along an existing seam, a new adapter behind an existing port, or a new
   deployment target behind the existing CLI command.
2. **AgentCore is a deployment target, not a framework.** The library already
   speaks HTTP and MCP; AgentCore fixes the port, the path, the health check, the
   session header, the authentication mode and the telemetry pipeline. Those are
   configuration and thin route aliases, not a rewrite.
3. **Fix the seams Part B needs first.** Several AgentCore changes land in the
   same files the review flagged as god modules (`integrations/http/agent.py`,
   `di/authentication.py`, `observability/otel.py`, `cli/deploy.py`). Splitting
   them first makes the AgentCore work smaller and safer.
4. **One rule set, one gate.** The UI and the generated workspaces are linted,
   type-checked and tested with the library's rules in CI before new features are
   added to either.

## Part A: remediation workstreams

Each workstream lists what changes, which review findings it closes, the tests
that prove it, and its size (S: under a day, M: one to three days, L: a week).
Public names and signatures stay compatible unless stated.

**Status, 22 October 2026:** A1 and A2 (Phase 0) and A3, A4 and A6 (Phase 1)
are implemented. Deviations from the text below, with the reason:

- Diagnostics warnings carry the error type and request id but no traceback,
  because the logging policy forbids exception text in logs.
- The UI import-linter contracts live in the UI's own `pyproject.toml` and CI
  job rather than the root linter, because the UI is not installed in the root
  environment.
- The container split produced `di/selection.py` (what the configuration
  selects, option checks) and `di/graph.py` (`AdapterGraph`: build order,
  lifetimes, close) rather than the `lifecycle`/`graph` pair named below;
  `di/lifetime.py` already existed. `di/container.py` is 654 lines of facade
  and docstrings, above the 500-line target; its responsibilities are now only
  accessors and framework entry points.
- The HTTP split added `http/prelude.py` (the shared request prelude) beside
  the four modules named, because the stream route needs the prelude without
  depending on the JSON route.
- The data pipeline's resilience stage reuses the tool limits: a named query
  only reads, so it is retried like a read-only tool. `Limits` gained no field.
- The MCP connector's timeout is the configured tool-call timeout
  (`EAP_LIMITS`, `tool.timeout_seconds`); no new option was added. Testing
  showed the SDK folds 401/403 into a generic MCP error, so the connector now
  raises for those statuses itself before the taxonomy mapping runs.
- The tracing integration gained `AgentTracing.observe(...)` taking a registry
  and an invocation callable; `run(graph, services, ...)` remains as the
  compatibility path the examples use. Sink construction stayed in the
  integration layer (`local_model_trace`) rather than `di/`.
- Four UI contracts with no implementation were deleted; the consumer check
  script now imports `ConversationStore` instead.
- Still over 500 lines and left for later: `adapters/data_rest.py` (648),
  `integrations/langgraph/model.py` (590), `cli/scaffold.py` (591), and the
  shipped contract suites in `testing/contracts.py` (812).

### A1. Green gates and CI coverage (S)

Closes: review findings 1 and 3.

- Fix `tests/architecture/phoenix_sql_checks.py`: declare the three class
  attributes, add a `[[tool.mypy.overrides]]` entry for `sqlalchemy` with
  `ignore_missing_imports`, or exclude the file from the root `mypy` `files` list
  with a comment that it runs in the Phoenix runtime. Move the Phoenix version
  guard from `main()` in `tools/phoenix_server.py` into `fix_distinct_queries`
  and unit-test the pure `select` wrapper with a skip when SQLAlchemy is absent.
- Add `ruff check` and `ruff format --check` for `packages/ai-agent-lib-ui` to the
  `ui` CI job (its `pyproject.toml` already extends the root rules).
- Add a root import-linter contract for the UI (`contracts` and `application` may
  not import `chainlit`, `fastapi`, `starlette` or `ai_agent_lib_ui.integrations`),
  mirroring `tests/test_boundaries.py`, so the rule is enforced where the other
  layer rules live.
- Make the batch-launcher tests (`test_local_dev.py`, `test_stop_local.py`) pass
  `sys.executable` to the `.bat` files through the `PYTHON` variable they already
  honour, instead of depending on PATH.

Acceptance: `ruff`, `mypy`, `lint-imports`, `pytest` and the UI job all pass in
CI on both platforms from a clean checkout.

### A2. Log every silent catch; one error-mapping path per boundary (M)

Closes: review finding 2 and the exception-handling list.

Core:
- `integrations/http/agent.py:396-397` (`inspect`) and
  `integrations/http/live.py:55-56`: `_LOG.exception(...)` with `request_id` and
  `route` in `extra`, mirroring `_answer`.
- `integrations/http/outcomes.py:78`: raise `IntegrityError` (not `ValueError`) for
  the journal limit; open the connection and create the table once in `start()`.
- `adapters/data_rest.py:410-413`: keep the cause and the path note
  (`exc.add_note(f"data source {name!r}"); raise`).
- `observability/diagnostics.py` and `langgraph/_agent_tracing.py:184-190`: add
  `exc_info` and `error_type`/`request_id` to the warnings.
- Replace bare `ValueError`/`RuntimeError` with the taxonomy where the condition
  is configuration or lifecycle: `langgraph/tools.py:287-299`,
  `_agent_tracing.py:157`, `contracts/options.py:481-490`, the "not started"
  sites in `data_rest.py`, `data_duckdb.py` and `langgraph/checkpoint.py`
  (one `NotStartedError(ConfigurationError)`); update the three tests that pin
  `RuntimeError`.

AWS pack:
- Add `UpstreamError(AgentLibError)` to `contracts/errors.py` with
  `retryable` decided by a reason code; raise it from
  `guardrails_bedrock.py:228-232` and `data_redshift.py:461,477,532`; document it
  on the `DataSource.query` and `GuardrailCheck` ports.
- Replace the nine `from None` sites with `from exc` plus `detail=describe(exc)`.
- `data_redshift.py:454-455,524-525`: narrow the suppression to
  `(BotoCoreError, ClientError)` and emit a telemetry event with the statement id.
- `registry_s3.py:250-255`: close the `StreamingBody`.

CLI:
- `deploy.py:227-233`: raise `CliError` naming the file and the parse error.
- `policy.py:294-309`: capture OPA's stderr and include its last lines in the
  `CliError`.
- `readers.py:349-357`: record `problem.summary`, not the class name.
- Make `CliError` a subclass of `AgentLibError` and keep one
  `report_error(error) -> int` used only by `__main__`; use `click.UsageError`
  for option-combination mistakes in `commands/data_options.py`.

UI:
- Add a module logger to `adapters/http.py`, `application/chat.py:135` and
  `adapters/inspection.py:144`; log status, path, agent id, thread and run ids
  (never bodies).
- `__main__.py:120-124`: narrow the `ImportError` guard to the lazy imports and
  add `--verbose` to print the traceback.
- `integrations/server.py:127`: explicit `ContractError` to HTTP status map
  (`UNAVAILABLE` 503, `INVALID_RESPONSE` 502, `STALE_BINDING` and
  `DESTINATION_DENIED` 422) with one test per code.

Acceptance: a negative test per changed branch (the review lists them), and
`grep -rn "noqa: BLE001"` shows a log call within three lines of every match.

### A3. Split the four god modules along their seams (L)

Closes: the structure findings for core and the CLI. Each split keeps the public
module as a facade that re-exports the old names.

- `di/container.py` (946 lines): extract `di/lifecycle.py` (start, own
  lifecycle, close, the background-failure coordinator) and `di/graph.py`
  (dependency resolution, `_build`, `_check_options`, `_where_configured`).
  `ServiceContainer` keeps the accessors and facades and delegates. Read the
  `frame_tool_results` and `tracing` options from the parsed options models
  produced by `_check_options`; delete the hand-written reads in
  `container.py:773` and `config/telemetry.py:32`.
- `integrations/http/agent.py` (661 lines): split into `http/health.py`,
  `http/invoke.py`, `http/stream.py` and `http/app.py` (`agent_app`). Replace
  `_StreamRoute(_Invoke)` inheritance with a shared `RequestPrelude` helper
  (read body, validate ids, authenticate) used by both; build the response
  envelope as a dict once and serialise once. This split is the prerequisite for
  Part B's `/invocations` and `/ping` aliases.
- `adapters/identity_jwt.py` (729 lines): `identity_jwt_options.py` (options
  models and `resolve_jwt_options`), `jwks.py` (`_SigningKeys`, fetching outside
  the lock), `identity_jwt.py` (the verifier and `ExchangingJwtIdentity`).
- `cli/deploy.py` (633 lines): `deploy/plan.py` (settings, dependency analysis,
  `DeploymentPlan`), `deploy/hcl.py`, `deploy/files.py`, `deploy/starter.py`.
  Inject a `DistributionResolver` callable instead of reading `sys.modules`.
  Move the bundle and "ours vs kept" logic out of `commands/deploy.py` into
  `deploy/files.py`; move the new-mcp orchestration out of `commands/create.py`
  into `scaffold.py`. This is the prerequisite for Part B's second target.

Acceptance: no source module over 500 lines in core or the CLI; existing tests
pass unchanged except for import paths; new unit tests for `AdapterLifetime`,
`RequestAuthenticator`, `McpBindings`, the deploy helpers and `options_help`.

### A4. Close the layering gaps (M)

- Break the `pipeline` <-> `observability` cycle: move `query_parameters` and
  the redaction helpers under `contracts/redaction.py`; add an import-linter
  contract forbidding `pipeline -> observability`.
- `pipeline/assembly.py` gains `build_data_pipeline(...)`; `GovernedDataSource`
  stops assembling its own stages and `DataStage.RESILIENCE` is filled from the
  container's `Operations`.
- Integrations stop reaching into the container: `_agent_tracing.py` takes a
  `RegistrySource` and an invocation callable; `langfuse.py`, `phoenix.py` and
  `tracing.py` receive settings and sinks from `di/`. One generic
  `tracing_lifetime(...)` replaces the two near-identical context managers;
  `config/langfuse.py` and `config/phoenix.py` share one `OriginSettings`
  validator and one layered-resolution helper.
- `integrations/mcp/client.py`: `HttpMcpConnector` owns a long-lived
  `httpx2.AsyncClient` closed through `SupportsAsyncClose`; the connector and its
  timeout are injected from `McpBindings`; `GovernedMcpTool` gets a shared base
  instead of inheriting `GovernedTool`. Add the connector tests with
  `httpx2.MockTransport` (401, 403, connection error, `ExceptionGroup`).
- UI: pass `Runtime` explicitly into `DataLayer`, `current_principal` and the
  route factories; keep the locator only in `callbacks.py` (the file Chainlit
  loads by path); let `compose()` be re-entered in tests. Move `answer_id` and
  `outcome_text` into `integrations/answer_format.py`; give `TraceLink` one
  display label. Make `SqliteThreads.session/new_session/revoke` async via
  `to_thread` and drop the per-draft `current_principal()` call.

Acceptance: `lint-imports` with the new contracts passes; in-process tests exist
for `security.py`, `data_layer.py` and `server.py`; the MCP connector's missing
coverage lines (79-92, 223-230) are covered.

### A5. Tests onto the injected clock; split the dumping grounds (M)

- Move the wall-clock tests in `test_limits.py`, `test_http_serving.py`,
  `test_aws_data_redshift.py` and the UI protocol suite onto `FrozenClock` or
  event waits; keep at most one real-clock smoke test per file marked `slow`.
- Mark process-spawning tests `integration` or `slow`: `test_stop_local.py`,
  `test_local_service.py`, `test_package_runtime.py`, the generated-workspace
  suite in the CLI. Add a Windows leg for `test_readme_workflows.py`'s `cmd.exe`
  test or mark it platform-specific explicitly.
- Split `test_browser_protocol.py` (chat, security, branding) and `test_chat.py`
  (service, store, catalog, shell, events); replace `patch.object(store, "_touch")`
  with an injected commit hook.
- Remove the permanent skip of `test_anthropic_live.py` in CI by either wiring a
  secret into the integration job or marking it manual in its docstring.

### A6. Delete dead weight (S)

- Remove `experiments/chainlit-phase0/` except `evidence/` and the review
  document (moved under `docs/`); update `docs/frontend-phase0-review.md:13` and
  the UI README link.
- Remove the speculative UI contracts (`CatalogReader`, `CheckpointReader`,
  `DiagnosticReader`, `ArtifactStore`, `QueryBinding`, `ContentPreview`) or move
  them to a clearly named `contracts/future.py` outside the public surface.
- `di/defaults.py:59-71` duplicate `TYPE_CHECKING` imports; `identity_jwt.py:71`
  dead alias; `aws/access.py:265-272` ImportError fallback; `contracts/__init__`
  export drift (enforce with a test that `__all__` equals the union of submodule
  `__all__`s).

### A7. Shared service composition in core (M)

The review found the entry-point wiring copied six times across the examples and
the generated templates. Add to `ai_agent_lib_core.integrations.http`:

- `run_service(name, env_file, serve_coroutine)`: `load_service_config`,
  `configure_logging`, `start_telemetry`, `asyncio.run`, `stop_telemetry`,
  `report_error`, with the interrupt convention in one place.
- `question_input(given) -> str` and
  `registered_mcp_tools(services, application)`.

Update the CLI templates (`agent/__main__.py.jinja`, `agent/service.py.jinja`,
`mcp/__main__.py.jinja`) to call them. The examples may adopt them later; they
are not changed in this plan. This is also where Part B's AgentCore entry point
plugs in.

## Part B: AgentCore readiness review and plan

### B1. What AgentCore requires, and where the library stands

AgentCore Runtime hosts one container (or one zip of code) per agent or MCP
server in a per-session microVM. It does not run sidecars, it fixes the network
contract, and it brings its own identity, memory, gateway, policy and
observability services. The table maps each requirement to the library today.

| AgentCore requirement | Library today | Gap | Change |
| --- | --- | --- | --- |
| HTTP protocol: `POST /invocations`, `GET /ping` on `0.0.0.0:8080`, ARM64 | `POST /invoke`, `/invoke/stream`, `GET /healthz`, `/readyz`; default host `127.0.0.1`, port from the workspace; images build for the host architecture | Paths, port, bind address, architecture | Route aliases and an `agentcore_app()` composition (B2); ARM64 build (B6) |
| `/ping` returns `{"status": "Healthy" \| "HealthyBusy"}`; `HealthyBusy` keeps the session alive during background work; a bad `time_of_last_update` prevents idle timeout | `/healthz` returns `{"status":"ok"}`; `/readyz` returns the lifecycle state with 503 until ready | Body shape; busy signal | `/ping` from `ServiceLifecycle`: `Healthy` when ready, `HealthyBusy` while a turn is admitted, non-200 before readiness (B2) |
| Session header `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` (at least 33 characters) identifies the microVM; the payload is passed through unchanged | Thread id comes from the JSON body (`thread_id`) and is validated by `_SAFE_ID`; request id from `x-request-id` | The session id must become the default thread id, and `_SAFE_ID` must accept it | Read the header when the body has no `thread_id`; verify or widen `_SAFE_ID` (B2) |
| One session, one microVM; ephemeral disk; sessions idle out (default 15 min) and end (default 8 h) | `InMemoryThreadAdmission`, the SQLite outcome journal and the SQLite checkpointer are all `local_only` or process-local | Correct by construction once the one-way guard refuses `local_only` adapters in `prod`; the journal must be off or external | Target preflight refuses `local_only` and the SQLite journal (B6); checkpoints go to Postgres or AgentCore Memory (B5) |
| Inbound auth is either IAM SigV4 or a JWT authorizer (`discoveryUrl`, `allowedClients`, `allowedAudience`, `allowedScopes`); the bearer reaches the container only when `requestHeaderAllowlist` includes `Authorization`; in SigV4 mode only `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` (opaque) reaches it | `RequestAuthenticator` takes a bearer token only; `JwtIdentity` verifies issuer, audience, keys; `entra` preset | JWT mode works today with the allowlist (the library re-verifies, which is the intended defense in depth). SigV4 mode has no identity source | `RequestAuthenticator` accepts a `CredentialSource` (bearer, or trusted headers); new `agentcore_runtime` identity adapter in the AWS pack that builds the `Principal` from the user-id header and marks the session trusted-at-perimeter (B3) |
| Outbound auth: the runtime delivers a `WorkloadAccessToken` header; agent code exchanges it for downstream tokens through AgentCore Identity (`GetResourceOauth2Token`, M2M or `USER_FEDERATION`) with credential providers (`CustomOauth2`, Microsoft, Google, GitHub) | `TokenExchangeOptions` does on-behalf-of or RFC 8693 directly against the identity provider; the agent never forwards the user token | An alternative exchange backend is needed where the runtime, not the agent, holds the client secret | `agentcore_identity` token-exchange adapter in the AWS pack behind the existing exchange port, keyed by `WorkloadAccessToken` (B3) |
| MCP protocol: `0.0.0.0:8000/mcp`, streamable HTTP, stateless by default; the platform injects `Mcp-Session-Id`; servers must accept it; JSON-RPC errors over HTTP 200 | Generated servers already use `streamable_http_app(stateless_http=True, json_response=True)`; `DoorTokenVerifier` requires a bearer | Port and bind address; SigV4 mode has no bearer | Server entry point defaults for the target (B2); a "runtime-trusted door" mode for SigV4 runtimes (B3) |
| Calling a runtime-hosted MCP server: `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped arn}/invocations?qualifier=DEFAULT` with a bearer, or SigV4; capture and echo `Mcp-Session-Id` | `HttpMcpConnector` sends a bearer over `httpx2`; registry entries hold a URL | SigV4 signing; ARN-to-URL resolution | `agentcore` MCP connector in the AWS pack (SigV4 via botocore, or bearer from B3); `ToolRegistry` server entry gains `runtime_arn` (B4) |
| Gateway: aggregates MCP targets into one virtual server (tools named `target___tool`), inbound JWT, outbound IAM role / OAuth credential provider / token passthrough; runtime targets are forwarded without aggregation; Policy (Cedar) and Guardrails apply at the gateway | `verify_registration` fingerprints tools by exact name; no notion of a gateway | Tool-name prefixing breaks pins; nothing provisions a gateway | Registry entry `tool_prefix`; `agentlib deploy --target agentcore` can emit a gateway with the workspace's MCP servers as targets (B4, B6) |
| Observability: the runtime sets ADOT environment variables and expects `opentelemetry-instrument` as the entry point; `aws-opentelemetry-distro>=0.18.0`; `DISABLE_ADOT_OBSERVABILITY=true` opts out; session id propagates from the header or `session.id` baggage | `start_telemetry` installs its own `TracerProvider` and OTLP exporters when `EAP_TELEMETRY=opentelemetry` | Two tracer providers collide; the library's spans would miss CloudWatch | New `TelemetryMode.AMBIENT`: use the global providers ADOT installed, install nothing; `agentcore` extra pulls `aws-opentelemetry-distro`; set `session.id` baggage from the session header (B2, B6) |
| Policy: no sidecars; Policy in AgentCore evaluates Cedar at the Gateway for tool calls | OPA sidecar on `localhost:8181` is the production policy decision point; `rules` adapter is in-process | OPA cannot run beside the container | Target preflight refuses `policy=opa` with a loopback URL; recommend the in-process `rules` adapter or a remote OPA URL; document Cedar at the gateway as the second layer (B6) |
| Deployment: container image in ECR (ARM64, up to 2 GB) or direct code zip (ARM64 manylinux wheels, 250 MB zipped, `PYTHON_3_13`, `entryPoint`); `CreateAgentRuntime` / Terraform `aws_bedrockagentcore_agent_runtime`; IAM execution role; `lifecycleConfiguration` | `agentlib deploy` writes ECS/Fargate Terraform, a Dockerfile and IAM statements derived from each adapter's `Access` declaration | A second target | `agentlib deploy SERVICE --target agentcore` (B6) |
| Memory: `AgentCoreMemorySaver`/`AgentCoreMemoryStore` from `langgraph-checkpoint-aws`, keyed by `actor_id` and `thread_id`; IAM `CreateEvent`, `ListEvents`, `RetrieveMemories` | `CheckpointBackend` port; `postgres` adapter in the AWS pack; scope-namespaced thread ids | None required (Postgres works from AgentCore); Memory is the native option | `agentcore_memory` checkpoint adapter in the AWS pack (B5) |

### B2. Workstream: AgentCore-shaped HTTP and MCP serving (core, M)

Depends on A3 (the `http/agent.py` split) and A7.

- `integrations/http/agentcore.py`: `agentcore_app(services, run, *, application,
  lifecycle, ...)` returns the same Starlette app as `agent_app` with
  `POST /invocations` and `GET /ping` added as aliases of invoke and health. The
  existing routes stay so one image serves both contracts.
- `/ping`: `{"status": "Healthy"}` with 200 once `lifecycle.ready`;
  `{"status": "HealthyBusy"}` while the thread-admission guard holds any thread;
  503 before readiness. Never set `time_of_last_update` on every ping.
- Session mapping: when the body has no `thread_id`, use
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`; reject a body `thread_id` that
  disagrees with the header. `_SAFE_ID` (`^[A-Za-z0-9._:-]{1,128}$`) already
  accepts the platform's 33-plus character ids and UUIDs, so no widening is
  needed; add a test that pins this. Put the session id into OpenTelemetry
  baggage as `session.id`.
- `serve()` keeps its defaults; the generated entry points gain
  `--profile agentcore` (or read a serve-profile variable) that sets host `0.0.0.0`,
  port 8080 for agents and 8000 for MCP servers, and selects `agentcore_app`.
- `TelemetryMode.AMBIENT`: `start_telemetry` installs nothing and returns a
  no-op flush; spans use `trace.get_tracer`, which already resolves to the
  global provider. Startup fails fast if `AMBIENT` is set and no global provider
  is installed (a clear message naming `opentelemetry-instrument`).
- `DoorTokenVerifier` gains a `trusted_perimeter` mode (B3) so an MCP server on a
  SigV4 runtime can admit calls that carry no bearer but do carry the runtime's
  headers.

Tests: ASGI tests for `/invocations` and `/ping` (ready, busy, not ready); the
header-to-thread mapping including the disagreement case; `AMBIENT` with and
without a provider; an MCP contract test that the server accepts a platform-set
`Mcp-Session-Id` in stateless mode.

### B3. Workstream: identity for both inbound modes and outbound exchange (core + AWS pack, L)

Depends on A3 (`identity_jwt.py` split) and the `RequestAuthenticator` extraction.

- `RequestAuthenticator.authenticate` takes a `CredentialSource` value (bearer
  token, selected headers, or both) instead of a bare string. The HTTP prelude
  builds it; existing callers pass the bearer.
- JWT mode (recommended for production): no new adapter. Document the runtime
  authorizer settings that must match the library's `JwtIdentityOptions`
  (`discoveryUrl` issuer equals `issuer`; `allowedAudience` equals `audience`;
  `requestHeaderAllowlist` includes `Authorization`). Add a `cognito` preset
  beside `entra` because the AgentCore quick starts use Cognito.
- SigV4 mode: `ai_agent_lib_aws/identity_agentcore.py` implements
  `IdentityVerifier` from `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` and the
  configured tenant; it is registered as `identity=agentcore_runtime`, allowed
  only when `deployment_env` is not `local`, and it records
  `authenticating_party="agentcore-runtime"` on the `Principal` so policy can
  tell perimeter-trusted identities from verified tokens. The pack's docs state
  the IAM guidance from AWS: deny `bedrock-agentcore:InvokeAgentRuntimeForUser`
  on runtimes that do not need user delegation.
- Outbound: `ai_agent_lib_aws/token_exchange_agentcore.py` implements the
  existing exchange port by calling `GetResourceOauth2Token` with the
  `WorkloadAccessToken` header value, a credential provider name and scopes
  (`M2M` for service-to-service, `USER_FEDERATION` when a user token was
  presented). Registered as `exchange.kind="agentcore_identity"`. The agent still
  never forwards the user's original token.
- `DoorTokenVerifier.trusted_perimeter`: for MCP servers on SigV4 runtimes,
  accept calls without a bearer when the configured runtime headers are present,
  and build the `Principal` through the same `agentcore_runtime` adapter.

Tests: contract suite runs for the new identity adapter (including the
cross-tenant leakage case); exchange adapter tested with a stubbed
`bedrock-agentcore` client for M2M and user federation; door tests for both
modes; a test that `agentcore_runtime` is refused in `local`.

### B4. Workstream: calling MCP servers through AgentCore (core + AWS pack, M)

Depends on A4 (connector owns its client; connector injected).

- `ToolRegistry` server entry gains optional `runtime_arn`, `qualifier`,
  `auth` (`bearer` | `sigv4`) and `tool_prefix`. `content_revision` and
  `verify_registration` strip the prefix before fingerprinting so gateway-exposed
  tools (`target___tool`) still pin.
- `ai_agent_lib_aws/mcp_agentcore.py`: an `McpConnector` that resolves the
  runtime URL from the ARN and region, signs with SigV4 through botocore when
  `auth=sigv4`, or sends the bearer obtained from B3 when `auth=bearer`; it
  captures and echoes `Mcp-Session-Id`, and maps the JSON-RPC error codes
  (`-32001`, `-32002`, `-32003`, `-32005` retryable conflict) onto `PolicyDenied`
  and `TransientError`.
- `McpBindings` selects the connector per server entry.

Tests: `MockTransport` tests for URL resolution, SigV4 header presence, session
id echo and each error code; registry tests for prefix stripping.

### B5. Workstream: state on AgentCore (AWS pack, S to M)

- `ai_agent_lib_aws/checkpoint_agentcore_memory.py`: `CheckpointBackend` over
  `AgentCoreMemorySaver(memory_id, region_name)` from `langgraph-checkpoint-aws`,
  under a new `agentcore` extra. Scope handling: the library's scoped thread id
  becomes `thread_id`; the scope's tenant and subject become `actor_id`. Run the
  shipped `CheckpointBackendContract` and the LangGraph checkpoint delegation
  tests against it (integration-marked; needs an account).
- Access declaration: `bedrock-agentcore:CreateEvent`, `ListEvents`,
  `RetrieveMemories` on the memory ARN, so the deploy target's IAM statements are
  derived like every other adapter's.
- Postgres stays supported; the target preflight requires one of
  `checkpoint=postgres|agentcore_memory|none` and refuses `sqlite`.

### B6. Workstream: `agentlib deploy SERVICE --target agentcore` (CLI, L)

Depends on A3 (`deploy/` split). The Fargate target is unchanged.

- `deploy.env` gains an `AGENTCORE_*` block: `AGENTCORE_PROTOCOL` (derived:
  `HTTP` for agents, `MCP` for servers), `AGENTCORE_AUTH=iam|jwt`,
  `AGENTCORE_DISCOVERY_URL`, `AGENTCORE_ALLOWED_CLIENTS`,
  `AGENTCORE_ALLOWED_AUDIENCE`, `AGENTCORE_NETWORK=PUBLIC|VPC` with subnets and
  security groups, `AGENTCORE_IDLE_TIMEOUT_SECONDS`,
  `AGENTCORE_MAX_LIFETIME_SECONDS`, `AGENTCORE_ARTIFACT=container|code`,
  `AGENTCORE_GATEWAY=none|create|<arn>`, `AGENTCORE_MEMORY=none|create|<id>`.
- Preflight (shared with `agentlib check --target agentcore`): refuse
  `local_only` adapters, `policy=opa` on loopback, `checkpoint=sqlite`, the
  SQLite outcome journal, `EAP_TELEMETRY=opentelemetry` (must be `ambient` or
  `off`), a Python version the runtime does not offer, and any dependency without
  an `aarch64-manylinux2014` wheel when `AGENTCORE_ARTIFACT=code`. Each refusal
  is a `ConfigurationError` with the variable to change.
- Container artifact: Dockerfile `FROM --platform=linux/arm64 python:3.13-slim`,
  `aws-opentelemetry-distro>=0.18.0` in the lock when telemetry is `ambient`,
  `EXPOSE 8080` or `8000`, `CMD ["opentelemetry-instrument", "python", "-m",
  "<package>.service", "--profile", "agentcore"]`; the existing dockerignore
  allowlist applies. Terraform: `aws_ecr_repository`, the build-and-push note,
  `aws_bedrockagentcore_agent_runtime` with `container_configuration`,
  `protocol_configuration.server_protocol`, `authorizer_configuration` or IAM,
  `request_header_configuration.request_header_allowlist = ["Authorization"]`
  in JWT mode, `network_configuration`, `lifecycle_configuration`,
  `environment_variables` from `deploy.env` (empty strings preserved, fixing
  `deploy.py:195`), an execution role with the adapter-derived statements plus
  `logs:*` for the agent log group and `logs:PutResourcePolicy` for unified
  spans, and `aws_cloudwatch_log_group`.
- Code artifact: a `deploy/<service>/build_zip.py` that runs
  `pip download --platform manylinux2014_aarch64 --only-binary=:all:
  --python-version 3.13 -r requirements.lock`, lays out `/var/task`, writes a
  `main.py` shim calling `service.main(["--profile", "agentcore"])`, sets POSIX
  modes 644/755, zips, and uploads to the bucket Terraform created;
  `code_configuration.entry_point = ["opentelemetry-instrument", "main.py"]`,
  `runtime = "PYTHON_3_13"`.
- Optional gateway: `aws_bedrockagentcore_gateway` with the workspace's MCP
  servers as MCP targets (outbound IAM role or OAuth credential provider), inbound
  JWT from the same discovery URL, and the agent runtime as a runtime target with
  `allowedWorkloadConfiguration` pinning the gateway. Emit the OpenAPI schema of
  the invoke envelope so Guardrails can apply to the HTTP target. If the Terraform
  provider lacks a resource at implementation time, fall back to a documented
  `boto3` script.
- Optional memory: `aws_bedrockagentcore_memory` when `AGENTCORE_MEMORY=create`.
- `agentlib deploy --plan` prints the target, protocol, port, auth mode, artifact
  kind and every refusal before writing anything, as it does for Fargate.

Tests: plan tests for every preflight refusal; rendered Terraform validated with
`terraform validate` in the container-marked job; a unit test that the Dockerfile
targets ARM64 and the right port per kind; zip builder tested with a fake
downloader.

### B7. Workstream: Agent Lab against AgentCore (UI, M, optional)

- An agent binding kind `agentcore` in `config/settings.py`: runtime ARN, region,
  qualifier, auth mode. `HttpDispatcher` gains a SigV4 signer (botocore) or a
  bearer from the user's session, sends the session header derived from the
  conversation's thread id, and understands the `x-amzn-ErrorType` header
  (`RetryableConflictException` retried with backoff, `RuntimeClientError`
  surfaced as the agent's own failure).
- The trace link opens the CloudWatch GenAI Observability view for the session.

### B8. Pilot and documentation (M)

- Deploy `examples/rmprep` (agent) and `rmprep-data` (MCP server) to a
  development account with `agentlib deploy --target agentcore`: JWT inbound from
  Entra ID, `agentcore_identity` exchange to the MCP runtime, Postgres or Memory
  checkpoints, `ambient` telemetry, no OPA (in-process `rules`). Record the run
  as `docs/agentcore-trial.md` in the style of `docs/phoenix-trial.md`.
- Developer guide: a new "Deploy to AgentCore" section generated from the target's
  options table by `docs/guide/build_guide.py`; ADR 0004 recording the choices in
  "Open decisions" below.
- `docs/Specification.md` §16 gains "AWS AgentCore deployment" beside Fargate.

## Roadmap

| Phase | Workstreams | Exit criterion |
| --- | --- | --- |
| 0 (first) | A1, A2 | All gates green in CI on both platforms; every broad catch logs |
| 1 | A3, A4, A6 | No module over 500 lines; new linter contracts kept; UI integrations tested in process; dead trees removed |
| 2 | A5, A7 | No wall-clock test outside `slow`; templates use the shared entry point |
| 3 | B2, B3, B5 | An agent image serves `/invocations` and `/ping` locally under `opentelemetry-instrument`; both identity modes pass contract suites; Memory checkpoints pass the contract |
| 4 | B4, B6 | `agentlib deploy rmprep --target agentcore --plan` passes preflight and renders validating Terraform for container and code artifacts |
| 5 | B8, then B7 | The rmprep trial is recorded; Agent Lab can chat with the deployed agent |

Phases 0 to 2 are the review's remediation and are independent of AgentCore.
Phase 3 can start after A3 and A4 land; it should not start before, because the
route aliases, the credential source and the telemetry mode all land in files
those workstreams split.

## Open decisions

These change the shape of B3, B5 and B6 and should be settled before Phase 3.

1. **Default inbound mode.** JWT with Entra ID keeps the library's verified
   `Principal` and needs no new identity adapter; SigV4 is simpler for
   service-to-service callers but yields a perimeter-trusted identity only.
   Recommendation: JWT by default, SigV4 supported.
2. **Checkpoint store on AgentCore.** Postgres (existing adapter, needs a VPC
   runtime and a database) or AgentCore Memory (new adapter, no infrastructure,
   also gives long-term memory). Recommendation: implement Memory, keep Postgres.
3. **Artifact kind.** Container (works with the existing allowlisted Dockerfile
   and CI, 2 GB) or direct code zip (faster iteration, 250 MB, 25 sessions per
   second). Recommendation: both, container first.
4. **Gateway fronting.** Whether `agentlib deploy` provisions a gateway by
   default. Recommendation: opt-in (`AGENTCORE_GATEWAY=create`), because it
   changes tool naming and adds a second identity hop.
5. **Policy decision point.** In-process `rules` with Cedar at the gateway, or a
   remote OPA. Recommendation: `rules` plus gateway Policy; keep OPA for Fargate.
6. **Python version.** The code artifact needs `PYTHON_3_13`; the library
   supports 3.11 to 3.13 and CI tests both. Recommendation: pin generated
   AgentCore services to 3.13.

## Sources

AWS documentation read on 22 October 2026:

- [Understand the AgentCore Runtime service contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html)
- [HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
- [MCP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html)
- [Deploy MCP servers in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp.html)
- [Use isolated sessions for agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)
- [Authenticate and authorize with Inbound Auth and Outbound Auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html)
- [RequestHeaderConfiguration](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_RequestHeaderConfiguration.html)
- [AgentCore Runtime targets (Gateway)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-http-runtime.html)
- [Direct code deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html) and [for Python](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html)
- [Add observability to your AgentCore resources](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [Integrate AgentCore Memory with LangChain or LangGraph](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-integrate-lang.html)
- [Policy in Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [Terraform `aws_bedrockagentcore_agent_runtime`](https://github.com/hashicorp/terraform-provider-aws/blob/main/website/docs/r/bedrockagentcore_agent_runtime.html.markdown)

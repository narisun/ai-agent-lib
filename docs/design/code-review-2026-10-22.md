# Code structure, exception handling, and test review

Reviewed the working checkout on 22 October 2026 at commit `ced474e`, including
the uncommitted changes to rmprep's response review, the Phoenix SQL shim and its
checks. All 478 Python files were read: the core, AWS, CLI and UI packages, the
rmprep and accounts examples, `tools/`, and `tests/architecture`. This is a review
of the present implementation, not of the last Git diff. Nothing was fixed in this
pass; every finding below is a proposal. Line numbers refer to the working tree on
the review date.

## Assessment

Keep the architecture. Contracts are framework-free, dependency injection is real
rather than nominal (clocks, id generators, sleep, HTTP transports, AWS sessions and
MCP connectors are all injectable), third-party exceptions are translated into one
error taxonomy with `expected`/`actual`/`fix`, and the contract suites run against
every adapter and every fake. Test density is high: about 1,470 tests, with test
code roughly 70% the size of production code, and the default suite measures 95%
line coverage.

The maintainability debt is concentrated, not diffuse:

1. Four modules have become god modules: the service container (946 lines), the
   HTTP agent integration (661), the JWT identity adapter (729) and the CLI's
   deployment planner (633). rmprep's `response_review.py` (435 lines and growing in
   the uncommitted change) and `selection.py` (483) are heading the same way.
2. Two layering gaps the import linter does not see: a `pipeline` <-> `observability`
   import cycle in core, and the UI's module-level `Runtime` service locator.
3. Several places still swallow exceptions silently: two HTTP handlers in core, the
   UI's agent-failure path, the CLI's pyproject reader and OPA launcher, and Redshift
   statement cancellation.
4. The flagship example (rmprep) and the UI run a weaker lint rule set than the
   library and are not linted in CI at all. The library's rules applied to rmprep
   produce 77 violations, including functions of complexity 18 and 30.
5. The rmprep agent and its MCP server share vocabulary (evidence-limit strings, tool
   names, section keywords) by copied string literals with no shared contract.
6. The uncommitted `tests/architecture/phoenix_sql_checks.py` breaks the root `mypy`
   gate.

## Checks run for this pass

The checked-in `.venv` and `packages/ai-agent-lib-ui/.venv` were both broken on the
review machine (`pydantic_core` import error; `mypy` missing `librt`), so the gates
were run in a fresh environment built with `tools/install_dev.py`. That is a
local-machine issue, not a code issue, but it means the gates had not been runnable
from this checkout.

| Check | Result |
| --- | --- |
| Repository Ruff lint and format checks | Passed; 528 files formatted |
| Import-linter | 11 contracts kept, 0 broken |
| Strict type checking (root config, 409 files) | **Failed**: 12 errors, all in the uncommitted `tests/architecture/phoenix_sql_checks.py` |
| Default library, AWS, CLI, example and architecture suite with coverage | 2,666 passed, 3 failed, 17 platform skips, 38 opt-in tests deselected; 95% line coverage |
| Library rule set applied to rmprep sources | 77 violations (see finding 3) |

The three failures are the batch-launcher tests
(`test_local_dev.py::test_batch_help_runs_from_another_directory[...]` and
`test_stop_local.py::test_stop_batch_runs_from_another_directory_with_spaces`). The
`.bat` files pick up whatever `python` is on PATH, which in the review environment
produced an "SRE module mismatch" between interpreters. That is an environment
artifact, but it shows those tests depend on the machine's PATH rather than on
`sys.executable`.

## Findings, in recommended fix order

### 1. P1 — The uncommitted Phoenix SQL check breaks the type-checking gate

**Location:** [`tests/architecture/phoenix_sql_checks.py`](../../tests/architecture/phoenix_sql_checks.py),
lines 17-18 and 31-33.

**Evidence:** the file imports `sqlalchemy`, which the root environment does not
install, and `setUpClass` assigns `cls.original_select`, `cls.modules` and
`cls.facilitator` without declaring them on the class. The root `mypy` `files` list
includes `tests`, so `python -m mypy` in CI fails with 12 errors on this working tree.

**Recommendation:** either exclude the file from the root `mypy` files (it is meant
to run inside the Phoenix package runtime) or declare the three class attributes and
add a `[[tool.mypy.overrides]]` entry for `sqlalchemy`. Also move the Phoenix version
guard from `main()` in [`tools/phoenix_server.py`](../../tools/phoenix_server.py)
(line 98) into `fix_distinct_queries` itself, and unit-test the pure `select` wrapper
(lines 45-53) against any SQLAlchemy column so the shim is not covered only by
integration tests that skip when Phoenix is absent.

### 2. P1 — Failures are swallowed with no diagnostic record

**Location:** core [`integrations/http/agent.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/http/agent.py)
lines 396-397 (`inspect`) and [`integrations/http/live.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/http/live.py)
lines 55-56; UI [`adapters/http.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/adapters/http.py)
lines 136-157, [`application/chat.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/application/chat.py)
line 135, [`adapters/inspection.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/adapters/inspection.py)
line 144; CLI [`deploy.py`](../../packages/ai-agent-lib-cli/src/ai_agent_lib_cli/deploy.py)
lines 227-233 and [`policy.py`](../../packages/ai-agent-lib-cli/src/ai_agent_lib_cli/policy.py)
lines 294-309; AWS [`data_redshift.py`](../../packages/ai-agent-lib-aws/src/ai_agent_lib_aws/data_redshift.py)
lines 454-455 and 524-525.

**Evidence:** core's `inspect` returns 503 and the live SSE path puts `"internal"` on
the queue, both under `except Exception` with no log line, while the neighbouring
`_answer` (agent.py lines 467-471) correctly calls `_LOG.exception`. The UI's HTTP
adapter has no logger at all; any 4xx whose body is not the exact envelope becomes
`state="failed", failure_code=None` and the body is dropped, so the user reads "Check
the agent logs for the cause" while the UI log says nothing. The CLI's `_project`
returns `{}` on `OSError` or `TOMLDecodeError`, so a corrupt `pyproject.toml` is
reported as a missing-dependency problem; OPA is launched with `stderr=DEVNULL`, so
an early exit yields only "OPA did not start". The Redshift best-effort
`cancel_statement` is wrapped in `contextlib.suppress(Exception)`, so a statement
still running at the warehouse after a timeout is invisible. None of these branches
has a test; coverage confirms the four broad catches in
`observability/diagnostics.py` (lines 149-152, 176-180, 222-223) are the uncovered
lines of that module.

**Recommendation:** log at every degradation point with error type, place and
request/thread identifiers (never bodies), keep user-facing text content-free, raise
a `CliError` naming the file for the pyproject case, capture OPA's stderr into the
error, and narrow the Redshift suppression to the SDK error types with a telemetry
event carrying the statement id.

**Regression cases:** a failing `TurnInspector` produces a 503 and a log record; a
journal write failure during SSE produces an `error` event and a log record; a UI
dispatch that gets a 422 without the envelope records the status and path; a corrupt
service pyproject names the file; OPA exiting at startup surfaces its stderr.

### 3. P1 — The example and the UI are held to a weaker standard than the library

**Location:** [`examples/rmprep/pyproject.toml`](../../examples/rmprep/pyproject.toml)
lines 60-65; root [`pyproject.toml`](../../pyproject.toml) line 7 (ruff `exclude`);
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) `ui` job.

**Evidence:** the rmprep workspace overrides ruff with
`select = ["E","F","W","I","B","UP","SIM","RUF"]`, dropping BLE, C90, D, S, T20,
ARG and DTZ. The root `ruff check .` excludes `examples/rmprep` and
`packages/ai-agent-lib-ui`, and the `ui` CI job runs tests and mypy but no ruff, so
neither tree is linted in CI. Applying the library's rule set to rmprep today yields
77 violations: `selection.py:326 _select_context` at complexity 18,
`rmprep_data/signals.py:53 review_findings` at complexity 30 (147 lines with inline
thresholds), three bare `except Exception` without a justification
(`service.py:190`, `__main__.py:43`, `rmprep_data/__main__.py:63`), and seven
`print` calls in `examples/trace_react.py`. Separately, both the non-package
workspace root and the agent are named `rmprep`, so `uv tree` in that workspace
fails with "two workspace members are both named rmprep".

**Recommendation:** replace the workspace `[tool.ruff.lint]` block with
`extend = "../../pyproject.toml"` as the UI already does, add ruff steps for both
trees to CI, fix the resulting violations (which forces the two complexity splits),
and rename the workspace root project.

### 4. P1 — The agent and its MCP server share vocabulary by copied literals

**Location:** rmprep [`response_review.py`](../../examples/rmprep/agents/rmprep/src/rmprep/response_review.py)
lines 54-58, 182-184, 318-319 (uncommitted), 395-398;
[`context.py`](../../examples/rmprep/agents/rmprep/src/rmprep/context.py) lines 80-96;
[`context_tools.py`](../../examples/rmprep/agents/rmprep/src/rmprep/context_tools.py) lines 35, 44;
[`specialist.py`](../../examples/rmprep/agents/rmprep/src/rmprep/specialist.py) lines 136, 179;
server [`measurements.py`](../../examples/rmprep/mcp-servers/rmprep-data/src/rmprep_data/measurements.py)
lines 19-37 and [`sections.py`](../../examples/rmprep/mcp-servers/rmprep-data/src/rmprep_data/sections.py) line 60.

**Evidence:** the agent activates review rules by comparing against strings copied
from the server's `EVIDENCE_LIMITS` (`"not_supplied_by_exception_record"`,
`"not_established_by_this_source"`, and three more), spells the MCP tool prefix two
ways (`rmprep-data.` and `rmprep-data_`), and hard-codes a keyword-to-section alias
table with no link to the server's `SECTIONS`. Each side's tests restate the literals
independently (`test_waiver_review.py:31`, `test_measurements.py:28-34`), so renaming
a limit on the server silently disables the agent-side check with every test green.
The uncommitted change adds the fifth such literal.

**Recommendation:** publish the limit vocabulary, tool identifiers and section keys
from one importable module (in `rmprep_data`, or the registry document) and import it
on both sides; add one cross-package test that fails when either side drifts.

### 5. P2 — The only production MCP connector is untested and opens a client per call

**Location:** core [`integrations/mcp/client.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/mcp/client.py)
lines 66-113 and 204-205; [`integrations/langgraph/bindings.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/langgraph/bindings.py) line 170.

**Evidence:** `HttpMcpConnector`, `load_mcp_tools`, `_refused_at_the_door` and
`_unwrap` have zero test references; the "server refused token" tests use a recording
fake, so the `HTTPStatusError -> PolicyDenied` and
`HTTPError/OSError/TimeoutError -> TransientError` mapping and the `ExceptionGroup`
unwrapping never run. Coverage confirms it: the missing lines of the module are
exactly 79-92 and 223-230. Every governed tool call opens a new `httpx2.AsyncClient`
and a new MCP session with a hidden 30 s timeout (line 73) that no options model
exposes, and `bindings.py` constructs the connector on the fly. `GovernedMcpTool`
(lines 116-163) also inherits `GovernedTool` for reuse without setting `_inner`, so
a positional tool input raises `AttributeError` instead of `ValidationFailed`.

**Recommendation:** test the connector with `httpx2.MockTransport` for 401/403,
connection error and an `ExceptionGroup` wrapping each; let the connector own a
long-lived client closed through `SupportsAsyncClose` and inject it with its timeout
from the composition root; extract a shared tool base instead of inheriting.

### 6. P2 — AWS adapters raise the base error class and chain causes inconsistently

**Location:** [`guardrails_bedrock.py`](../../packages/ai-agent-lib-aws/src/ai_agent_lib_aws/guardrails_bedrock.py)
lines 228-232; [`data_redshift.py`](../../packages/ai-agent-lib-aws/src/ai_agent_lib_aws/data_redshift.py)
lines 461, 477, 532; `from None` in `secrets_manager.py:321,324`,
`registry_s3.py:236-237`, `audit_firehose.py:102`, `data_redshift.py:406`,
`session.py:301`.

**Evidence:** upstream failures are raised as `AgentLibError` itself; the
`DataSource.query` port promises only `ValidationFailed`, `PolicyDenied` and
`TransientError`, and `retryable` and `fallback_allowed` are both false on the base
class, so nothing downstream can classify them. Nine sites drop the SDK cause with
`from None` while sixteen keep it with `from exc` plus `detail=describe(exc)`;
`errors.causes()` walks `__cause__`, and `describe()` already keeps SDK text out of
messages, so the `from None` sites lose information for no gain.

**Recommendation:** add an `UpstreamError` to the taxonomy (or map by error code to
`TransientError`/`ConfigurationError`/`ValidationFailed`), document it on the port,
and standardise on `from exc` with `detail`.

### 7. P2 — Agent Lab blocks its event loop and wires through a module-level locator

**Location:** UI [`adapters/sqlite.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/adapters/sqlite.py)
lines 524-532; [`integrations/security.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/integrations/security.py)
lines 24 and 66; [`integrations/server.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/integrations/server.py)
line 162; [`integrations/runtime.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/integrations/runtime.py)
lines 35-58; [`integrations/data_layer.py`](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/integrations/data_layer.py) lines 30-83.

**Evidence:** `SqliteThreads.session()` opens a fresh connection with `timeout=5`
and is called synchronously from the async ASGI middleware, from `new_session` and
`revoke`, and from `current_principal()` on every live-draft tick (up to 20 per
second); every other store method uses `asyncio.to_thread`. A lock wait stalls the
whole server. The module-level `_runtime` and `runtime()` locator are used
throughout the Chainlit layer while `SessionBoundary` gets its resources injected,
and `compose()` refuses a second call, so `callbacks.py`, `data_layer.py`,
`security.py` and `server.py` have no in-process tests. Framework-neutral
presentation (`answer_id`, `outcome_text`) lives in the Chainlit data layer, so
plain FastAPI pages import Chainlit transitively and `test_chat.py` must set
`CHAINLIT_APP_ROOT` just to import it.

**Recommendation:** make session lookups async via `to_thread` (or cache with
periodic revalidation) and drop the per-draft principal check; pass `Runtime`
explicitly into the data layer, security and route factories and keep the locator
only at the Chainlit file boundary; move `outcome_text` and friends into
`integrations/answer_format.py`.

### 8. P2 — A package cycle and raw option reads undermine the layering core enforces

**Location:** core [`pipeline/data.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/pipeline/data.py)
line 34 and [`observability/logs.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/observability/logs.py)
line 26; [`di/container.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/di/container.py)
line 773; [`config/telemetry.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/config/telemetry.py) line 32.

**Evidence:** `pipeline.data` imports `observability.query_logs` while
`observability.logs` imports `pipeline.context`; the import-linter contracts only
forbid pipeline -> di and integrations, so the cycle is invisible to the gate. The
container reads `options.get("frame_tool_results", True)` and the telemetry config
reads `options.get("tracing", False)` by hand, duplicating `GuardrailsOptions` and
`AuditOptions`; `tests/unit/test_guardrails.py:403-407` shows the resulting type
error surfacing at `services.tools(...)` time instead of at `start()`.

**Recommendation:** move `query_parameters` under `contracts.redaction` (its only
dependency) and add an import-linter contract between `pipeline` and
`observability`; read `frame_tool_results` and `tracing` from the parsed options
model that `_check_options` already produces. Add a UI boundary contract to the root
linter as well, as the 13 October review proposed.

## Further findings by concern

### Structure, separation of concerns, dependency injection

Core:

- `di/container.py` lines 102-946: `ServiceContainer` owns lifecycle (207-394),
  construction (835-946), options validation (885-936), typed accessors, a LangGraph
  facade (546-575), an MCP facade (629-707), authentication (577-627) and
  `GovernedDataSource` assembly (506-542), although its docstring says the facade
  delegates to focused collaborators. Extract a lifecycle/graph collaborator as was
  done for `McpBindings` and `RequestAuthenticator`.
- `config/langfuse.py` and `config/phoenix.py` are near-duplicates (same origin
  regex, same env > extra-file > dotenv layering, same `flag()` parser, same
  masking). One origin validator and one layered-resolution helper.
- `pipeline/data.py` lines 140-154 assemble their own pipeline although
  `pipeline/assembly.py` declares itself the one place that decides which stages are
  active; `DataStage.RESILIENCE` (`stages.py:64`) is never filled, so governed queries
  get no per-attempt timeout or retry.
- `integrations/langgraph/_agent_tracing.py` lines 37, 41, 244, 289, 344 take a
  `ServiceContainer` and call `services.registry.tools.entries()` and
  `services.invocation(context)`; `langfuse.py:80-85`, `phoenix.py:70-75` and
  `tracing.py:24` load settings themselves (with a `Path(".env")` default) and
  construct `JsonlModelTraceSink` inside the integration layer. Pass a registry and
  an invocation callable; move settings loading and sink construction to `di/`.
- `integrations/http/agent.py` (661 lines) mixes health routes, invoke, status,
  inspect, SSE streaming and app assembly. `_StreamRoute` inherits `_Invoke` and
  passes a sentinel `_unused_run` that raises `AssertionError` (504-513, 565-566);
  `_recorded_answer` re-parses its own `JSONResponse.body` (347-348); the terminal
  error set includes `"unauthorized"`, which `_answer` can never return (355-365).
- `adapters/identity_jwt.py` (729 lines) holds options models (95-173), about 155
  lines of preset resolution (200-355), the JWKS cache (380-453), the verifier and
  `ExchangingJwtIdentity`; `checkpoint_options.py` is the precedent for splitting
  options out. Dead alias `_APP_TOKEN_KIND = _APP_TOKEN` at 71-72.
- `integrations/langgraph/model.py` lines 425 and 443 hard-code an adapter name
  (`kind="simulated" if self.provider_name == "fake" else "reported"`); line 480
  imports `output_preview_check` inside the per-call hot path. Add a
  `simulated_usage` capability flag.
- Duplicated private protocols and deadline denials: `_HasContext` in
  `pipeline/deadline.py:26` and `pipeline/identity.py:13`; `_Call` in `budget.py:97`
  and `resilience.py:28`; the `deadline_exceeded` denial is built three ways
  (`deadline.py:35-42`, `resilience.py:136-144`, `identity.py:33-34`, the last
  without `expected`/`fix`).
- `pipeline/budget.py:131`: `BudgetInterceptor(kind: str)` branches on `== "model"`;
  any other string silently counts as a tool call. Use a `Literal`.
- `pipeline/stages.py` lines 34, 49, 51-52: reserved enum members
  (`ModelStage.CONTEXT`, `ToolStage.APPROVAL`, `IDEMPOTENCY`, `JUDGE`) have no
  interceptor but are locked into the golden order test.
- `di/defaults.py` lines 59-71: the `TYPE_CHECKING` block re-imports nine port names
  already imported at runtime on lines 47-57.
- `contracts/__init__.py` export drift: `StreamingGuardrailCheck`,
  `REGION`/`ACCOUNT`/`NO_ACCESS`, `SchemaMismatch`, `redaction`, `options_help`,
  `diagnostics` and `inspection` are in submodule `__all__`s but not re-exported, so
  the AWS pack and the CLI import from submodules.

AWS and CLI:

- `cli/commands/deploy.py` lines 29-44 and 98-110 compute the Rego bundle and the
  "ours vs kept" file set inside the Click handler; `commands/create.py` lines
  249-302 hold the whole new-mcp orchestration. Both are testable only through
  `main([...])`.
- `cli/read_redshift.py:203`: `fetch_catalog` constructs
  `AwsSessionFactory(profile=..., region=...)` itself, ignores `ExternalSettings`
  (CA bundle, proxy) and never calls `aclose()`.
- `cli/deploy.py` (633 lines) mixes settings reading, dependency analysis, plan
  prose, HCL serialisation, file assembly and starter-env authoring;
  `_owning_distribution` (236-252) decides what the service image needs from
  `sys.modules` and `packages_distributions()` of the CLI's own process, so tests
  must monkeypatch both (`test_cli_deploy.py:394-411`).
- `aws/access.py` lines 265-272 and `aws/pack.py` lines 150-153 import
  `PostgresCheckpointOptions` from `checkpoint_postgres` behind a try/except
  ImportError fallback although `checkpoint_options.py` exists precisely so the
  options import without the driver (and `pack.py:41` already does so).
- `cli/read_csv.py` lines 63-77 and `cli/read_redshift.py` lines 116-130 each define
  their own `_parameter_type` with separate type-name tables.
- `aws/pack.py` lines 135-145: the "no DRAFT guardrail in prod" rule lives in the
  registry wiring closure, so the adapter used directly cannot enforce it.

UI:

- `adapters/sqlite.py` (539 lines) mixes schema bootstrap, HMAC cursor signing
  (duplicated at 199-211/444-455 and 251-261/480-487), session-token hashing, and a
  `CREATE TABLE IF NOT EXISTS reconciliations` inside a write path (388-391) that is
  absent from `migrations/001_local.sql`.
- `config/shell.py` lines 320-328: `prepare_shell` mutates `os.environ` (five
  `CHAINLIT_*` variables) and writes files as a side effect of composition.
- The failure-code set is spelled out four times (`contracts/conversations.py:72`,
  `adapters/http.py:88,147`, `adapters/events.py:75`).
- `experiments/chainlit-phase0/` is a 1,061-line stale duplicate of the UI package,
  module for module, outside pytest, mypy and CI; `spike/security.py` carries an
  older CSP with `'unsafe-eval'`, and `docs/frontend-phase0-review.md:13` still says
  the production package is unimplemented. Delete it, keep the evidence images and
  the review document.
- `contracts/ports.py` lines 37-140 and `contracts/queries.py` lines 54-65 and
  118-130: speculative contracts with no implementation (`CatalogReader`,
  `CheckpointReader`, `DiagnosticReader`, `ArtifactStore`, `QueryBinding`).

Examples and tooling:

- Two examples copy the same wiring verbatim: `_question()`
  (`accounts_agent/service.py:94` = `rmprep/service.py:129`),
  `registered_mcp_tools()` (`accounts_agent/graph.py:34` = `rmprep/graph.py:36`), and
  the `load_service_config -> configure_logging -> start_telemetry -> asyncio.run ->
  stop_telemetry -> report_error` entry point appears six times. Core lacks
  `question_input()`, `registered_mcp_tools(services, application)` and a
  `run_entrypoint()` helper; the 13 October review's proposal 4 said the same.
- `rmprep/response_review.py` (435 lines, +110 uncommitted) holds payload parsing
  (`tool_payload`, imported by four other modules), text utilities and five
  unrelated regex rule families; `_waiver_issues` (306-381) is 75 lines at
  complexity 12. Split parsing into `evidence_payload.py` and put each rule behind a
  small `issues(reply, evidence) -> list[Issue]` protocol.
- Conversation focus has three live representations (`state.py:21-27`,
  `focus.py:20-58`, `specialist.py:35`, `context.py:142`, `router.py:38-54`):
  `focus`, the legacy `active_client`/`active_memo_id`/`active_client_uens` triple,
  and `additional_kwargs["rmprep_context"]`, with migration code on two paths.
- Selection is computed on two divergent paths: `router.py:60-70` and the
  specialist's fallback at `specialist.py:101-110` pass different `active_clients`
  and different message windows; the fallback exists only so tests can skip the
  router.
- `rmprep/specialist.py` lines 97-223: `call_model` is a 127-line closure (selection
  fallback, budget, context assembly, model call, five checks, revision loop),
  testable only through `graph.ainvoke`.
- Dead or misplaced code shipped in the rmprep wheel: `langfuse_trial.py` and
  `langfuse_settings.py` (test-only callers), `examples/trace_react.py` (imports the
  sibling `rmprep_data` package that the agent's pyproject does not declare, and
  serves as a fixture for three test modules), and eight of eleven prompt files
  unreferenced (about 560 lines). Prompt loading is import-time I/O
  (`prompts/__init__.py:24-26`) and there is no record of what changed between
  versions.
- `tools/local_dev.py` lines 284-347: logic functions print progress inside the
  work, so tests assert on captured stdout; the Windows interrupt exit-code set is
  duplicated in `tests/architecture/test_stop_local.py:168`.

### Exception handling and fail-fast

- `core/adapters/data_rest.py` lines 410-413 re-raise
  `ConfigurationError(f"data source {name!r}: {exc}") from None`, flattening
  `expected`/`actual`/`fix` into a multi-line message and discarding the
  `add_note(f"in {path}")` added at line 261; the offending YAML path is lost.
- Inconsistent lifecycle-misuse errors: `data_rest.py:396,644`,
  `data_duckdb.py:101,253` and `langgraph/checkpoint.py:338` raise
  `RuntimeError("... not started")`; `checkpoint_none.py:25-28` and
  `RestDataSource.validate` (`data_rest.py:631`) raise `ConfigurationError` for the
  same condition. Tests pin both.
- Bare `ValueError` where the taxonomy exists: `langgraph/tools.py:287,290,299`
  (while `GovernedChatModel.bind_tools` raises `ConfigurationError` for the same
  mistake); `_agent_tracing.py:157`; `http/outcomes.py:78` (the journal size limit,
  which the HTTP layer then turns into an unlogged "internal");
  `contracts/options.py:481-490` (`Section(name)` raises `ValueError` though the
  docstring promises `ConfigurationError`); `aws/data_redshift.py:512`
  (`zip(strict=True)`); UI `catalog.py:125,143,145`, `shell.py:279`,
  `settings.py:239-249`, `sqlite.py:59`, which force `ui/__main__.py:133-144` to
  enumerate exception classes to pick a message.
- `cli/errors.py` lines 117-162: `CliError` re-implements `AgentLibError`'s shape
  as a parallel hierarchy; errors are formatted at three sites (`__main__.py:47-69`,
  `commands/check.py:338-340`, `commands/create.py:291-296`). Exit codes are also
  inconsistent for option mistakes: `click.UsageError` (exit 2) at
  `commands/common.py:247` versus `CliError` (exit 1) at
  `commands/data_options.py:339-363` for the same category.
- `cli/readers.py` lines 349-357: `check_queries` swallows the first `AgentLibError`
  and records `f"dropped {name}: {type(problem).__name__}"`, discarding message and
  fix.
- `ui/__main__.py` lines 120-124: `except ImportError` covers the whole 70-line try
  body, so any `ImportError` inside `compose()`, Chainlit or `dotenv` is reported as
  "Install the UI in its own environment"; the following `except Exception` flattens
  the rest with no opt-in traceback.
- `ui/integrations/server.py:127` maps `ContractError` to HTTP with
  `{NOT_FOUND: 404, INVALID_INPUT: 400}.get(code, 409)`, so `UNAVAILABLE`,
  `INVALID_RESPONSE`, `STALE_BINDING` and `DESTINATION_DENIED` all surface as 409.
- Broad catches that log constants only: `core/observability/diagnostics.py` lines
  87-91, 149-152, 176-180, 222-223 log `"diagnostics.write_failed"` with no error
  type, `exc_info` or request id; `_agent_tracing.py:184-190` likewise, while the
  sibling `model_trace.py:121-126` logs both; `ui/integrations/callbacks.py:343`
  logs only "UI operation failed".
- rmprep error messages lack identifiers: `memos.py:48` ("An accepted memo version
  cannot be changed", no memo id or version); `prompts/__init__.py:15` rejects
  without echoing the bad agent/type/version; `context.py:181-184` omits estimated
  versus budget token counts that `manifest` already holds;
  `tools/check_consumer.py:37` raises a bare `StopIteration` when a wheel is missing.
- `tools/*.py` raise bare `ValueError` for expected launcher failures
  (`local_dev.py:41,117,135,289,321,345`, `package_runtime.py:42-199`,
  `phoenix_dev.py:65`, `local_processes.py:49,57`) and `main()` catches
  `(OSError, ValueError, RuntimeError)` to print "Local startup error", so a
  programming `ValueError` is also reported as configuration without a traceback.
  `phoenix_dev.py:132` and `local_dev.py:336` swallow `KeyboardInterrupt` and exit 0
  while `stop_local.py:61` exits 1.
- `contracts/errors.py` lines 104-112: `AgentLibError.from_error` copies only base
  fields, so a subclass with extra state (`PolicyDenied.reason_code`,
  `CredentialsExpiredError.fix_command`) silently loses it. Latent today.
- `di/container.py` lines 488-503: `resolve_model` mutates the caught exception
  (`error.fix = ...`) instead of `add_note` as the rest of the container does.
- `aws/registry_s3.py` lines 250-255 read `reply["Body"].read(_MAX_BYTES + 1)` and
  never close the `StreamingBody` before raising the size error.

### Test coverage and test quality

Gaps:

- No direct tests for core `di/lifetime.py` (`AdapterLifetime`),
  `di/authentication.py`, `di/mcp.py`, `contracts/options_help.py`,
  `observability/trace_links.py` and `config/tracing.py::resolve_tracing_settings`;
  UI `security.py`, `data_layer.py`, `server.py` and `logs.py` (black-box only
  through the Socket.IO suite); CLI `deploy.py` pure helpers (`installed_extras`,
  `_missing_dependencies`, `deployment_files`, `_permissions_tf`, `_settings_tf`,
  `starter_settings`), exercised only end to end via substring assertions on rendered
  Terraform; `tools/check_consumer.py` and `consumer_model_contract.py`.
- Untested raising paths found above: the two silent HTTP catches, the journal
  size limit, `install.py:414,423`, `CliError.from_error` note joining, UI JWT
  identity mismatch (`security.py:131-132`), websocket close 4403,
  `DataLayer.delete_thread` 409, `FileCatalog.mcps` with a non-empty server list,
  `ProgressFeed.publish` coalescing and truncation, `__main__ init --overwrite`.
- `tests/integration/test_anthropic_live.py:23-26` skips unless
  `ANTHROPIC_API_KEY` is set and the CI integration job never sets it, so the test
  is permanently skipped in CI.

Quality:

- Wall-clock tests: `core/tests/unit/test_limits.py:272-301` (`SystemClock`, 60 ms
  deadline against a 120 ms sleep; `time.sleep(0.08)` against 30 ms),
  `test_http_serving.py:139` (`sleep(0.3)` then `assert not done`),
  `aws/tests/unit/test_aws_data_redshift.py:356-381,545-574` (50 ms deadlines,
  `time.sleep(0.3)`, 2 s polling), UI `test_browser_protocol.py` (busy-wait loops of
  100 x 50 ms, `sleep(0.3)` before asserting, bind-then-release `free_port`),
  `tests/architecture/test_stop_local.py:175-284` (four real servers plus a
  supervisor, bind-then-release ports, 25 s polling) and
  `accounts-agent/tests/test_accounts_agent_service.py:173` (polling with no
  deadline). The same files show the right pattern (`FrozenClock`, injected
  `sleep`); use it, and mark the process-spawning tests `integration` or `slow`.
- Dumping-ground test modules: UI `test_browser_protocol.py` (1,077 lines, one
  module-scoped real server steered by mutable fake attributes, also asserting SVG
  colour strings), UI `test_chat.py` (895, patches private `_touch`, raw
  `INSERT INTO runs`), rmprep `test_context_management.py` (655) and
  `test_named_context.py` (531); `test_focus.py:15` imports fixtures from another
  test module in a directory without `__init__.py`.
- Assertions on prose and magic counts: rmprep tests assert on issue prose
  (`"Waiver execution and its blockers" in issue`, five times in the new
  `test_waiver_review.py`) rather than a stable code; `test_prompts.py:19-23` asserts
  prompt sentences; `test_rmprep_tracing.py:51` asserts `len(queries) == 22`;
  `test_response_review.py:388-389` asserts 357 and 347; `test_phoenix_dev.py:306`
  (uncommitted) asserts `"Ran 5 tests" in stderr` and lines 261-265 Phoenix's exact
  banner spacing; `test_cli_data.py:384` asserts the bare class name of a dropped
  error. The new `test_waiver_review.py` also re-implements the revision-loop harness
  from `test_response_review.py:276-303`.
- Slow or environment-bound tests in the default suite:
  `cli/tests/generated/test_generated_workspace.py:93-191` runs real ruff, pin
  subprocesses, five scaffolds and `pytest` subprocesses with a 600 s timeout without
  an `integration` marker; `test_readme_workflows.py:280-314` runs only where
  `cmd.exe` exists, so never on the Ubuntu CI leg.
- Coupling to private state and third-party internals:
  `test_di_container.py:158,276` read `services._closed`;
  `test_aws_session.py:56-65,142-145` assert on
  `client._endpoint.http_session._verify`; `langgraph/checkpoint.py:44` reads
  LangGraph's private `configurable["__pregel_runtime"]` with no test that fails
  loudly if the key disappears.

Measured coverage (default suite, UI excluded as in the root configuration):

| Area | Statements | Missed | Coverage |
| --- | --- | --- | --- |
| core | 7,469 | 352 | 95.3% |
| aws | 886 | 46 | 94.8% |
| cli | 2,124 | 133 | 93.7% |
| rmprep (agent and data server) | 1,154 | 118 | 89.8% |
| accounts-agent | 95 | 29 | 69.5% |
| accounts-mcp | 54 | 54 | 0% (exercised only through subprocesses) |
| total | 15,800 | 732 | 95% |

Lowest-covered production modules and what the missing lines are:

- `core/integrations/http/serving.py` at 34%: the uvicorn server loop; its tests
  need Unix sockets and all skip on Windows, so nothing covers it on this platform.
- `accounts-agent/service.py` at 60%, and every `__main__.py` and `pins.py` in the
  examples at 0%: entry points are never exercised in-process.
- `core/integrations/langgraph/phoenix.py` at 63%: exporter construction and the
  context-manager body (38-44, 70-86).
- `cli/processes.py` at 73% and `cli/policy.py` at 81%: the OPA launch path
  (`policy.py:293-317`) is the uncovered block, the same block that discards OPA's
  stderr.
- `core/integrations/mcp/client.py` at 88%: the missing lines are exactly the
  connector (79-92) and the error mapping (223-230).
- `core/observability/diagnostics.py` at 84%: the missing lines are exactly the four
  broad catches.

### General best practice

- `core/di/sync.py` lines 50-53 and 73: `SyncServices.__init__` creates an event
  loop and thread eagerly (never closed if the object is not entered), `run()` blocks
  with no timeout, and `_stop` joins without a bound.
- `core/integrations/http/outcomes.py` lines 56-88 reconnect to SQLite, run `mkdir`
  and `CREATE TABLE IF NOT EXISTS` on every `begin`/`finish`/`read`, and multiplex
  three operations through a string parameter.
- `core/adapters/identity_jwt.py` lines 433-453 hold the `asyncio.Lock` across the
  JWKS network refresh (default 5 s), so every concurrent verification stalls behind
  one fetch; lines 403-404 map any non-200 JWKS status to "published no usable
  signing keys" without the status code.
- `cli/deploy.py:195` drops any `deploy.env` variable whose value is the empty
  string (`if (value := source.get(name))`), so an explicit empty-string override
  vanishes silently.
- In-place mutation of inputs: `rmprep/specialist.py:35` mutates the model message's
  `additional_kwargs`; `rmprep_data/measurements.py:185,205` `enrich_page` mutates
  `page` and each row and also returns it; `test_measurements.py:36` relies on the
  side effect.
- `core/config/resolver.py:257`: `warnings.warn(..., stacklevel=4)` is coupled to
  one call depth and points elsewhere from `explain()`.
- `core/config/reference.py` lines 54-82: `default_text()` re-runs the full resolver
  for every profile once per binding row.
- `core/observability/logs.py:105` and `langgraph/model.py:480` import inside hot
  paths with no cycle to justify it.
- UI pages are assembled by f-string concatenation with manual `escape()`
  (`server.py:27-49,194-219,239-292`, `logs.py:27-80`, `inspection.py:107-151`);
  three inline `style=` attributes are why the CSP must allow
  `style-src 'unsafe-inline'`.
- `ui/adapters/catalog.py` lines 215-249: `agents()` re-reads and re-parses both
  registry files per binding (2N+1 reads per `/catalog`).
- `ui/__main__.py:35`: `add_argument(..., action="append", default=[])` is the
  shared-mutable-default pattern.
- `core/adapters/token_exchange.py` lines 73 and 84: `verify: object = True` plus
  `type: ignore[arg-type]` where `ssl.SSLContext | bool` is the real type.
- Docstring drift: `aws/data_redshift.py` (the `_CLEANUP_SECONDS` docstring at
  line 254 is stranded after `_LateStatement`), `aws/testing.py:3-4` (says Stubber,
  ships fakes), `cli/processes.py:266-270` (`GraphReader` undocumented, `ToolRunner`
  documented twice), UI `settings.py:242-246` ("Phase 2 requires EAP_PROFILE=local"
  in a runtime message), `data_layer.py:239` ("Use Logs to archive" although
  archiving lives at `/conversations`).

## Changes that would simplify development

These are proposals, not additional confirmed bugs.

| Order | Suggestion | Benefit and boundary |
| --- | --- | --- |
| 1 | Fix the `mypy` break in `phoenix_sql_checks.py` before committing | Keeps the type gate green; no behaviour change |
| 2 | Log at every silent catch named in finding 2 | Operators can diagnose a 503, a failed UI run, a corrupt pyproject or an OPA exit from logs; user-facing text stays content-free |
| 3 | Lint the UI and rmprep with the library's rules in CI | Forces the two complexity splits and the three unjustified broad catches into the open; one rule set for the whole repository |
| 4 | Publish rmprep's evidence-limit, tool-name and section vocabulary from one module | A rename on either side fails a test instead of silently disabling a check |
| 5 | Test `HttpMcpConnector` with a mock transport and give it a long-lived client | Covers the only production MCP path and removes a per-call handshake |
| 6 | Add `UpstreamError` to the taxonomy; standardise on `from exc` plus `detail` | Downstream code can classify and retry; causes stay inspectable without copying SDK text |
| 7 | Move UI session lookups off the event loop; inject `Runtime` | Removes a 5 s stall risk and unlocks in-process tests for `integrations/` |
| 8 | Break the `pipeline` <-> `observability` cycle and add linter contracts (core pair, UI boundary) | The layering the repository already enforces becomes complete |
| 9 | Split `ServiceContainer`, `http/agent.py`, `identity_jwt.py`, `cli/deploy.py` and `response_review.py` along the seams named above | Each module keeps one responsibility; keep public names and signatures |
| 10 | Extract the shared example wiring into core; delete `experiments/chainlit-phase0`, unreferenced prompt versions and the langfuse facades | Two examples stop drifting apart; the wheel ships only what runs |

## What is done well

- Error taxonomy: every raise in core carries `expected`/`actual`/`fix`, causes are
  preserved with `from`, notes are added on the way out, and `describe`/`redact` keep
  secrets out of messages; tests assert that secrets never appear in `str(error)`.
- Fail-fast configuration: unknown variables with did-you-mean, every adapter's
  options validated before any factory runs, DuckDB queries explained at startup,
  REST endpoint definitions shape-checked, JWKS fetched in `validate()`, MCP
  registrations verified against live schemas.
- Fail-closed cross-cutting behaviour: audit raises `IntegrityError` on write
  failure, policy denials are never retried, deadlines are re-checked after blocking
  work, and stage order is enforced by enum and golden tests.
- Security hygiene: algorithm allowlist and required `kid`/`exp`/`iss`/`aud`;
  `trust_env=False`, `follow_redirects=False`, response-size caps; SQL as a sqlglot
  AST with bound parameters and a read-only check; strict YAML; constant-time
  access-code and cursor comparison; CSP and no-store headers; Markdown rendered with
  `html=False`.
- Test discipline: contract suites applied to every adapter and every fake,
  hand-written fakes rather than mocks, warnings as errors, network sockets blocked,
  injected clocks and id generators, and unusually thorough lifecycle edge cases
  (background failure, cancellation during construction, double close).
- Agent Lab's durable admission, compare-and-set transitions, partial unique index
  and explicit `unknown` state with read-only reconciliation are carefully designed
  and tested through restart and lost-response scenarios.
- The uncommitted documentation changes (README, `docs/phoenix-trial.md`,
  `context-management.md`, `data-review.md`) match the code.

## Validation and limits

The review read every module rather than sampling, and every finding carrying a
line number was checked against the working tree. No external model calls, business
records or credentials were used; the gates ran in a fresh local environment built
from `tools/install_dev.py`. Agent Lab's own suite was not re-run in this pass (its
separate environment was broken on the review machine), so its coverage figures are
not included above. Live Anthropic, Langfuse, Phoenix and AWS behaviour remain
outside this offline review.

# Code structure, reliability, and developer experience review

Reviewed the current working checkout on 13 October 2026, including the shared
Langfuse integration and Agent Lab changes. This is a review of the present
implementation, not just the last Git diff. The three confirmed runtime findings
below were subsequently fixed. Their original reproductions are retained to
explain the regressions; broader restructuring suggestions remain proposals.

## Assessment

Keep the current package boundaries. Framework-independent contracts, injected
providers, governed operations, and separate HTTP services are a useful foundation.
Business graphs can be tested with scripted collaborators, and Agent Lab does not
need to import an agent or open its checkpoint database. A broad rewrite or another
agent base class would add migration cost without addressing the failures found here.

The most valuable improvements are at boundaries: serialize conversation writes,
bound optional diagnostics, validate every enabled integration, and simplify service
startup. Documentation should lead with a working path and explain advanced options
where they become relevant.

The review followed request admission and recovery, scoped checkpoint access,
model/tool trace collection, configuration resolution, CLI generation/upgrades,
local process supervision, and UI persistence/presentation. Three findings were
reproduced offline using synthetic input. Existing regression checks complement
these reproductions; passing them does not establish that the uncovered cases work.

## Confirmed issues, in recommended fix order

**Fix status:** all three are implemented with regression tests. HTTP thread
admission rejects overlap before execution or request reservation and holds
ownership through cleanup. Langfuse checkpoint reads have a one-second deadline
and a 64-reference cap. Startup and CLI diagnostics share Langfuse resolution,
validation, masked output, and source tracking. Existing public entry points and
successful response formats remain compatible.

The default admission guard covers one application instance. Multi-worker or
multi-replica deployments must inject shared coordination; see
[thread admission](../http-thread-admission.md). Tests include two app instances
sharing an injected guard, but do not claim to validate a distributed backend.

### 1. P1 — Overlapping HTTP turns can lose an exchange from the current checkpoint

**Location:** core [`integrations/http/agent.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/http/agent.py),
`_Invoke._respond`, lines 271–275; also the separate `_StreamRoute` execution path.

**Trigger:** two requests use the same authenticated scope and thread but different
request IDs. This can happen through independent HTTP clients or separate Agent Lab
state folders. One Agent Lab instance already prevents concurrent admissions on a
thread; that protection does not extend to the agent's HTTP boundary.

The outcome journal reserves `(owner, thread, request)`, so both different requests
are admitted. Both graph invocations can read the same previous checkpoint and
produce competing continuations. The latest checkpoint contains only one branch.

**Reproduction:** a scoped in-memory LangGraph with an `add_messages` reducer and
one synthetic answering node. A barrier ensures both requests enter the node before
either finishes. Submit `first` and `second` concurrently through `agent_app`, with
`SqliteOutcomeJournal` enabled. Both return HTTP 200 and their corresponding answer.
Reading the latest state returns only one of the two human messages. Repeating the
probe changes which branch wins. The other branch may still exist in checkpoint
history; the problem is that subsequent conversation turns do not inherit both
accepted exchanges.

**Recommendation:** introduce thread admission keyed by tenant, subject,
application, and thread before graph execution. Reject or serialize overlap across
both invocation transports. Keep request deduplication separate from thread
serialization. A local lock can protect one process; shared deployments need an
injected lease/transaction mechanism or equivalent coordination across instances.
Hold ownership through actual graph completion, including streaming cancellation
and outcome persistence. Avoid an indefinitely growing dictionary of locks.

**Regression cases:** same-thread concurrent requests cannot both fork state;
different owners/threads remain concurrent; duplicate IDs still never execute
twice; cancellation releases ownership safely; JSON and both streaming routes obey
the same admission rule. Include a multi-instance contract test for a shared provider.

### 2. P2 — Optional checkpoint tracing can block an already completed answer

**Location:** core [`integrations/langgraph/langfuse.py`](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/langgraph/langfuse.py),
`LangfuseTracing.run` line 345 and `_checkpoints` lines 373–378.

**Trigger:** the business response finishes, but the following
`aget_state_history` read stalls. The 65-record limit bounds quantity, not elapsed
time. The exception handler only helps after the read raises.

**Reproduction:** inject a graph whose response immediately returns an accepted
answer and whose history iterator waits on an unset event. The response callback
finishes, but `tracing.run` stays pending until externally cancelled. No network
export is involved; the exporter is in memory.

**Impact:** optional diagnostics delay the final result and the HTTP outcome-journal
write. Agent Lab can reach its deadline and hold the thread as unknown even though
the graph already produced an answer and checkpoints.

**Recommendation:** give checkpoint reference collection a short, explicit deadline
and mark references unavailable on timeout. Return the business result regardless
of an ordinary diagnostic failure. An alternative is to collect checkpoint IDs
during the run, avoiding a post-response history query. Preserve caller cancellation
semantics and ensure cleanup does not leave an unobserved background task.

**Regression cases:** a history read that never yields, fails, or produces more
than the limit; an accepted answer still returns once; the trace identifies missing
references; cancellation closes spans; exporter failures do not retry business work.

### 3. P2 — `doctor` and `config explain` omit enabled Langfuse configuration

**Location:** CLI [`diagnostics.py`](../../packages/ai-agent-lib-cli/src/ai_agent_lib_cli/diagnostics.py),
`check_service` lines 65–69 and `explain_service` line 170.

**Trigger:** enable Langfuse in a generated agent's `.env` with a malformed URL,
missing keys, or another invalid Langfuse setting. The service calls the separate
Langfuse loader, while these CLI paths only call the main service resolver.

**Reproduction:** generate a fresh fake-model agent and append:

```dotenv
EAP_LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=invalid-origin
```

All ten service diagnostic checks pass, including `settings: resolved`.
`explain_service` returns no Langfuse variables. Calling the same Langfuse settings
loader used at service startup raises `ConfigurationError`.

**Impact:** a developer receives a successful diagnosis for a service that cannot
start, and the advertised configuration explanation cannot identify the cause.

**Recommendation:** let enabled integrations participate in a shared diagnostic
and configuration-explanation path. Reuse the existing Langfuse loader and its
precedence rules. Report whether tracing is disabled or valid, mask keys, and show
which file supplied each value. Keep syntactic/configuration checks separate from
an explicitly requested connectivity check; `doctor` should not export test content.

**Regression cases:** disabled settings, invalid origin, missing keys, a missing
separate environment file, process/file precedence, legacy rmprep aliases, and
secret redaction in both successful and failing CLI output.

## Changes that would simplify development

These are proposals, not additional confirmed bugs or commands implemented by this review.

| Order | Suggestion | Benefit and boundary |
| --- | --- | --- |
| 1 | Fix the three findings above before extending the simulator | Protect saved context and make completion/configuration behavior predictable |
| 2 | Add a workspace command such as `agentlib dev AGENT --ui` | Resolve the agent's registered MCP dependencies, start them in order, select the separate UI interpreter, and print the URL, login-code location, and log locations. Reuse the existing supervisor; preserve current commands. |
| 3 | Provide a real `agentlib update --plan` | Preview all file changes before writing. Keep current `--diff` compatibility, but clearly describe that it applies updates to untouched files and only previews edited files. |
| 4 | Extract reusable HTTP service composition | Reduce repeated logging, telemetry, Langfuse, container, lifecycle, and local-journal wiring in generated `service.py`, accounts-agent, and rmprep. Keep graph construction, response shape, and business policies injectable. |
| 5 | Normalize rmprep's legacy context once, then use one authoritative focus value | Reduce parallel updates to `active_client`, `active_client_uens`, `active_memo_id`, and `focus`. Preserve old checkpoints through explicit migration/projection at the boundary. |
| 6 | Separate UI page rendering from route orchestration as those pages grow | Keep ownership and transport checks in routes/services; share layout and presentation helpers for catalog, logs, inspection, and documents. Keep Chainlit hooks thin. |
| 7 | Add UI dependency-boundary checks alongside its existing tests | Enforce that contracts/application stay independent of Chainlit/FastAPI and agent implementations. Root import-linter currently lists core, AWS, and CLI only. |

### Make the common CLI path short

A new developer should be able to generate, install, test, and start one agent
without learning every provider option first. Keep the current lower-level CLI
for automation and advanced users; add small orchestration commands above it.

For a proposed workspace launcher:

- Show the resolved services, ports, interpreters, and configuration files before
  startup, without credentials. Validate duplicate ports and missing packages.
- Use the registry and workspace answers together: the registry defines permitted
  dependencies; the workspace identifies local source and start commands. Explain
  when a dependency is external instead of trying to start arbitrary registry URLs.
- Initialize Agent Lab bindings from an explicit selection. Preserve its URL
  allowlist and separate environment. Do not silently trust every discovered agent.
- Keep the login-code file and browser address visible at startup. The developer
  should not need to search a README to enter the simulator.
- Add focused status/diagnostic output before adding more flags. Prefer a small
  local scenario file when configuration exceeds a few meaningful options.

The existing batch scripts are useful and should remain as familiar Windows entry
points. Their current rmprep-specific behavior is appropriate for the included demo;
a general workspace launcher should be a separate capability.

### Reduce repeated composition without hiding behavior

The service entry points repeat ownership and shutdown code. A reusable helper can
own infrastructure setup while accepting `build_graph`, `respond`, and optional
inspection/outcome providers. Test startup failure, cancellation, draining, and
cleanup once at this shared boundary, with a generated-agent test proving the
template uses it correctly.

Keep `agent_app`, `ServiceContainer`, and existing `build_app` signatures available.
Avoid a large inherited agent class: business graphs should still be ordinary
LangGraph graphs, and test doubles should still be supplied through interfaces.
Likewise, do not combine durable audit and best-effort tracing behind one error
policy; their reliability requirements differ.

### Make context policy easier to reason about

rmprep already separates routing, selection, bounded model input, evidence checks,
and memo artifacts. Keep those responsibilities explicit. The remaining complexity
comes from legacy state projections and language heuristics spread across stages.

Use a normalized request/selection value to carry intent, client set, evidence date,
memo references, and retrieval mode. Document which stage may change each field.
Keep a compact invariant table next to regression cases: switching clients removes
unrelated evidence, historical references select the intended version, tool calls
have their results, and a long history preserves current evidence within the model
budget. Increasing the budget alone does not establish these invariants.

### Naming and documentation conventions

Most public names already describe responsibilities: `ContextBuilder`,
`QuestionAnswerAdapter`, `OutcomeJournal`, and `LangfuseTracing` are good examples.
Prefer targeted cleanup to renaming public modules:

- Change the private `_ToolSpans` parameter/attribute named `trial` to `tracing`.
  Keep the public rmprep `LangfuseTrial` compatibility shim until a planned migration.
- Use **Agent Lab** for the product and `ai-agent-lib-ui` for the package. Use
  **thread**, **turn/request**, **model call**, and **trace** distinctly in UI text
  and documentation; they have different identifiers and lifetimes.
- Reserve “accepted answer” for the final committed result. Call provisional text
  a draft, including when business checks may still revise it.
- Document ownership, cancellation, failure behavior, and scope on interfaces.
  Keep implementation history in design records, rather than comments that make
  current behavior sound like a temporary experiment.
- Reduce `Any` at shared graph/response boundaries when extracting composition.
  Add small protocols for the operations actually used, rather than mirroring an
  entire framework API or creating abstractions for every helper function.

## Documentation improvements applied in this pass

- Added a [documentation map](../README.md), code ownership map, request flow, and
  state ownership table. It distinguishes current setup guides from design proposals.
- Put Agent Lab's setup, login-code location, and agent-selection path near the top
  of the root README. Removed the duplicated feature narrative at the bottom.
- Put Agent Lab's setup steps ahead of detailed feature descriptions in its README.
  Removed stale phase language from its everyday instructions.
- Explained how the library, MCP services, Agent Lab, checkpoints, model context,
  and optional Langfuse fit together in the developer guide.
- Clarified that the generated `tutorial` workspace and the checked-in rmprep batch
  scenario are different starting points. Updated generated README instructions
  to keep the UI environment separate.
- Corrected the root README's streaming description: negotiated `/invoke` supports
  model drafts; `/invoke/stream` is the separate graph-step route.
- Replaced incomplete CLI path placeholders with a runnable Command Prompt example;
  updated package installation examples to use Python/pip and the local checkout.
- Made `update --diff` write behavior explicit. Moved the local startup guide's
  response internals after the first login and question.

## Validation and limits

The synthetic review probes used generated temporary projects, an in-memory trace
exporter, scoped graph checkpoints, and a local SQLite outcome journal. They made
no external model calls and used no business records or credentials.

Checks run for this pass:

| Check | Result |
| --- | --- |
| Architecture/docs, CLI diagnostics/generated workspaces, core live invocation/outcomes/inspection/Langfuse | 139 passed; 2 platform signal tests skipped; 2 opt-in integration tests deselected |
| Full Agent Lab regression suite | 196 passed |
| Main strict type checking | 376 source files passed |
| Agent Lab strict type checking | 47 source files passed |
| Import-linter | 11 contracts kept, 0 broken |
| Agent Lab Ruff lint and format checks; pip dependency check | Passed |
| Repository Ruff lint and format checks | Passed; normalized the Langfuse guide's Python example |
| Developer guide generation and freshness check | Passed |
| Local documentation links and anchors | 276 checked across 11 documents; all resolve |

The table above records the initial review checks. The subsequent fixes add
regressions in `test_http_admission.py`, `test_langfuse.py`, and
`test_cli_diagnostics.py`. They cover actual scoped graph histories, all HTTP
route combinations, cancellation/send failures, stalled/failed history reads,
configuration precedence, and successful/failing CLI secret masking.
Live Anthropic/Langfuse calls, distributed coordination, and AWS deployment
validation remain outside this offline fix pass.

### Verification after the fixes

| Check | Result |
| --- | --- |
| Full default library, AWS, CLI, example and architecture suite | 2,315 passed; 17 platform/capability skips; 36 opt-in tests deselected |
| Full Agent Lab suite in its separate environment | 197 passed, including rejection of a busy thread without replay |
| Strict type checking | Main: 379 source files; UI: 47 source files passed |
| Ruff lint and formatting | Repository and UI passed |
| Import-linter | All 11 contracts kept |
| Pip dependency checks | Library and UI environments passed |
| Generated developer guide | Rebuilt and freshness check passed |
| Local documentation links | 280 links across 11 documents passed |

The full run also exposed a stale generated-file inventory expectation, which now
includes `docs/langfuse.md`. Streaming cleanup tests cover ASGI 2.0 and 2.4 so that
generator cleanup happens in its originating task before thread ownership ends.

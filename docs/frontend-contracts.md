# Agent Lab contracts: Phase 1

Agent Lab is the application supplied by the `ai-agent-lib-ui` package. Phase 1
provides the contracts shared by future
chat, document and inspector screens. Phase 2 adds the local browser server,
HTTP dispatcher, SQLite history and `init`, `check`, `serve` commands. Start with the
[package setup](../packages/ai-agent-lib-ui/README.md) to install and run its checks
using Command Prompt, Python and pip.

## Package boundaries

The UI uses core contracts for identity, scope and diagnostic metadata. It talks
to agents through HTTP. Each agent continues to own its graph, tools, policy and
checkpoints. An agent or AWS service never needs to install the UI package.

```text
UI contracts and application rules
        |
        +-- injected catalog, HTTP, conversation, artifact and diagnostic ports
        |       (concrete adapters arrive in later phases)
        |
        +-- core contracts: identity, scope, diagnostic records

Agent HTTP service -> governed model -> optional metadata sink
                                     -> separately enabled raw model trace
```

The repository installer skips the UI package, and the UI has a dedicated CI job.
Importing UI contracts does not load LangGraph, LangChain, MCP, or a web framework.
Core's base distribution currently includes graph dependencies in its installation;
separating those dependencies into an extra is a different packaging change.
Core's existing top-level runtime exports load on demand, with the same names and
behavior. The registry schema and `/invoke` envelope are unchanged.

## Describe a registered agent

A presentation manifest describes how a registered agent appears in chat. It
does not grant permission or replace the registry. The endpoint is taken from
the server-owned registry and checked against an exact server-configured allowlist.
Production destinations require HTTPS. Explicit local mode permits HTTP on
`127.0.0.1` or `::1`.

```json
{
  "schema": "agentcraft2.presentation/v1",
  "agent_id": "rmprep",
  "agent_version": "0.1.0",
  "registry_revision": "revision-1",
  "title": "Relationship manager preparation",
  "adapter": "question-answer/v1",
  "api_audience": "api://rmprep",
  "required_scopes": ["agent.invoke"],
  "capabilities": ["artifacts", "checkpoints", "model_traces"]
}
```

Use `read_manifest` and `bind_agent` from
`ai_agent_lib_ui.application.catalog`. Binding checks the agent ID, version,
registry revision, adapter and destination. Rebind against the latest registry
before dispatch. Authentication, audience-specific credentials and policy checks
will be supplied by the HTTP/catalog adapters; the manifest never authorizes a call.
Transport adapters must disable redirects and environment proxies and use the
deployment's egress controls. URL validation alone is not DNS-rebinding protection.

Unknown manifest schema versions and adapters are errors. Unknown **optional**
capabilities return `unsupported`, which a UI should display as “Not supported by
this agent.” They do not prevent ordinary chat.

## Map a chat turn

`QuestionAnswerAdapter` is a pure mapper. Give it the verified `ViewContext`, a
`TurnAdmission`, and a manifest. Its request contains only the new question:

```json
{"input": {"question": "Explain the cash flow section."}, "thread_id": "thread-1"}
```

The agent restores its conversation from checkpoints. The UI never sends its
entire displayed transcript. A successful terminal response has the existing shape:

```json
{
  "output": {"answer": "Here is the explanation."},
  "thread_id": "thread-1",
  "request_id": "request-2"
}
```

The adapter checks the thread and validates the response before accepting its
answer. Graph progress events and raw model responses are not terminal answers.
The future HTTP client must enforce the byte limit while reading the response;
the mapper checks it again before decoding.

Optional `output.artifacts` are read only if the manifest advertises `artifacts`.
Each `agentcraft2.artifact/v1` version has its own ID, a stable document ID, a version
number, title, media type, timestamp, thread and request. It contains exactly one
of inline content or an opaque content reference. A reference cannot be a local
path or a URL. The verified invocation supplies tenant, subject and agent scope.
Malformed optional artifacts produce `unavailable` and an `invalid_artifacts`
warning while preserving the accepted answer. The local UI stores inline versions
atomically with completed turns and renders Markdown/plain text in its optional
memo panel. Content reads enforce thread ownership and `ui.artifact.read`. An
existing ID or document/version pair keeps its first saved content. External
content references are not resolved by the local panel. Optional presentation
capability changes preserve existing chat bindings; endpoint, version, adapter,
audience and scope changes still require a new binding.

See the executable synthetic [manifest](../packages/ai-agent-lib-ui/tests/fixtures/presentation.json)
and [memo response](../packages/ai-agent-lib-ui/tests/fixtures/invoke.json) fixtures.

## Scope, permissions and query limits

Every thread, request and document reference carries the agent's full
`Scope(tenant, subject, application)`. `ViewContext` checks that the caller owns
that scope. This is a consistency check: the server still authenticates the caller
and authorizes the action on every read or write. Cross-user support access is
not enabled by these contracts.

| Rule | Contract |
| --- | --- |
| Catalog and chat access | Separate `ui.catalog.read` and `ui.agent.invoke` actions. |
| Conversations | Separate read/write actions; one selected agent per thread. |
| Documents | `ui.artifact.read`; immutable versions qualified by scope and thread. |
| Inspector | Separate diagnostic metadata and content permissions. |
| Unscoped service logs | Separate `ui.service-log.read`; no request-ID-only joins to conversations. |
| Pagination | Default 50, maximum 200 summaries; opaque cursors bound to full scope, resource and query filters. |
| Content preview | Maximum 64 KiB of UTF-8 text, with an explicit truncation flag. |
| Artifact content | Default 256 KiB inline, configurable up to 1 MiB. |
| Question / response | Defaults 64 KiB / 1 MiB; the manifest defines bounded limits. |
| Errors | Stable code and request ID; missing and unauthorized object IDs both use `not_found`. |
| Availability | `available`, `unsupported`, `disabled`, `pending`, `expired`, `unavailable`. |

The ports in `contracts/ports.py` describe these requirements for adapters.
Phase 2 adds local authorization, signed history cursors, SQLite queries and
parent sessions. Additive ports in `contracts/chat.py` preserve the original
interfaces and keep the application independent of the browser integration.
Checkpoint ports expose a scoped agent diagnostic API, never an unrestricted
checkpoint database connection or an object deserializer.

The local per-response inspector uses the additive `TurnInspector` port and
`agentcraft2.turn-inspection/v1` envelope. An agent opts in with
`agent_app(inspector=...)`, exposing `GET /requests/{id}/inspection?thread_id=...`.
The reader authorizes diagnostic content independently of agent invocation. The UI
resolves the stored run's request and thread IDs after checking ownership, then
checks the returned tenant, subject, application, thread, request and schema.
It disables redirects, bounds responses to 512 KiB and limits reads to ten seconds.
The four sections are LLM inputs, LLM outputs, MCP call audit records and decoded
parent-graph checkpoint snapshots. Each carries availability, record count and an
explicit truncation flag, with at most 64 KiB of text. Diagnostic content is escaped.

`LocalTurnInspector` reads explicitly configured private JSONL files using all
five correlation fields. It does not infer actual MCP execution from a model's
proposed tool call. `GraphCheckpointPreview` uses the agent's scoped graph API
and filters `agentcraft2_request_id` metadata added by `services.invocation()`.
Historical checkpoints without that metadata remain unavailable per turn. This
preview does not enumerate subgraph checkpoint namespaces. No checkpoint state is
deserialized in the UI, and diagnostic reads never invoke/resume the graph.

## Capture usage without storing prompts

The additive core contracts are:

| Schema | Meaning |
| --- | --- |
| `agentcraft2.model-usage/v1` | One observed SDK attempt, with usage, monotonic duration and explicit links. |
| `agentcraft2.operation-decision/v1` | Pipeline or application acceptance/rejection of a specific model attempt. |
| `agentcraft2.operation-summary/v1` | Terminal application outcome and the expected attempt references. |
| `agentcraft2.request-outcome/v1` | Agent journal state, including an honest `unknown` outcome after uncertainty. |

`DiagnosticSink` is a small async interface. Supply a sink and inject one recorder
into both the container and HTTP entry point. For example, in the agent's
composition root, where `config`, `run`, `lifecycle` and `metadata_sink` already exist:

```python
from ai_agent_lib_core.adapters.system import SystemClock, UuidGenerator
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.http import agent_app
from ai_agent_lib_core.observability import Diagnostics

diagnostics = Diagnostics(metadata_sink, SystemClock(), UuidGenerator())
services = ServiceContainer(config, diagnostics=diagnostics)
# Start/close services through your existing service lifecycle.
app = agent_app(
    services,
    run,
    application="rmprep",
    lifecycle=lifecycle,
    diagnostics=diagnostics,
)
```

The caller owns the sink's lifetime. Phase 1 supplies the interface and capture
logic; production storage, indexing and diagnostic routes arrive in later phases.
For a direct Python invocation, wrap the complete agent run with
`async with diagnostics.operation(context)`. Do not also wrap a run already
covered by `agent_app`, and await all model work before returning.

Metadata capture is independent of `AC2_MODEL_TRACE_PATH`. Its default is off
unless a recorder is injected. It stores no prompts, responses, tool arguments,
provider exception text or credentials. The existing `agentcraft2.model-trace/v1`
records and audit format remain unchanged. When both captures are enabled, they
share model call IDs and attempt numbers.

Each SDK invocation gets a distinct attempt number under a stable call ID. Tokens
are captured as soon as the provider returns, before output validation. A rejected
draft still spent tokens. An SDK failure with no usage has null counts; SDK-internal
retries remain unobservable. Fake-provider usage is labelled `simulated`.

LangChain's normalized input count includes cache reads and writes already. Raw
Anthropic input excludes them, so the standalone normalizer adds those categories
once when all counts are present. Anthropic's cache-duration breakdown is handled
without counting its total a second time. Missing cache data remains unknown,
while an explicitly reported zero remains zero. Only allowlisted numeric usage
fields are preserved for inspection.

The boundary captures model-emitted tool-call IDs and available LangGraph node
metadata. A configured checkpoint ID describes the input checkpoint, not a later
checkpoint created by the graph. Trusted agent code can use `bind_diagnostic_links`
to supply known links explicitly. A pipeline `passed` decision means its checks
passed; business code may still reject the draft. Business validation can record
an explicit application decision with `Diagnostics.decide` and an `AttemptRef`.

Only the terminal invocation output is accepted as the chat answer. The summary's
`accepted_output: "invoke.output"` refers to that application return, never the
last model response. Streaming progress currently has no accepted-output mapping,
so the new `agent_app` option summarizes `/invoke` only.

Consumers deduplicate attempts by full scope, request ID, call ID and attempt
number. Compare received attempts with the terminal summary. A missing summary
means completeness is unknown. `capture_complete` describes record capture; it
does not claim that the provider reported every token count. A diagnostic sink
failure preserves chat and marks capture incomplete when a summary can be written.
Writes have a one-second timeout by default, configurable through
`Diagnostics(write_timeout_seconds=...)`, so an unresponsive async sink cannot
hold a chat request indefinitely. Sinks must use nonblocking, cancellation-aware IO.

The [diagnostic fixture](../packages/ai-agent-lib-ui/tests/fixtures/diagnostics.json)
shows a 120-token draft rejected by application validation, followed by an SDK
failure whose usage is unavailable. Both belong to the terminal attempt list;
neither becomes an accepted answer. Phase 3 will implement metric aggregation,
including partial totals and a cache-read ratio only when the counts are complete.

## Represent uncertain request outcomes

`TurnAdmission` defines the UI's future atomic admission contract. Persist the
idempotency key and question before dispatch. The same key and content must return
the existing run. Different content for the same key is a conflict. Admit only one
active turn per scoped thread, including across replicas.

Run states are `accepted`, `running`, `completed`, `failed`, and `unknown`. A timeout
after dispatch does not prove that tools did not run. An unknown run holds its
admission and can move to a terminal state only after authoritative reconciliation;
`require_transition` enforces this rule. Do not replay it automatically.

`RequestOutcomeJournal` defines an agent-side compare-and-set port. It is distinct
from checkpoints, and Phase 1 does not claim atomic checkpoint/journal commits or
durable agent recovery. Phase 2 implements UI admission storage and conservative
unknown-outcome recovery; the agent's
full diagnostic inspector belongs to Phase 4. Local agents can now inject an
`OutcomeJournal`: `GET /requests/{request_id}?thread_id=...` returns the confirmed
terminal envelope to the same authenticated authority, or `{"state":"unknown"}`.
The UI only reconciles a matching terminal result and audits that transition;
missing status never permits replay. JSON and SSE answers both validate the
original request and thread IDs. Thread history has bounded pages with signed,
owner- and thread-bound cursors. Unrelated registry revisions do not invalidate
an otherwise identical approved agent binding.

## Checks and remaining work

Tests cover serialized fixtures, legacy envelopes and registry entries, versions,
scope mismatches, stale bindings, destinations, artifact degradation, import
boundaries, usage normalization, discarded drafts, SDK retries, concurrent/sync
calls, diagnostic outages and terminal outcomes. The UI's isolated environment
also runs strict type checking and `pip check`.

The remaining Phase 0 gates are enterprise dependency/CSP approval and an actual
200% browser zoom check. Phase 2 adds adapters, durable local history,
runtime configuration and chat integration. These implementations do not remove
those acceptance gates or present the framework experiment as a production UI.

# Agent Lab: frontend implementation plan

Status: Phase 2 implemented for local development, updated 13 October 2026. This plan implements the
[frontend specification](frontend-specification.md). The optional package now
contains contracts, registry-backed chat, SQLite history and local runtime commands.
Live drafts, response metrics and per-response trace links are available.
The temporary draft disappears when the saved outcome is ready. Local turn
inspection remains available from Logs. The optional memo panel stores inline versions
and offers version selection. Shared deployment remains a later phase.
The package and CLI module names stay `ai-agent-lib-ui` and
`ai_agent_lib_ui`.

## Delivery approach

Build one working path first: choose rmprep, create a memo, ask a follow-up on the
same thread, and inspect the evidence that produced the answer. Keep the current
agent HTTP API stable. Add optional contracts and adapters, then broaden storage
and identity support. Existing agents should remain usable without adopting every
diagnostic feature.

Use small reviewable changes in the order below. Each phase ends with a working
demonstration and tests for the new behavior. The application layer remains
independent of the selected UI shell so a framework decision does not dictate
checkpoint, authorization, or persistence design.

## Phase 0: prove the framework fit

The [Phase 0 experiment and decision record](frontend-phase0-review.md) now
demonstrate the functional path with Chainlit 2.12.0. The decision is conditional:
actual 200% browser zoom and enterprise dependency/CSP approval remain open.
See the [sample setup](../experiments/chainlit-phase0/README.md) to repeat it.

Create a disposable integration sample using the candidate pinned Chainlit version
and a scripted HTTP agent. This is a time-boxed technical spike, not the production
package or a live-model test.

Verify:

- Python/pip installation in a clean Windows virtual environment using approved
  wheels; all browser assets are served locally and need no public CDN.
- Registry-backed chat profiles, persistent authenticated history, a side memo,
  an inline metrics element, and a full-width inspector link using supported hooks.
- The parent FastAPI identity/session applies to every Chainlit HTTP, WebSocket,
  history, element, and download route; forged identity headers are ineffective.
- A data-layer adapter can map to one application-owned thread store without
  leaking another subject's threads or duplicating the transcript.
- Reloading or reconnecting does not re-invoke the agent; a disconnected browser
  does not destroy the server-owned request task.
- Layout works at 390, 768, and 1440 pixels and 200% zoom, including wide JSON/tables.

Deliver a short decision record with tested package versions, dependency/license
inventory, screenshots, and the evidence for each item. If achieving these items
requires patching Chainlit internals or bypassing authorization, select React and
assistant-ui with the same Python backend. CI bundles its built assets in the wheel;
the agent developer's installation stays Python/pip. This is the only framework
selection gate; do not implement two full frontends.

## Phase 1: contracts and package boundaries

Implemented in [ai-agent-lib-ui](../packages/ai-agent-lib-ui/README.md) with additive
core metadata capture. The [contract guide](frontend-contracts.md) records the
schemas, examples, compatibility rules and scope of the implementation. The
package has its own Python environment and Windows/Linux CI job. Phase 2 now adds
runtime adapters, assets, migrations and `__main__.py` in the directories below.

Add the optional `packages/ai-agent-lib-ui` distribution and the smallest necessary
core diagnostic integration. Keep UI dependencies out of core and AWS consumers.
Prepare this structure, adjusting names only where established repository
conventions require it:

```text
packages/ai-agent-lib-ui/
  pyproject.toml
  src/ai_agent_lib_ui/
    contracts/       # typed values and replaceable ports
    application/     # catalog, conversations, inspector, metrics
    adapters/        # HTTP agents, storage, identity, diagnostic readers
    integrations/    # Chainlit and FastAPI boundary code
    config/          # validated options and environment bindings
    assets/          # packaged UI elements/styles/pages
    migrations/      # UI-owned schema changes
    __main__.py      # init, check, serve
  tests/
```

Define the presentation manifest, `question-answer/v1` adapter, thread/run DTOs,
artifact references, pagination/error semantics, and permission actions. Preserve
the existing registry schema and `/invoke` response. Unknown optional capabilities
must degrade to a clear message, not break chat.

Define usage and operation event schemas with call/attempt IDs even when content
tracing is disabled. Extend framework-boundary capture with cache counts and
checkpoint/tool/validation links where available. Specify how to identify an
accepted final answer, and retain old trace/audit readers. Separate content capture
from metadata so ordinary metrics never require raw prompt storage.
Define an agent terminal operation summary for checking ingestion completeness,
and a request outcome journal that can represent crash uncertainty honestly.

Tests: serialized contract fixtures, legacy HTTP/registry compatibility, import
boundaries, invalid manifests/destinations, scope binding, unknown schema versions,
and provider usage normalization. Include a fixture for a rejected draft that
spent tokens and an SDK failure with unavailable usage.

Exit: reviewed schema examples and passing contract tests; no UI code reaches
agent internals or unscoped checkpoint storage.

## Phase 2: local chat with durable threads

Implemented in the [UI package](../packages/ai-agent-lib-ui/README.md): the `chat`
optional extra, supported Chainlit hooks, file-registry pins, local sessions and
SQLite storage. One server owns a state folder and one configured static identity;
UI and agent endpoints use loopback. Agent checkpoints and `/invoke` remain unchanged.

Admission is persisted before HTTP. Server-owned tasks survive browser cancellation;
accepted output is committed atomically and unknown runs remain held without replay.
Registry changes require explicit pin refresh. Existing threads retain their
original binding. The Conversations page provides rename, title search, archive and
restore. Metrics and the diagnostic inspector follow in later phases.

Validated locally on Windows/Python 3.11: 66 UI checks, 2,082 library checks
(17 platform skips and 36 opt-in checks deselected), four installed-wheel HTTP/
Socket.IO checks, strict type checks, lint, import boundaries, dependency checks
and guide generation. Browser checks covered sign-in, two questions, refresh,
history restoration, rename, title search, archive and restore. The chat header
also fits a 390-pixel viewport. Actual 200% browser zoom and enterprise approval
remain the Phase 0 release gates. CI runs the UI checks on Windows and Linux.

Implement the catalog and HTTP agent adapters, local identity, SQLite thread/run
store, and supported UI shell integration. Add create, rename, search, archive,
and resume. Persist the selected agent and accepted transcript in one store.
Send only the new question to the agent. Support `question-answer/v1` first.

Persist turn admission and idempotency before sending the HTTP request. Implement
one active turn per scoped thread, duplicate-send handling, disconnected-browser
reattachment, and safe unknown-outcome behavior. Start with `/invoke`; add step
progress only after its final-answer mapping is explicit and tested.

Provide `init`, `check`, and `serve` commands with useful configuration errors.
Keep local identity on loopback and use the existing registry/provider settings
through explicit configuration translation. Do not launch registered services or
load arbitrary Python paths from browser input.

Tests: two questions on one thread, refresh/restart, two different users, denied
agent, missing endpoint, changed registry version, double send, simultaneous
same-thread requests, timeout after dispatch, and failed persistence. Use scripted
HTTP endpoints and local files; run on Windows and Linux.

Exit: an engineer can install the wheel, select a registered agent, chat, restart
the UI, and continue the same conversation using documented Command Prompt steps.

## Phase 3: documents and response metrics

Local inline documents, the optional memo panel, version selection, and response
metrics are implemented. rmprep emits accepted memo artifacts without changing
`output.answer`. The panel uses Chainlit's supported `custom_js` entry point,
owns its DOM outside the chat root, and reads authorized application routes.
It does not access Chainlit's internal React state or require a JavaScript build.
Downloads and external content-reference readers remain future work.

Add immutable artifacts and local content storage. Extend rmprep with an optional
artifact emission path for accepted memos while preserving `output.answer`.
Q&A responses keep the memo open; a new memo produces a new version. Add source
links, bounded Markdown/plain-text rendering, version selection, and downloads.

Implement the metrics aggregator over canonical attempt records and attach its
summary to the accepted turn. Show input/output/total tokens, prompt cache read
percentage, response time, and completeness. Provide per-call expansion. Do not
read all raw traces merely to paint a metrics footer.

Tests: artifact ownership, path traversal and unsafe link/markup handling,
version immutability, rejected drafts, missing usage, cache writes vs reads,
duplicate events, mixed models, retries, and overlapping call timing.

Exit: a memo opens beside chat; its follow-up uses the same thread; metrics match
fixture calculations and remain available when content capture is disabled.

## Phase 4: checkpoint, model, MCP, and log inspector

Add opt-in authenticated diagnostic routes to the agent integration, supplied
with a checkpoint reader by the service composition root. Expose summaries and
bounded decoded state through the existing scoped APIs. Keep routes disabled
unless configured, and authorize diagnostic content separately from chat.
Add agent-side same-thread admission for deployments with other API clients;
test a direct caller racing a UI turn. Preserve public request/response shapes.

Implement incremental JSONL indexing with rotation/incomplete-line handling and
idempotent event ingestion. Record capture status and indexing watermarks. Build
the inspector views with lazy payload loading, filters, pagination, and a shared
request timeline. Link tool IDs, model attempts, graph steps, and checkpoints
only when explicit metadata establishes the connection.

Create a small deterministic rmprep fixture with a tool response reused as the
next model input, a same-thread follow-up, and a discarded draft. Use synthetic
or approved redacted data in committed test fixtures; keep private live-run
artifacts outside distributable wheels and screenshots.

Tests: object-level access for every endpoint, forged IDs/cursors, expired and
late content, disabled capture, duplicate/rotated JSONL, corrupt records, payload
limits, service-log filtering, and a diagnostic outage that leaves chat usable.
Verify that no trace viewer path executes tools or deserializes arbitrary objects.

Exit: engineers can explain the memo's evidence path and see why checkpoint
messages, model exchanges, and accepted chat history are different views.
Phases 0–4 form the first complete local release.

## Phase 5: AWS identity, persistence, and deployment

Add PostgreSQL implementations for UI-owned records and S3 artifact/diagnostic
storage. Build an indexed ingestion path for agent and MCP records; object storage
is the durable content source, not the interactive query engine. Keep migration
ownership separate from agent checkpoint schemas. Continue using the current S3
registry provider; implement a PostgreSQL `RegistrySource` independently if the
team wants that migration now.

Implement Entra server sign-in, sessions, agent-audience token acquisition/refresh,
and policy-based catalog/diagnostic access. Reuse and test existing agent-to-MCP
OBO behavior. Document app registrations and role mapping per resource. Validate
the same tenant/object identity across BFF, agent, MCP, and checkpoint reads.

Prepare the Fargate deployment with HTTPS, WebSockets, required affinity, session
secrets, approved destination rules, telemetry, health/readiness, and shutdown
draining. Use shared run admission/concurrency state. Simulate browser disconnect,
rolling replacement, agent failure, expired sessions, revoked permissions, and an
ambiguous in-flight outcome. No automatic replay of possibly executed tool calls.

Configure encryption, access controls, retention, and purge/reconciliation jobs for
UI records and their content indexes. Preserve audit retention separately from a
user archiving a conversation. Test diagnostic-read audit records without storing
the content being read in those audit records.

Tests: opt-in PostgreSQL/S3/Entra integration on suitable CI hosts, two UI replicas,
cross-tenant denials, service-role permissions, credential rotation, schema
upgrade/rollback procedure, ingestion lag, and bounded queries over a large history.

Exit: the complete workflow passes in the enterprise environment. Publish the
remaining limitation that unknown cross-service outcomes require reconciliation;
do not advertise exactly-once execution or automatic recovery of a lost agent run.

## Phase 6: release and developer walkthrough

Add the UI extra to the CLI's optional installation flow and update new workspace
templates with the UI setup, keeping agent/MCP-only installations unchanged.
Document all UI variables in one generated table, consistent with the existing
guide's local/AWS format. Pin a supported framework range and tested dependency
set; mirror wheels/assets through the enterprise package process.

Publish a short user walkthrough and an engineer walkthrough. The latter uses
rmprep to show a memo, follow-up, metrics, tool result becoming model input,
checkpoint comparison, and a rejected draft. Run deterministic fixtures in CI.
A live Anthropic verification is opt-in, uses the approved configured service,
and records model/version/run metadata without committing client evidence or
credentials. Do not reuse private traces as public sample data.

Release checks:

- Clean wheel installation on a restricted Windows account through the approved
  package index; packaged assets and all proposed commands work.
- Core/CLI/AWS compatibility tests and import boundaries still pass.
- Browser accessibility, three-panel layout, scrolling, and reconnect tests pass.
- Authorization tests cover chat, history, diagnostics, documents, downloads,
  WebSocket actions, and every alternate Chainlit access path.
- Installation instructions use Command Prompt, Python, and pip; missing optional
  capabilities and deployment limitations are explained with actionable messages.

## Planning estimates and dependencies

These are initial engineering estimates, not delivery commitments. They assume
one engineer familiar with the library plus part-time UI and platform review.
Enterprise provisioning and approval lead times are additional.

| Phase | Approximate effort | Main dependency or uncertainty |
| --- | --- | --- |
| 0. Framework trial | 2–3 engineer-days | Supported auth/layout hooks and approved wheels |
| 1. Contracts | 3–5 engineer-days | Stable usage/correlation and checkpoint projection design |
| 2. Local chat | 4–6 engineer-days | Thread-store integration and reconnect semantics |
| 3. Documents/metrics | 3–5 engineer-days | rmprep artifact contract and provider usage fixtures |
| 4. Inspector | 5–8 engineer-days | Diagnostic permissions, correlation, and indexing |
| 5. AWS/Entra | 6–10 engineer-days | App registrations, Fargate/PostgreSQL/S3 access, multi-replica tests |
| 6. Release/docs | 2–4 engineer-days | Clean consumer and browser-test environments |

Total: approximately 25–41 engineer-days. Re-estimate after Phase 0 and after the
contract review. A switch to a custom React shell adds UI implementation work;
it should not require redesigning the application services.

Deployment inputs to resolve during implementation: approved dependency versions,
Entra app registrations and scopes, diagnostic access policy, retention periods,
expected concurrent users/history size, and ownership of the diagnostic ingestion
service. Local development can proceed using the explicit defaults in the spec.
The AWS release requires those deployment values and measured load targets.

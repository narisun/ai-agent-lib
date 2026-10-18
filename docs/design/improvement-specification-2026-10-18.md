# Improvement specification — 18 October 2026

This specification turns the findings of the
[enterprise architecture review](enterprise-architecture-review-2026-10-18.md)
into requirements a coding agent can implement. It extends the
[October specification](../Specification.md) and changes none of its design
principles; every requirement here is expressible inside the existing layering
and import rules. The implementation plan lives at
`docs/superpowers/plans/2026-10-18-enterprise-review-improvements.md`.

## Scope

**In scope:** offline evaluation depth (R1), online evaluation (R2), HTTP
service composition (R3), quality gates (R4), developer experience (R5), and
two small structural items (R6.2, R6.3).

**Out of scope, deferred to their own plans:** the CLI package regrouping
(R6.1) and the `agentlib lab` workspace launcher. Both are recorded here so
the decision is traceable, but they are independent subsystems and should not
ride along with this work.

**Non-goals.** No new DI framework, no agent base class, no change to the
fixed interceptor orders, no new runtime dependency in `ai-agent-lib-core`'s
mandatory set, no breaking change to any public API or `EAP_` variable, and no
online scoring on the request path — capture is on-path and append-only;
scoring is always a separate process.

## Design principles that bind every requirement

These are the existing rules of the library, restated because every task must
honor them:

1. `contracts` imports nothing else from the library; `config`, `pipeline`,
   `adapters` import `contracts` only; the request path never imports
   `evaluation` (enforced by import-linter).
2. Only `config` reads the environment; every other module receives typed,
   frozen options.
3. Constructor injection only; time and identifiers come from injected
   `Clock`/`IdGenerator`; determinism in tests.
4. New record formats carry a versioned `schema` field, like
   `agentlib.decision/v1` and `agentlib.registry/v1`.
5. Fail closed; typed errors from the existing taxonomy (`ValidationFailed`,
   `ConfigurationError`, …).
6. Strict mypy, ruff (Google docstrings, line length 100), async-first,
   frozen dataclasses with `slots=True` for value types.
7. Local-first: everything below runs with pip-installed packages, no Docker,
   no network in unit tests.

---

## R1 — Offline evaluation depth

### R1.1 Versioned report with provenance

`EvalReport.to_json()` gains a `"schema": "agentlib.eval-report/v1"` field and
a `"metadata"` object. `run_eval` accepts `metadata: Mapping[str, str] | None`
and carries it onto the report. Metadata is opaque to the library; the
generated template records at least `model` (provider:id), `dataset`
(digest), and `application`.

**Acceptance:** a report round-trips through JSON with its schema, metadata,
means, and per-case scores intact; an old-format report (no `schema`) is still
loadable by R1.3's loader.

### R1.2 Dataset digest and per-case timeout

- `evaluation.cases.dataset_digest(path: Path) -> str` returns
  `"sha256:" + <64 hex digits>` of the file bytes.
- `run_eval(..., case_timeout: float | None = None)` bounds each case's task
  with `asyncio.wait_for`. A timed-out case scores 0 on every metric with
  `error_type == "TimeoutError"`; the run continues. `None` keeps today's
  unbounded behavior.

**Acceptance:** a task that never returns, run with `case_timeout=0.05`,
produces a completed report in under a second with that case failed and other
cases scored.

### R1.3 Baselines and regression gating

New module `ai_agent_lib_core/evaluation/baseline.py`:

- `SavedReport` (frozen): `means: Mapping[str, float]`,
  `case_scores: Mapping[str, Mapping[str, float]]` (case id → metric → value),
  `metadata: Mapping[str, str]`.
- `load_report(path: Path) -> SavedReport` reads a v1 report (and tolerates a
  pre-v1 report that lacks `schema` and `metadata`). A file that is not a
  report raises `ValidationFailed` naming the path.
- `regressions(current: EvalReport, baseline: SavedReport, *,
  tolerance: float = 0.0) -> list[Regression]` where `Regression` (frozen)
  has `metric: str`, `baseline: float`, `current: float`,
  `cases: tuple[str, ...]` (ids present in both runs whose score for that
  metric dropped by more than `tolerance`). A metric is regressed when its
  mean drops by more than `tolerance`. Metrics only in one report are ignored.
- `require_no_regression(current, baseline, *, tolerance=0.0) -> None` raises
  `ValidationFailed` listing each regressed metric with both means.

**Acceptance:** improved and equal runs pass; a mean drop greater than the
tolerance fails and names the metric, both means, and the dropped case ids.

### R1.4 Judge calibration

New module `ai_agent_lib_core/evaluation/calibration.py`:

- `LabeledCase` (frozen): `case: EvalCase`, `output: Any`,
  `human: float` (0–1; validated like `Score`).
- `calibrate(scorer: Scorer, labeled: Sequence[LabeledCase], *,
  within: float = 0.25) -> CalibrationReport` runs the scorer on each labeled
  output. `CalibrationReport` (frozen): `cases: int`,
  `mean_absolute_error: float`, `agreement: float` (fraction with
  `abs(judge - human) <= within`),
  `disagreements: tuple[tuple[str, float, float], ...]` (case id, human,
  judge; only pairs outside `within`), and
  `summary() -> list[str]` for terminal output.

**Acceptance:** a perfect judge yields MAE 0.0 and agreement 1.0; a judge
that inverts the human grades reports every case in `disagreements`.

### R1.5 CLI and template integration

- `agentlib eval SERVICE --accept` copies the service's
  `.agentlib/eval-report.json` to `evals/baseline.json` after the evals pass
  (and errors with a fix message when no report exists). `--accept` without
  `SERVICE` is an error.
- The generated eval test (template
  `tests/test___package___eval.py.jinja`) passes `metadata` (model, dataset
  digest, application) and `case_timeout=120.0` to `run_eval`, and when
  `evals/baseline.json` exists calls
  `require_no_regression(report, load_report(baseline), tolerance=0.05)`.
- New exports from `ai_agent_lib_core.evaluation`: `dataset_digest`,
  `SavedReport`, `load_report`, `Regression`, `regressions`,
  `require_no_regression`, `LabeledCase`, `CalibrationReport`, `calibrate`.

**Acceptance:** `agentlib new agent` output passes `agentlib check`; the CLI
generated-workspace tests cover `--accept` and the baseline-comparing test.

## R2 — Online evaluation

Capture on the request path is append-only writes of versioned records;
scoring always happens in a separate process using the same `Scorer` machinery
as offline evals. No request-path module imports `evaluation` (the existing
import contract already enforces this; feedback capture lives in the UI
package, which may import `evaluation`).

### R2.1 Feedback records

New module `ai_agent_lib_core/evaluation/feedback.py`:

- `FEEDBACK_SCHEMA = "agentlib.feedback/v1"`.
- `FeedbackRecord` (frozen): `request_id: str`, `thread_id: str`,
  `application: str`, `subject: str` (who judged), `verdict:
  Literal["up", "down"]`, `comment: str = ""`, `created_at: str = ""`
  (ISO 8601, supplied by the caller's clock — the record never reads a clock).
- `JsonlFeedbackLog(path)` with `async def write(record: FeedbackRecord) ->
  None`: one JSON object per line including the schema field, serialized
  writes, file created on first write (same pattern as
  `JsonlModelTraceSink`).
- `load_feedback(path: Path) -> list[FeedbackRecord]`; a line with the wrong
  schema or shape raises `ValidationFailed` naming the line number.

### R2.2 Interaction records, sampling, and scoring

New module `ai_agent_lib_core/evaluation/online.py`:

- `INTERACTION_SCHEMA = "agentlib.interaction/v1"`.
- `Interaction` (frozen): `request_id: str`, `thread_id: str`,
  `question: str`, `answer: str`, `application: str = ""`,
  `verdict: str = ""` (empty, `"up"`, or `"down"` when feedback was joined).
- `write_interactions(path, interactions)` and
  `load_interactions(path) -> list[Interaction]` (JSONL, same validation
  rules as R2.1).
- `sampled(interactions: Sequence[Interaction], *, rate: float,
  salt: str = "") -> list[Interaction]`: deterministic — an interaction is
  chosen when `int(sha256(salt + request_id).hexdigest()[:8], 16) / 0xFFFFFFFF
  < rate`. Same inputs, same sample, on every machine.
- `score_interactions(interactions, scorers, *, concurrency=4,
  case_timeout=None, metadata=None) -> EvalReport`: builds an `EvalCase` per
  interaction (`id=request_id`, `input={"question": ...}`,
  `expected={}`, tag `"up"`/`"down"` when a verdict is present) and runs
  `run_eval` with a task that returns the recorded `answer` — the scorers
  (typically `judged`) do any model calls. Reuses R1's report, metadata, and
  baseline machinery unchanged, so online drift gates with
  `require_no_regression` like offline runs.

**Acceptance:** sampling is reproducible and respects the rate on a known id
set; scoring 3 recorded interactions with a stub judge produces a v1 report
whose per-case ids are the request ids; `report.by_tag("down")` isolates the
user-rejected answers.

### R2.3 Agent Lab feedback capture

In `ai-agent-lib-ui`:

- A `FeedbackLog` protocol in `contracts/` (`async def write(record:
  FeedbackRecord) -> None`) so the application layer stays framework- and
  file-free.
- The Chainlit data layer implements `upsert_feedback` instead of raising:
  it resolves the run whose answer step id matches the feedback's `forId`,
  builds a `FeedbackRecord` (verdict `"up"` for value 1, `"down"` for 0,
  comment preserved, subject = the signed-in user, created_at from the UI's
  clock), and appends it through the injected `FeedbackLog`.
  `delete_feedback` keeps returning falsy/404 (append-only evidence).
- The default wiring appends to `feedback.jsonl` beside the UI's existing
  state (same directory as `conversations.sqlite`).
- A feedback record for an unknown step id is refused (404) exactly as today.

**Acceptance:** UI tests drive `upsert_feedback` with a fake store and a
temp-file log and assert the written record's schema, verdict mapping, and
request/thread ids; unknown `forId` still yields 404; the UI import contracts
(R4.1) still pass.

### R2.4 Agent Lab interaction export

In `ai-agent-lib-ui`, a conversation-store–level exporter that turns saved
threads (admitted question, accepted answer, request/thread ids) into
`Interaction` records, joining feedback by `request_id` when a feedback log is
present, and writes them with `write_interactions`. Exposed as a documented
Python entry point (`ai_agent_lib_ui.adapters.interactions.export_interactions
(store, out_path, *, feedback_path=None) -> int` returning the record count);
a CLI wrapper may come later with `agentlib lab`.

**Acceptance:** UI tests export from a fake store with two threads and one
feedback line and assert the JSONL content and the returned count.

### R2.5 Evals guide

New `docs/evals.md` documenting the full loop: generated offline evals →
baseline accept → online capture (feedback + export) → sampled scoring →
regression gate; linked from `docs/README.md`'s task table. States the
privacy posture: interaction and feedback files contain conversation content;
keep them inside the service's `.agentlib/` state and treat like the model
trace (masked credentials, test data in development).

## R3 — HTTP service composition

### R3.1 `AgentService` and `run_agent`

New module `ai_agent_lib_core/integrations/http/composition.py` (inside the
existing `[serve]` extra surface):

- `read_question(given: object) -> str`: the generalized `_question` helper —
  a non-empty `"question"` string from a mapping, else `ValidationFailed`
  with the existing expected/actual message shape.
- `AgentService` (frozen dataclass): `application: str`,
  `build_graph: Callable[[ServiceContainer], Awaitable[Any]]` (performs its
  own MCP discovery), `respond: Callable[[Any, ServiceContainer,
  RequestContext, str], Awaitable[dict[str, Any]]]`,
  `question: Callable[[object], str] = read_question`,
  `stream: Callable[[Any, ServiceContainer], Stream] | None = None`,
  `live_updates: bool = True`, `default_port: int = 8000`,
  `app_kwargs: Mapping[str, object]` (defaults empty; forwarded verbatim to
  `agent_app` for diagnostics/outcomes/inspector/admission).
- `async def build_agent_app(service, services, lifecycle, *, tracing=None)
  -> Starlette`: registers graph preparation via `lifecycle.prepare`, wraps
  `respond` with the optional `LangfuseTracing`, and calls `agent_app` with
  the service's settings — the behavior of today's generated `build_app`.
- `def run_agent(service: AgentService, argv: Sequence[str] | None = None)
  -> int`: today's generated `main` — argparse (`--host`, `--port` defaulting
  to `service.default_port`), `load_service_config`, `configure_logging`,
  `start_telemetry`, `langfuse_tracing`, container lifecycle,
  `ServiceLifecycle`, `serve`, teardown, `report_error`. Returns the exit
  code.

Existing names (`agent_app`, `serve`, `ServiceLifecycle`, per-service
`build_app`) keep working; composition is additive.

**Acceptance:** core tests build an `AgentService` around a scripted
graph/respond, start it against the test transport used by the existing
serving tests, and verify `/healthz`, readiness gating on `prepare`,
`POST /invoke` happy path, and a `ValidationFailed` question → 4xx mapping;
`run_agent` returns non-zero and reports when configuration loading fails.

### R3.2 Migrations

- The agent template's `service.py.jinja` is rewritten on the helper; the
  generated file keeps `build_app` and `main` as thin delegations so existing
  callers and docs stay valid.
- `examples/accounts-agent/src/accounts_agent/service.py` and
  `examples/rmprep/agents/rmprep/src/rmprep/service.py` migrate; rmprep's
  inspection/outcome providers pass through `app_kwargs`.

**Acceptance:** generated-workspace CLI tests, accounts-agent service tests,
and the rmprep suite pass unchanged in behavior; each migrated `service.py`
contains no telemetry/logging/langfuse boilerplate.

## R4 — Quality gates

### R4.1 UI import contracts

`packages/ai-agent-lib-ui/pyproject.toml` gains import-linter contracts:
`contracts` imports nothing from the package; `application` imports neither
the UI `integrations`/`adapters` nor `chainlit`/`fastapi`/`starlette`; only
`integrations` imports `chainlit`. `import-linter` joins the UI `dev` extra
and the CI `ui` job runs `lint-imports` from the UI package directory.

### R4.2 Coverage gate

The CI `gate` job (one matrix cell is enough — Linux 3.11) measures branch
coverage of `ai_agent_lib_core.contracts`, `.config`, `.di`, and `.pipeline`
over the core package's tests and fails under a threshold. The threshold is
the specification's 90 when the measured value meets it; otherwise the
measured value (rounded down) is used and recorded in a comment beside the
step so the gate never regresses while the gap to 90 is closed separately.

## R5 — Developer experience

### R5.1 POSIX launchers

`setup-local.sh`, `start-local.sh`, `stop-local.sh`, `start-phoenix.sh` at the
repository root: `#!/bin/sh` wrappers resolving their own directory and
delegating to the same `tools/*.py` as their `.bat` twins, executable bit set.
The README's Agent Lab section documents both shells. A repository test
asserts each wrapper pair delegates to the same tool module.

### R5.2 `agentlib update --plan`

`Scaffolder.plan(root) -> UpdateReport`: the same walk as `update` with no
writes (`updated` means "would be updated"). `agentlib update --plan` prints
the would-be updated/created/kept files and exits without modifying the tree;
`--plan --diff` additionally shows diffs for kept files. Help text and the
documentation's warning about `--diff` are updated to point at `--plan`.

**Acceptance:** in a generated workspace with one stale generated file and one
developer-edited file, `update --plan` lists both correctly and a before/after
tree hash is identical; plain `update` still writes.

## R6 — Structure

- **R6.1 (deferred, own plan):** regroup `ai_agent_lib_cli` into `commands/`,
  `generation/` (scaffold, render, proposals, toml_text, yaml_lists, names,
  pins), `sources/` (readers, read_csv, read_openapi, read_redshift,
  dataplan), `deployment/` (deploy), `governance/` (governance, policy),
  `ops/` (processes, diagnostics), with CLI import contracts.
- **R6.2:** write ADR 0004 recording the tracing-configuration decision: keep
  the separate `LANGFUSE_*`/`PHOENIX_*` loader path (documenting its rules and
  why third-party names are kept), or fold tracing into the binding table with
  aliases. The ADR records the decision; no refactor in this change.
- **R6.3:** remove `experiments/chainlit-phase0/` (the decision and evidence
  live in `docs/frontend-phase0-review.md` and git history); update links that
  reference the directory; the documentation link check passes.

## Acceptance for the whole change

From the repository root with the contributor setup: `python -m ruff check .`,
`python -m ruff format --check .`, `python -m mypy`, `lint-imports`, and
`python -m pytest` all pass; the UI package's own pytest, mypy, and new
`lint-imports` pass in its environment; `python docs/guide/build_guide.py
--check` passes; a freshly generated fake-model agent passes `agentlib check`
and `agentlib eval` reports "no evals… / skipped" semantics unchanged except
the new baseline behavior.

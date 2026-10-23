# Enterprise architecture review — 18 October 2026

Reviewed the clean checkout at `ced474e` against the library's stated goals: one
governed foundation for building LangGraph agents and MCP servers, with
cross-cutting concerns abstracted through separation of concerns, interfaces,
and dependency injection; local-first defaults for contained desktops that
allow only approved binaries and an enterprise pip index; and evaluation of
agents both offline and online.

This review complements the [13 October code structure review](code-structure-review-2026-10-13.md).
That review's three runtime findings are fixed; several of its structural
proposals remain open and are carried forward here with priorities. The
companion [improvement specification](improvement-specification-2026-10-18.md)
turns the findings below into requirements, and the implementation plan at
`docs/superpowers/plans/2026-10-18-enterprise-review-improvements.md` breaks
them into tasks a coding agent can execute.

## What is working well — keep it

These properties are the library's value. No finding below proposes weakening
them, and a broad rewrite would lose them for no benefit.

1. **Ports and adapters with enforced boundaries.** Every capability is a
   protocol in `contracts`; eleven import-linter contracts plus the
   architecture tests make the layering real, not aspirational. `contracts`
   imports nothing; only `integrations.langgraph` imports LangGraph; core never
   imports `boto3`; the request path never imports `evaluation`.
2. **Configuration containment.** One binding table
   (`config/bindings.py`) owns every `EAP_` name; unknown names fail startup;
   the variable reference and `.env.example` are generated from it; an
   architecture test forbids `EAP_` literals and environment reads elsewhere.
3. **Deliberate dependency injection.** `ConfigResolver` → frozen
   `ServiceConfig` → `ServiceProviders` registry → `ServiceContainer`
   lifecycle, with typed `ServicePort[T]` descriptors, declared dependencies,
   validated build order, reverse teardown, and task-affine adapter lifetimes
   (ADR 0001). No DI framework, no service locator, no import-time side effects.
4. **Fail-closed governance.** Fixed interceptor order in one assembly module,
   typed error taxonomy where `PolicyDenied` is never retried and never falls
   back, schema-pinned tool registries, scoped storage keys, cross-tenant
   leakage contract tests.
5. **Local-first defaults for contained environments.** SQLite, JSONL, DuckDB
   over CSV, the fake model, in-process policy rules, and the static dev
   principal — all flagged `local_only` with a one-way guard — give a complete
   stack with no Docker, no cloud account, and pip-only installation
   (ADR 0002). Approved-Python/pip discipline is carried through every doc.
6. **Testing strategy.** Contract suites shipped inside the package and reused
   by consumers; fakes over mocks; socket-blocked unit tests; template
   generation tested per variant; consumer wheel gates at minimum and latest
   framework versions; strict mypy; CI on Windows and Linux at 3.11 and 3.13.
7. **Documentation discipline.** A documentation map, code ownership map,
   request-flow walkthrough, ADRs, and a release-scope traceability matrix that
   separates the target design from shipped behavior. Doc freshness is tested.

## Findings

Ordered by value to the library's stated goals. "Carried forward" marks items
the 13 October review proposed that remain open.

### F1 (P1) — Online evaluation does not exist

The stated goal includes evals "both offline and online". Offline evals exist
(see F2); there is no online path at all:

- **No feedback capture.** Agent Lab's Chainlit data layer explicitly rejects
  feedback: `upsert_feedback` raises `HTTPException(404, "Feedback is not
  enabled")`
  ([data_layer.py:261](../../packages/ai-agent-lib-ui/src/ai_agent_lib_ui/integrations/data_layer.py)).
  The one place a person already judges real answers throws the judgement away.
- **No production-trace scoring.** The library records everything needed —
  accepted outcomes in the `OutcomeJournal`, model traces in JSONL, Agent Lab
  conversations in SQLite, spans in Phoenix/Langfuse — but nothing samples
  those records, replays them through scorers, or reports drift against an
  offline baseline.
- **No interaction record type.** There is no shared, versioned schema for
  "one production exchange" that feedback, exports, and scorers could agree on,
  in the way `agentlib.decision/v1` and `agentlib.registry/v1` anchor their
  concerns.

The import boundary that keeps `evaluation` off the request path is already in
place and is exactly right for online evals too: capture happens on the
request path (cheap, append-only), scoring happens off it.

**Recommendation.** Add a versioned `Interaction` record and JSONL store, a
deterministic sampler, and `score_interactions(...)` that reuses the existing
`run_eval`/`Scorer` machinery over recorded answers; implement Chainlit
feedback capture in Agent Lab writing versioned `FeedbackRecord` lines; add an
Agent Lab export from its conversation store to interaction records. Spec
requirements R2.x.

### F2 (P1) — Offline evaluation is a seed, not yet a capability

`evaluation/` (cases, run, scorers, report — ~600 lines) is well designed:
clean `Scorer` protocol, NaN-safe scores, `judged` with a model-alias judge,
JSONL cases, thresholds via `report.require`. The CLI (`agentlib eval`) and the
generated per-agent eval test and dataset close the loop. What an enterprise
team needs next is missing:

- **No regression story.** A report is written to
  `.agentlib/eval-report.json` and forgotten. Nothing compares a run to a
  baseline, so "did this prompt change make the agent worse" is answered by a
  human diffing JSON.
- **No provenance.** The report does not record which model, dataset digest,
  or revision produced it, so two reports cannot be compared trustworthily.
  The JSON has no schema identifier, unlike every other record the library
  writes.
- **No per-case timeout.** One hung case hangs the whole eval
  ([run.py](../../packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/run.py)
  awaits `task(case)` unbounded) — the same class of problem as the fixed
  Langfuse checkpoint stall.
- **No judge calibration.** The `judged` docstring says "check the judge
  against answers a person has graded first", but nothing in the library does
  that check.

**Recommendation.** Version the report schema and add run metadata and a
dataset digest; add `case_timeout` to `run_eval`; add baseline
load/compare/`require_no_regression`; add a small calibration harness for
judges; add `agentlib eval --accept` to promote a report to the baseline.
Spec requirements R1.x.

### F3 (P1, carried forward) — Every service repeats ~150 lines of composition

The HTTP entry points of the generated template (130 lines), accounts-agent
(150), and rmprep (197) each re-implement the same sequence: argparse,
`load_service_config`, `configure_logging`, `start_telemetry`,
`langfuse_tracing`, container construction and validation,
`ServiceLifecycle` wiring, `serve`, teardown, and `report_error`. This is the
exact cross-cutting concern the library exists to abstract, and it is the code
every application team copies and then drifts on. The 13 October review
proposed extracting it (suggestion 4); it remains open.

**Recommendation.** Add a composition helper in `integrations/http`
(`AgentService` value + `run_agent` entry point) that owns infrastructure
wiring and accepts `build_graph`, `respond`, and optional stream/inspection/
outcome providers. Keep `agent_app`, `ServiceContainer`, and existing
`build_app` signatures working. Migrate the template and both reference
agents; a generated service's `service.py` should shrink to its business
content (~40 lines). Spec requirements R3.x.

### F4 (P2) — Local developer entry points are Windows-only

`setup-local.bat`, `start-local.bat`, `stop-local.bat`, and
`start-phoenix.bat` are 4-line wrappers over portable Python
(`tools/local_dev.py` and friends), yet there are no POSIX equivalents and the
README's Agent Lab path says "Open Command Prompt". The tutorial documents
bash; the simulator stack does not. For mixed fleets (this checkout is running
on macOS) the contained-environment story should be identical on both.

**Recommendation.** Ship `*.sh` wrappers mirroring each `.bat` (same
`python3 tools/local_dev.py` delegation), document both, and test that the
wrappers stay in step. Spec requirement R5.1.

### F5 (P2, carried forward) — Agent Lab has no enforced boundaries

The UI package has the right shape (`contracts/`, `application/`, `adapters/`,
`integrations/`, `config/`) and today only `integrations/` imports Chainlit/
Starlette — but nothing enforces that. Root import-linter covers core, AWS,
and CLI only; the UI pyproject has no import-linter section and the CI `ui`
job never runs `lint-imports`. The 13 October review proposed this
(suggestion 7); it remains open. Boundaries that are only conventions decay.

**Recommendation.** Add import-linter contracts to the UI package (contracts
import nothing from the package; application is framework-free; only
integrations imports Chainlit) and run them in the UI CI job. Spec
requirement R4.1.

### F6 (P2) — The specified coverage gate is not enforced

The specification (§19) sets "90% branch coverage on `contracts`, `config`,
`di` and `pipeline`" as the CI bar from phase 2. `pytest-cov` is installed,
but no CI step measures coverage and nothing fails below a threshold.

**Recommendation.** Add a CI step measuring branch coverage of those four
core modules with a fail-under gate; record the chosen threshold where it is
set. Spec requirement R4.2.

### F7 (P2, carried forward) — `agentlib update --diff` writes while "previewing"

`Scaffolder.update()` writes updated files during its walk; `--diff` only
changes what is printed. The docs now warn "the flag is not a dry run", which
documents the trap without removing it. The 13 October review proposed a real
preview (suggestion 3); it remains open.

**Recommendation.** Add `Scaffolder.plan(root)` (same walk, no writes,
same `UpdateReport`) and an `agentlib update --plan` flag that previews every
change — updated, created, and kept — then exits without touching the tree.
Spec requirement R5.2.

### F8 (P3) — CLI package organization is flat and mixed

`ai_agent_lib_cli` has 26 top-level modules plus a `commands/` package.
Command definitions live in `commands/`, but their collaborators — generation
(`scaffold`, `render`, `proposals`, `toml_text`, `yaml_lists`, `names`,
`pins`), source introspection (`read_csv`, `read_openapi`, `read_redshift`,
`readers`, `dataplan`), deployment (`deploy`), governance (`governance`,
`policy`), and operations (`processes`, `diagnostics`) — sit unsorted beside
them, and no import contract orders them. The CLI is where application teams
first read library code; its organization should teach the same layering the
core does.

**Recommendation.** Regroup into `commands/` (thin Click), `generation/`,
`sources/`, `deployment/`, `governance/`, and `ops/` subpackages, and add CLI
import contracts (commands may import the services; services never import
commands). Mechanical, but wide: schedule as its own change after the
higher-value work. Spec requirement R6.1 (deferred; separate plan).

### F9 (P3) — Tracing configuration lives outside the containment system

`config/langfuse.py` and `config/phoenix.py` read `LANGFUSE_*`, `PHOENIX_*`,
and `EAP_LANGFUSE_ENABLED` through their own loaders rather than the binding
table, and tracing code is spread across `config/`, `observability/`, and
`integrations/langgraph/` (nine modules). The October `doctor` blind spot was
a direct symptom of the parallel configuration path; the fix shared the
loader, but the second path still exists, and "unknown `EAP_` names fail
startup" does not protect these sections equally.

**Recommendation.** Record a decision (ADR 0004): either fold tracing
settings into the binding table as sections with third-party names kept as
aliases, or explicitly accept the second path and document its rules. Do not
refactor ahead of the decision. Spec requirement R6.2 (decision record only).

### F10 (P3) — Concluded spike still ships in the main tree

`experiments/chainlit-phase0/` (own pyproject, lockfiles, JPEG evidence) was a
phase-0 trial whose decision is recorded in
[frontend-phase0-review.md](../frontend-phase0-review.md). Keeping concluded
spike code beside supported packages costs every reader a "is this real?"
check and keeps stale pins in the repo.

**Recommendation.** Remove the directory (history and the review document
preserve the evidence); fix the links that point into it. Spec requirement
R6.3.

### F11 (P3, carried forward) — Local-stack orchestration sits outside the CLI

`tools/` holds 15 modules of setup, supervision, redaction, and readiness
logic for the rmprep demo stack, tested from `tests/architecture/`. It
partially duplicates `ai_agent_lib_cli.processes`, and none of it is
installable for application teams, who will want the same "start my agent,
its MCP servers, and Agent Lab" workflow for their own workspaces
(13 October review, suggestion 2).

**Recommendation.** Keep `tools/` for the repository's own demo scenario, but
plan an `agentlib lab` command that generalizes it for generated workspaces
(resolve registered MCP dependencies, start in order, print URL and login-code
location). Design-level recommendation; schedule after R1–R5.

### F12 (P4) — Observations not requiring action now

- `di/container.py` (946 lines) is large but already delegates to focused
  collaborators (`lifetime`, `dependencies`, `authentication`, `mcp`,
  `providers`); splitting further is optional polish, not a need.
- `contracts/options.py` (509 lines) centralizes option models deliberately —
  one place to see every section. Keep.
- rmprep still carries legacy checkpoint projections (`focus.py`,
  `context.py:legacy`) by design, with explicit boundary migration. Acceptable;
  re-examine when old checkpoints age out.
- The developer guide's HTML-source pipeline (`guide.src.html` → generated
  HTML + companion files) is unusual but tested and self-checking. Not worth
  churning.

## Traceability to the library's goals

| Goal | Today | Gap closed by |
| --- | --- | --- |
| Build agents and MCP servers on governed plumbing | Strong: pipelines, registries, policy, identity, contract suites | — (keep) |
| Cross-cutting concerns via SoC/OOP/DI | Strong in core; weakest at the service entry point, which every team copies | F3 |
| Offline evals | Minimal but sound seed | F2 |
| Online evals | Absent | F1 |
| Local-first defaults in contained environments | Strong on Windows; asymmetric elsewhere | F4 |
| Easy to understand, test, maintain | Strong core; flat CLI, unenforced UI boundary, missing coverage gate | F5, F6, F8 |

## Suggested order of work

1. **Phase 1 — Offline evaluation depth** (F2): report schema v1, metadata,
   dataset digest, per-case timeout, baselines, calibration, `eval --accept`.
2. **Phase 2 — Online evaluation** (F1): feedback capture in Agent Lab,
   interaction records and export, sampling, trace scoring, evals guide.
3. **Phase 3 — Service composition** (F3): `AgentService`/`run_agent`,
   migrate template and both reference agents.
4. **Phase 4 — Quality gates** (F5, F6): UI import contracts in CI; coverage
   gate on core's critical modules.
5. **Phase 5 — Developer experience** (F4, F7): POSIX launchers;
   `agentlib update --plan`.
6. **Phase 6 — Structure** (F8–F10): archive the spike, ADR 0004, then the
   CLI regrouping as its own plan.

Each phase leaves the tree releasable; none changes a public API
incompatibly. The [specification](improvement-specification-2026-10-18.md)
states the requirements and acceptance criteria; the implementation plan
turns phases 1–5 and the quick phase-6 items into executable tasks.

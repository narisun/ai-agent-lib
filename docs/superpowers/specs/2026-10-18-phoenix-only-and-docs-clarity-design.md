# Phoenix-only observability and documentation clarity — design

18 October 2026. Approved direction from the maintainer: review the README and
docs for readability and currency; make the docs establish the core ideas
(separation of concerns, cross-cutting concerns, pluggable and extendable
architecture, a codebase that is easy to understand, test, and maintain);
**keep Phoenix and fully remove Langfuse** to reduce unnecessary
configuration; make the configuration docs state plainly what is default and
local versus the AWS/server-side options; and bring code comments and
docstrings in line so the whole story is coherent, simple, and precise.
"Finish the rmprep migration" is confirmed in scope: with Langfuse gone,
rmprep's entry point collapses onto the shared `run_agent`.

This design is grounded in a five-scout inventory (Langfuse footprint — 55+
locations; tracing architecture; 25-doc audit; configuration-doc clarity;
comment audit of 16 modules). Scout corrections applied here: Phoenix stays —
`docs/phoenix-trial.md` and the README's Phoenix mentions are **kept and
promoted**, not deleted.

## Intent

One tracing backend (Phoenix), reached through one composition call
(`run_agent`), configured by one small variable set (`EAP_PHOENIX_*` +
`PHOENIX_*`), documented in one guide — and a documentation tree whose front
door, hub, tutorial, and reference all tell the same architecture story the
code enforces.

## Goals

1. Langfuse is gone: code, settings, tests, templates, guides, and links.
2. `run_agent` wires Phoenix tracing, so a generated agent (and
   accounts-agent) honors `EAP_PHOENIX_ENABLED` with zero code.
3. rmprep's `main`/`_serve` collapse onto `run_agent`; its bespoke entry code
   and Langfuse shims are deleted.
4. No silent coverage loss: every shared-behavior test that lived only in a
   Langfuse test file is ported to Phoenix before the file dies.
5. Configuration docs state the local-first contract plainly and show, per
   section: the local default, whether it is local-only, and the AWS/server
   option.
6. The docs below the root README catch up with the eval/composition wave and
   lose their Windows-only framing; the developer guide's principles section
   gains the `AgentService`/`run_agent` composition-root exemplar and a
   refreshed evals story.
7. Docstrings and comments match the docs' vocabulary; the audit's findings
   are fixed.

## Non-goals

- No new tracing backends, no pluggable-tracing registry. `agent_tracing`
  stays as the single named seam; adding a backend later is a change inside
  that one module.
- No binding-table registration of `PHOENIX_*` variables (ADR 0004's deferred
  step stays deferred; the ADR is amended to record the removal).
- No restructuring of the doc tree: ownership stays as is (guide owns the
  principles narrative; docs/README is the hub). Frozen records
  (Specification, design reviews, executed plans, dated verified-run docs)
  are not rewritten.
- No CLI regrouping, no `agentlib lab` — unchanged from the previous cycle's
  deferrals.

## Anti-goals (failure even if it "works")

- A generated agent that silently stops tracing (run_agent must gain Phoenix,
  not lose Langfuse into nothing).
- Old Agent Lab conversations erroring on reload because stored `langfuse`
  trace links no longer validate.
- Deleting shared `otel` machinery: the `otel` extra, opentelemetry deps,
  `_agent_tracing.py`, `_openinference.py`, the `SpanSchema`/`TraceSettings`
  protocols, `publish_trace`, and the model-trace JSONL sink all serve
  Phoenix and operational telemetry and must survive.
- Rewriting history: dated records keep their Langfuse mentions.

## Constraints

- All existing import-linter contracts and architecture tests keep passing;
  generated artifacts (`docs/variables.md`, `.env.example`,
  `docs/developer-guide.html`, generated-agent `.env.example`) are changed
  only via their generators and regenerated in the same change.
- `config` may not import `di` (layering) — the local-only documentation
  mechanism must respect that (see D5).
- Public API changes are removals of Langfuse names plus additive changes
  only; the `AgentService`/`build_agent_app`/`run_agent` call shapes stay
  compatible for existing callers (`tracing` parameter widens, never
  narrows).
- Same quality gates as the previous cycle: ruff, strict mypy (8 known
  Windows-only errors), lint-imports, full pytest, UI suite in its own venv,
  coverage gate, guide freshness.

## Decided design

### D1 — Phoenix-only tracing core

Delete `config/langfuse.py`, `integrations/langgraph/langfuse.py`, and
`LangfuseSchema` (keep `_trace_schema.py`'s protocols). Delete
`config/tracing.py` (`TracingResolution` and the dual-destination guard are
vacuous with one backend). Rewrite `integrations/langgraph/tracing.py` —
`agent_tracing(application, config, *, dotenv_path=Path(".env"),
settings=None)` becomes a thin Phoenix-only wrapper (resolve settings, yield
a running `PhoenixTracing` or `None`), no `legacy_prefix`. It remains the
single composition-boundary name the docs teach.

`composition._serve` switches from `langfuse_tracing` to `agent_tracing`
(function-local import, same model-trace pairing: the one adapter instance
goes to both `ServiceContainer(model_trace=...)` and
`build_agent_app(tracing=...)`). `build_agent_app`'s `tracing` parameter is
retyped to `AgentTracing | None` (runtime only calls `.run`); the template,
accounts-agent, and rmprep drop their `LangfuseTracing` annotations and
rmprep drops its cast. **Recorded behavior change:** `run_agent` now honors
`EAP_PHOENIX_ENABLED` — called out in release-scope notes;
`EAP_PHOENIX_CAPTURE_CONTENT` defaults to false, bounding content exposure.

`config/resolver.py` drops the `LANGFUSE_VARIABLES` whitelist: a leftover
`EAP_LANGFUSE_*` in a service `.env` fails startup loudly as an unknown
variable (deliberate; release note says to delete the lines). Plain
`LANGFUSE_*`/`RMPREP_LANGFUSE_*` names become inert — the release note names
them too. The legacy-prefix alias mechanism dies with its only user.

### D2 — rmprep finishes migrating

Add one additive hook to `AgentService`:
`app_extras: Callable[[ServiceContainer], Mapping[str, Any]] | None = None`,
resolved inside `build_agent_app` (which holds the live container) and merged
over `app_kwargs` (extras win on key conflict; docstring says so). This is
what rmprep's container-aware `LocalTurnInspector` needs.

rmprep's `service.py` then collapses: `_build_graph(services)` constructs its
`ContextBuilder(load_context_budget(ENV_FILE))` itself; `_outcomes(config)`
stays; `_extras(services)` builds the local-env inspector; `SERVICE =
AgentService(..., env_file=ENV_FILE, outcomes=_outcomes,
app_extras=_extras)`; `main = run_agent(SERVICE, argv)`. The thin `build_app`
compat shim stays for its tests. Prefer core `read_question` over rmprep's
duplicate `_question` unless a test pins the exact wording (builder
verifies). Delete `langfuse_settings.py`, `langfuse_trial.py`, the pyproject
`[tool.ai-agent-lib.langfuse]` table, and the `.env.example` Langfuse block
(replaced by the Phoenix block). `tests/test_context_settings.py`'s
entrypoint test is rewritten to the new seam (the budget-enforcement test
through `build_app` already covers the behavior); `test_langfuse_trial.py`
becomes `test_phoenix_trial.py` keeping its Phoenix halves plus the ported
exporter-closed-on-container-failure and broken-exporter-concurrency tests.

### D3 — coverage ports before deletion

- Core: the shared `AgentTracing` behaviors tested only in
  `test_langfuse.py` (callback preservation; ContextVar isolation under
  concurrency/cancellation; disabled-integration-imports-no-OTel-SDK;
  bounded/timed-out checkpoint collection; `checkpoint_timeout_seconds`
  validation) move into `test_phoenix.py` against `PhoenixTracing`. The
  dual-destination test dies with the guard.
- CLI: `tests/generated/test_langfuse_agents.py` becomes
  `test_phoenix_agents.py` — a generated agent traced end-to-end (JSON + SSE
  trace links, session correlation across turns) with an in-memory exporter,
  asserting Phoenix attribute keys and the Phoenix variables in the generated
  `.env.example`.
- Architecture: `test_local_config_report.py` goes Phoenix-only; the masking
  and escaping fixtures move onto `PHOENIX_API_KEY`-style values so that
  coverage survives.
- CLI diagnostics tests: Langfuse doctor/explain cases are deleted; verify
  the existing Phoenix cases keep masking/precedence coverage, adding any
  that only existed for Langfuse.

### D4 — UI: narrow the contract, tolerate old rows

`TraceLink.provider` narrows to `Literal["phoenix"]` (route and label
simplify). Stored conversations written when Langfuse existed must keep
loading: a `mode="before"` validator on `RunRecord.trace` degrades any trace
payload that no longer validates to `None` (answers and metrics intact),
with a test that a stored langfuse-shaped row loads trace-less. The
`data_layer` label keeps the "Agent Lab" branding for Phoenix links and gains
the why-comment the audit asked for (local Phoenix ships embedded as Agent
Lab's trace viewer). UI README gains the missing feature docs (feedback
capture + `feedback.jsonl`, `export_interactions`, the enforced import
contracts), fixing `docs/evals.md`'s dangling cross-reference. UI tests:
langfuse parametrizations drop; the malformed-URL hardening matrix is kept
against Phoenix URLs.

### D5 — configuration clarity (local-first, explicitly)

- `config/phoenix.py` gains `phoenix_env_example()`;
  `config/reference.py::render_env_example` and CLI `envfiles.agent_env`
  append it where the Langfuse block used to go (fixes the historical
  asymmetry where Phoenix never appeared in `.env.example`).
- `render_reference` gains a preamble stating the local-first contract ("with
  nothing set you get the local profile: file, SQLite, and in-process
  providers, no cloud account; `EAP_PROFILE=aws` or any
  `EAP_<SECTION>_PROVIDER` opts into server providers") and a generated
  **Profile defaults** table (section | local default | AWS default) from the
  existing `_profile_defaults` machinery. `render_env_example`'s header gains
  the one-line version.
- Local-only visibility without breaking layering (`config` cannot import
  `di`): the per-section `details` strings in `bindings.py` name both sides
  and flag the local-only one (the way IDENTITY and POLICY already do), and
  `EAP_DATA_SOURCES` names `duckdb_csv` as the local-only kind with
  `rest`/`redshift_data` deployable. A **new architecture test** asserts the
  hand-written local-only list in the `EAP_DEPLOYMENT_ENV` details matches
  the `local_only` registrations in `di/defaults.py`, killing the drift risk.
- `bindings.py` TELEMETRY detail fixes `configure_telemetry()` →
  `start_telemetry()`.
- `guide.src.html`'s variables-lesson intro reframes Local as the zero-config
  default and AWS as per-variable opt-in (not "Windows tutorial vs Fargate").
- Root README gains one local-first sentence near "Run your first agent".
- Regenerate `docs/variables.md`, `.env.example`, `docs/developer-guide.html`
  via their generators in the same change.

### D6 — documentation wave

`docs/phoenix-trial.md` becomes the single tracing guide: Langfuse
comparisons and dual-destination framing removed; a new short section states
that generated services trace through `run_agent` (set
`EAP_PHOENIX_ENABLED=true`, done) with the manual `agent_tracing` wiring as
the by-hand appendix; dated verified-run records fold into `<details>`;
commands go dual-OS. Delete `docs/langfuse.md` + `docs/langfuse-trial.md`
and fix every inbound link (hub, release-scope, evals.md — whose privacy
rule is inlined — core/UI/rmprep READMEs, guide prose). The generated-agent
`docs/langfuse.md.jinja` is replaced by a slim `docs/tracing.md.jinja`
(Phoenix quick-start for the generated workspace; the CLI file-set pin and
`envfiles.variable_names` follow).

Readability/currency edits per the audit, by file: root README (dual-OS
convention stated once instead of six "Or on POSIX" pairs; verified-run
detail trimmed to its link; explore-table rows for Agent Lab and evals;
contributor note on the coverage gate), docs/README (drop the
Windows-Command-Prompt framing; hub rows for identity.md,
http-thread-admission.md, anthropic-example.md, verified-anthropic-run.md,
frontend-contracts.md, and the two 2026-10-18 design records; Find-the-code
rows for `evaluation/`, `integrations/http` composition, and UI `contracts/`;
POSIX-ized test-command table; `update --plan` surfaced for upgraders),
getting-started (step 8 rewritten around `agentlib eval`, `--accept`,
baselines, with a link to evals.md; step 9 leads with `update --plan`;
recipes/read-more rows), local-startup (dual-OS pass — the biggest single
staleness; precedence section tabled), CLI README (command table gains
`--accept`/`--plan`; three update sections become one; quickstart drops its
venv-activation divergence; Toolbox narrative kept), core README
(composition + evals mentioned; venv wording fixed), AWS README (one
Command-Prompt line), UI README (per D4), rmprep README (service.py
code-map row; Phoenix wording; validation-rules duplication with
local-startup reduced to one canonical copy), evals.md (fix the "--accept
only runs after the evals already passed" misreading; inline the privacy
rule), release-scope.md (matrix rows for the eval/composition/gates/launcher
wave or an explicitly narrowed intro claim — builder's judgment; `update
--plan` noted; Phoenix-only recorded as a scope decision with the
`EAP_LANGFUSE_*`-fails-startup migration note).

Guide (`guide.src.html`): principles section gains `AgentService`/`run_agent`
as the composition-root exemplar with a short worked example; one bridging
sentence reconciles the spec's "interceptor" with the guide's "pipeline";
links to http-thread-admission.md and release-scope's
`ServiceProviders.register` block as worked port/adapter cases; the #evals
section catches up with baselines/online evals and links docs/evals.md;
Langfuse prose removed per the footprint inventory. ADR 0004 is amended:
Langfuse removed, Phoenix the only agent-trace backend, single-loader rule
restated for Phoenix, binding-table registration still future work.

### D7 — comment and docstring pass

Fix everything the audit marked `edit`: composition.py (module docstring,
`build_agent_app` arg docs incl. tracing ownership, `Respond`/`StreamSteps`
docstrings); agent.py (admission "legacy stream request IDs" reword;
`_recorded_answer` docstring; the dead-looking "unauthorized" entry is
verified against outcome-journal tests before touching — comment-only if
live); container.py + providers.py (define "legacy order" once; name the
audit `tracing: true` option consistently; de-crypt the "permission plan"
comment); `_agent_tracing.py` (`trial` → `tracing`, 65/64 over-fetch comment,
`_CONTENT_LIMIT` unit, name the JSONL sink); evaluation/online.py (module
docstring rewritten: interactions are exported from saved conversations, not
captured on the request path); data_layer.py (label why-comment; `close()`
comment/docstring merge); bindings.py TELEMETRY detail. "Legacy" ends up
with one meaning per file or is replaced.

## Sequencing constraints (from the inventory's couplings)

1. Coverage ports (D3) land with or before each deletion they protect.
2. `build_guide.py` loses its langfuse import **before** the module deletion;
   generated artifacts regenerate in the same commit as their generator
   edits.
3. `config/resolver.py` + `tests/architecture/test_config_containment.py`
   change atomically with the `config/langfuse.py` deletion.
4. composition retype (D1) lands before rmprep's cast removal (D2).
5. Docs that link langfuse guides change in the same task that deletes them.

## Verification (whole change)

Full main suite, UI suite (own venv), ruff + format, strict mypy (known 8),
lint-imports (11 + 3 UI contracts), coverage gate, `build_guide.py --check`,
regeneration checks, and a final `grep -ri langfuse` whose only hits are the
frozen records (docs/design/*, docs/superpowers/*, Specification, dated
review docs) and — if kept — inert defensive entries explicitly decided
(e.g. `tools/phoenix_dev.py`'s env scrub, builder's call, commented).
A fresh generated agent must pass `agentlib check`, and `agentlib doctor`
must show the Phoenix row and no Langfuse row.

## Builder-check resolutions (designer's calls)

A pre-planning builder review raised four questions; resolved as follows.

1. **How `app_extras` reaches the compiled graph (rmprep's inspector).** The
   hook receives the container AND a zero-arg graph getter:
   `app_extras: Callable[[ServiceContainer, Callable[[], Any]],
   Mapping[str, Any]] | None`. `build_agent_app` passes its own closure over
   the prepared graph (the same object `respond` receives); the getter is
   valid once readiness has passed, which every request implies — matching
   rmprep's existing `graph_box[-1]` pattern. The `build_app` compat shim
   drops `app_extras` the same way it rebuilds `outcomes`, so an explicit
   `inspector=` argument from a direct caller always wins.
2. **Fail-fast on a malformed `RMPREP_CONTEXT_*` value is preserved.** rmprep
   keeps a thin `main` that builds
   `ContextBuilder(load_context_budget(ENV_FILE))` first — reporting the
   error and returning non-zero before any listener exists — then delegates
   to `run_agent` with the builder threaded into `build_graph`. The pinned
   entrypoint test is rewritten to assert exactly that (budget failure →
   non-zero exit, `run_agent` never invoked).
3. **Phoenix's server-side story.** Phoenix is local-first and
   remote-capable: the default is the bundled local server
   (`start-phoenix`), and the server-side option is a team-hosted Phoenix
   over HTTPS via `PHOENIX_COLLECTOR_ENDPOINT` + `PHOENIX_API_KEY` (the
   settings already validate exactly that). Docs state it that way, with the
   caveat that the API key is read from the process environment (inject it
   as a task-definition secret on Fargate) — not the secrets provider — and
   that binding-table registration remains ADR 0004 future work.
4. **Trace-link tolerance applies everywhere, not just stored rows.** One
   helper degrades any trace payload that no longer validates to `None`
   (answer and metrics intact), used by BOTH the stored-row path
   (`RunRecord.trace` before-validator) and the live ingestion paths in the
   UI adapters — a mixed-version rollout where an old agent still emits
   `provider: "langfuse"` must not fail the answer. Tests cover both paths.

## Left to the builder

- Exact wording of rewritten prose (match each doc's existing voice; the
  models are identity.md, evals.md, the AWS README).
- Whether `tools/phoenix_dev.py` keeps the `LANGFUSE_` env-scrub entry
  (harmless defense) — either way, with a comment or clean removal.
- Mechanics of the `RunRecord.trace` degradation validator (field validator
  vs model validator) — whichever reads cleaner under the UI's pydantic
  conventions, with the stored-row test as the contract.
- How much of `test_langfuse_trial.py`'s big dual-parametrized test survives
  in the Phoenix port (keep the Phoenix halves; drop Langfuse-specific
  attribute assertions).
- Order of doc edits inside the docs wave, provided link integrity holds per
  task.

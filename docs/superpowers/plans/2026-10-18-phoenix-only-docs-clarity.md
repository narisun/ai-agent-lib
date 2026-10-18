# Phoenix-Only Observability and Docs Clarity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `docs/superpowers/specs/2026-10-18-phoenix-only-and-docs-clarity-design.md` — full Langfuse removal with Phoenix as the only agent-trace backend, rmprep finishing its migration onto `run_agent`, configuration docs that state the local-first contract plainly, a documentation wave that catches every doc up with the eval/composition work and the architecture narrative, and a docstring/comment coherence pass.

**Architecture:** The removal follows a quarantine-then-delete sequence: first every importer stops touching the Langfuse modules (suite stays green with the modules present), then the modules, settings, and tests are deleted atomically with their generated artifacts regenerated. Coverage that lived only in Langfuse test files is ported to Phoenix *before* deletion. rmprep collapses onto `run_agent` via one additive hook (`app_extras` receiving the container and a graph getter). Docs change only through their generators where generated, and in-voice by hand where prose.

**Tech Stack:** Python 3.11+, pydantic 2, pytest + pytest-asyncio (auto), ruff (line 100, Google docstrings), strict mypy, import-linter, Click, Jinja templates, OpenTelemetry SDK (otel extra), Chainlit 2.12 (UI, separate venv).

## Global Constraints

- Work in the worktree the controller creates; `.venv/bin/python` for the main tree, `.venv-ui/bin/python` for `packages/ai-agent-lib-ui` (run UI commands from that directory).
- Repo-wide mypy on this macOS machine shows 8 pre-existing Windows-only `[attr-defined]` errors (tools/local_processes.py, tests/architecture/test_stop_local.py, core test_http_windows_signals.py) plus 1 in UI test_browser_protocol.py — not findings; any NEW error is.
- Generated artifacts are NEVER hand-edited: `docs/variables.md` and `.env.example` regenerate via `.venv/bin/python -m ai_agent_lib_core.config reference` / `... env-example` (check `config/__main__.py` for exact subcommands); `docs/developer-guide.html` via `.venv/bin/python docs/guide/build_guide.py`. `tests/architecture/test_generated_docs.py` pins all three byte-exact.
- Frozen records keep their Langfuse mentions: `docs/Specification.md`, `docs/design/*.md`, `docs/superpowers/plans/*.md`, `docs/superpowers/specs/*.md`, dated verified-run sections. Never "fix" history.
- Phoenix stays everywhere: `docs/phoenix-trial.md`, README Phoenix mentions, `tools/phoenix_dev.py`, Phoenix tests. An instruction to remove "tracing" references means Langfuse only.
- Value types `@dataclass(frozen=True, slots=True)`; errors via the existing taxonomy (`ConfigurationError`, `ValidationFailed` with expected/actual/fix); async-first; no `os.environ` outside `config` (architecture-tested); no `EAP_` literals outside `config`.
- Commit per task, short imperative subject (e.g. `core: phoenix-only agent tracing`), body optional, ending with the repo's attribution trailer.
- Before each commit: focused tests, then `.venv/bin/python -m ruff check <touched packages>` + `ruff format --check`, mypy once, `.venv/bin/lint-imports` when imports changed.

---

## Phase 1 — Core goes Phoenix-only

### Task 1: Port shared AgentTracing coverage to Phoenix

**Files:**
- Modify: `packages/ai-agent-lib-core/tests/unit/test_phoenix.py`
- Read (source of ports, do NOT delete yet): `packages/ai-agent-lib-core/tests/unit/test_langfuse.py`

**Interfaces:**
- Produces: Phoenix-based tests covering the five shared `AgentTracing` behaviors so Task 3 can delete `test_langfuse.py` without coverage loss.

- [ ] **Step 1: Read both test files end to end.** The behaviors living ONLY in `test_langfuse.py` (line refs from the current file): existing-callback preservation through a full graph run (~136), ContextVar isolation under concurrent + cancelled turns (~205), disabled integration imports no OTel SDK and raises `ConfigurationError` without the otel extra (~257), bounded checkpoint diagnostics — stalled/failed/over-limit/cancelled history reads (~287), `checkpoint_timeout_seconds` validation (~364).
- [ ] **Step 2: Port each behavior into `test_phoenix.py`** against `PhoenixTracing`/`load_phoenix_settings` (same InMemorySpanExporter/injected-exporter style the file already uses; keep each test's assertions equivalent — the behaviors are base-class, only the adapter and settings fixture change). Name them so the origin is clear (e.g. `test_tracing_preserves_existing_callbacks`). Annotate `-> None`; strict mypy covers tests.
- [ ] **Step 3: Run both files** — `.venv/bin/python -m pytest packages/ai-agent-lib-core/tests/unit/test_phoenix.py packages/ai-agent-lib-core/tests/unit/test_langfuse.py -q` — all pass (langfuse file untouched).
- [ ] **Step 4: Lint/mypy, commit** `tests: port shared agent-tracing coverage to phoenix`.

### Task 2: Decouple every importer from the Langfuse modules

The Langfuse modules still exist after this task; nothing imports them outside their own tests. Suite stays green.

**Files:**
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/langgraph/tracing.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/langgraph/__init__.py` (export `AgentTracing` if not already public)
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/http/composition.py`
- Modify: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/agent/src/__package__/service.py.jinja`
- Modify: `examples/accounts-agent/src/accounts_agent/service.py`
- Modify: `examples/rmprep/agents/rmprep/src/rmprep/service.py` (imports/cast only — the full migration is Task 6)
- Modify: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/diagnostics.py`
- Modify: `docs/guide/build_guide.py`
- Test: existing suites + `packages/ai-agent-lib-core/tests/unit/test_http_composition.py`

**Interfaces:**
- Produces: `agent_tracing(application, config, *, dotenv_path=Path(".env"), settings=None)` — Phoenix-only async context manager yielding `PhoenixTracing | None`, no `legacy_prefix`; `build_agent_app(..., tracing: AgentTracing | None = None)`; `_serve` uses `agent_tracing`.
- Consumed by: Tasks 3, 5, 6.

- [ ] **Step 1: Rewrite `integrations/langgraph/tracing.py`** as the thin Phoenix-only wrapper:

```python
"""The agent-tracing entry point at the composition boundary.

One backend ships today: Phoenix. ``agent_tracing`` is the stable name a
service composes with; adding a backend later is a change inside this module,
never in a service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ai_agent_lib_core.contracts import ServiceConfig
from ai_agent_lib_core.integrations.langgraph.phoenix import PhoenixTracing, phoenix_tracing

__all__ = ["agent_tracing"]


@asynccontextmanager
async def agent_tracing(
    application: str,
    config: ServiceConfig,
    *,
    dotenv_path: Path = Path(".env"),
) -> AsyncIterator[PhoenixTracing | None]:
    """Yield a running tracing adapter, or None when tracing is not enabled.

    Resolves the Phoenix settings (``EAP_PHOENIX_*`` and ``PHOENIX_*``) from
    the process environment and ``dotenv_path``, starts the adapter, and
    closes it when the context exits. The adapter doubles as the model-trace
    sink: pass the same instance to ``ServiceContainer(model_trace=...)`` and
    to ``build_agent_app(tracing=...)``.

    Args:
        application: The service's name, stamped on every span.
        config: The resolved service configuration.
        dotenv_path: The service's own ``.env``, for the settings layering.
    """
    async with phoenix_tracing(application, config, dotenv_path=dotenv_path) as tracing:
        yield tracing
```

Adjust to `phoenix_tracing`'s REAL signature (read `integrations/langgraph/phoenix.py:61-87` first — if it takes `settings=`, pass it through and document it). Update the two core test callers (`test_phoenix.py:26,123`) if signatures shifted.

- [ ] **Step 2: composition.py** — module docstring line ~5 says "optional agent tracing (Phoenix)"; drop the `LangfuseTracing` TYPE_CHECKING import for `AgentTracing` (import from the module that defines it — `integrations/langgraph/_agent_tracing.py` via a public re-export: add `AgentTracing` to `integrations/langgraph/__init__.py`'s exports if absent); retype `build_agent_app(..., tracing: AgentTracing | None = None)`; `_serve` replaces the function-local `langfuse_tracing` import+call with `agent_tracing(service.application, config, dotenv_path=...)` (same env_file fallback logic as today, same single-instance pairing into `ServiceContainer(model_trace=tracing)` and `build_agent_app(tracing=tracing)`). While here, apply the audit's D7 items for this file: document `build_agent_app`'s args (incl. tracing ownership: the context manager owns/closes the adapter), add one-paragraph docstrings to the `Respond` and `StreamSteps` aliases (mirror `Run`/`Stream` in `http/agent.py:76-89`), reword `AgentService.env_file`'s convoluted fallback sentence.
- [ ] **Step 3: Retype the three services** — template `service.py.jinja` (lines ~16-18, 68) and `examples/accounts-agent/src/accounts_agent/service.py` (18, 69) import/annotate `AgentTracing | None` instead of `LangfuseTracing | None`; rmprep `service.py` drops the `LangfuseTracing` import and the `cast("LangfuseTracing | None", ...)` + its "predates Phoenix support" comment (31-33, 100, 163-167) — keep the rest of rmprep untouched for Task 6.
- [ ] **Step 4: CLI `diagnostics.py`** — remove the langfuse import (22), the `TracingResolution` import (24), the langfuse `CheckResult` block (72-83, 96) and dual-destination validate (97-101); `explain_service` (204, 228-231) resolves Phoenix only (`resolve_phoenix_settings` directly). Keep the phoenix doctor row and explain rows intact.
- [ ] **Step 5: `build_guide.py`** — remove the `LANGFUSE_VARIABLES` import (47), `rows.extend(_langfuse_variable_rows())` (249) and `_langfuse_variable_rows` (256-301), and the `ai_agent_lib_core.integrations.langgraph.langfuse` API_MODULES entry (731). Keep `_phoenix_variable_rows` and the langfuse.md walkthrough exclusion (790-795) for now (template dies in Task 4). Do NOT regenerate the guide yet — Task 3 regenerates everything once.
- [ ] **Step 6: Delete the langfuse cases in `packages/ai-agent-lib-cli/tests/unit/test_cli_diagnostics.py`** (the `_LANGFUSE_VALID` fixture ~185-189 and the four langfuse tests ~192-309, and the `(_LANGFUSE_VALID, 'Enable one')` phoenix-conflict parametrization case ~321). Verify the surviving phoenix tests still cover key masking and precedence (the file's phoenix test ~346 does); if any assertion existed only in a deleted langfuse test (e.g. the [tool...].legacy-prefix alias cases), note in the report that it dies with the alias mechanism — do not port alias tests.
- [ ] **Step 7: Regenerate the guide and verify.** Because Step 5 changed `build_guide.py`, the checked-in guide is now stale: regenerate `docs/developer-guide.html` (`.venv/bin/python docs/guide/build_guide.py`) and run `tests/architecture/test_generated_docs.py` (`docs/variables.md` and `.env.example` stay untouched — `reference.py` is unchanged in this task). Then: `grep -rn "langfuse" packages/ai-agent-lib-core/src packages/ai-agent-lib-cli/src examples/accounts-agent/src docs/guide/build_guide.py --include="*.py" --include="*.jinja" -il` → only the two langfuse modules themselves (config/langfuse.py, integrations/langgraph/langfuse.py) and config/tracing.py/reference.py/resolver.py/__init__.py/envfiles.py (Task 3's targets) plus rmprep's shims (Task 6). Run: full core + CLI suites, `test_http_composition.py` focused, ruff, mypy, lint-imports.

- [ ] **Step 8: Full suite** `.venv/bin/python -m pytest -q` green. Commit `core: phoenix-only agent tracing seam; decouple langfuse importers`.

### Task 3: Delete the Langfuse modules, settings, and generated surfaces

**Files:**
- Delete: `packages/ai-agent-lib-core/src/ai_agent_lib_core/config/langfuse.py`, `config/tracing.py`, `integrations/langgraph/langfuse.py`, `packages/ai-agent-lib-core/tests/unit/test_langfuse.py`
- Modify: `config/__init__.py`, `config/resolver.py`, `config/reference.py`, `config/phoenix.py`, `integrations/langgraph/_trace_schema.py`, `tests/unit/test_phoenix.py` (fixture leftovers), `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/envfiles.py`, `tools/local_config_report.py`, `tools/local_environment.py`, `tools/phoenix_dev.py`, `tests/architecture/test_config_containment.py`, `tests/architecture/test_generated_docs.py`, `tests/architecture/test_local_config_report.py`, `tests/architecture/test_local_phoenix.py`, `tests/architecture/test_phoenix_dev.py`, `docs/guide/guide.src.html` (langfuse code-walkthrough prose only), `docs/guide/build_guide.py` (walkthrough exclusion if simple)
- Regenerate: `docs/variables.md`, `.env.example`, `docs/developer-guide.html`

**Interfaces:**
- Produces: `phoenix_env_example() -> str` in `config/phoenix.py` (commented `EAP_PHOENIX_*`/`PHOENIX_*` block, mirroring the deleted `langfuse_env_example`'s shape), appended by `reference.py::render_env_example` and `envfiles.py::agent_env`; `variable_names()` emits `phoenix_*` labels instead of `langfuse_*`.

- [ ] **Step 1: `config/phoenix.py`** — add `phoenix_env_example()` returning a commented block covering `EAP_PHOENIX_ENABLED`, `EAP_PHOENIX_CAPTURE_CONTENT`, `EAP_PHOENIX_ENV_FILE`, `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_PROJECT_NAME`, `PHOENIX_API_KEY` with the defaults from `PHOENIX_VARIABLES` — read the deleted-module's `langfuse_env_example` (config/langfuse.py:239-253) FIRST and mirror its exact formatting conventions. Export it from `config/__init__.py` if `langfuse_env_example` was exported.
- [ ] **Step 2: The deletions + edits, in one sweep** (the module deletion forces atomicity):
  - `config/__init__.py`: drop `LangfuseSettings`/`load_langfuse_settings` import + `__all__` entries.
  - `config/resolver.py`: drop the `LANGFUSE_VARIABLES` import (30) and `known.update(LANGFUSE_VARIABLES.values())` (210); keep the PHOENIX whitelist line.
  - `config/reference.py`: drop the langfuse import (21) and the "Optional Langfuse integration" section (117-125); `render_env_example` appends `phoenix_env_example()` where `langfuse_env_example()` was (151).
  - `_trace_schema.py`: delete `LangfuseSchema` (44-89), keep the protocols.
  - `envfiles.py`: langfuse import (22) → phoenix; `agent_env` appends `phoenix_env_example()` (116); `variable_names()` emits `phoenix_*` labels (144) — read how templates consume `var.*` first (scaffold.py ~408/435); the generated tracing doc arrives in Task 4, so emit the labels `docs/tracing.md.jinja` will need (endpoint, project, api key, enabled, capture, env file).
  - `tools/local_config_report.py`: drop `EAP_LANGFUSE_*`/`RMPREP_LANGFUSE_*`/`LANGFUSE_BASE_URL`/`LANGFUSE_PROJECT_ID` from `_VISIBLE_EXTRAS` (31-38); keep `LANGFUSE_` in the child-env scrub prefix tuple (111) with a one-line comment ("defense for stale environments"); replace the `(tracing.langfuse, tracing.phoenix)` loop (119-122) with the single Phoenix resolution (the `TracingResolution` type is gone — take `PhoenixResolution | None`).
  - `tools/local_environment.py`: `resolve_tracing_settings` (24, 40-42) → `resolve_phoenix_settings`; adapt the report call.
  - `tools/phoenix_dev.py`: keep the `LANGFUSE_` scrub entry, add the same one-line comment (design leaves this open; keeping with a comment is the decided default).
  - `tests/architecture/test_config_containment.py`: drop langfuse import (13) + `known.update(...)` (118).
  - `tests/architecture/test_generated_docs.py`: drop langfuse import (18) + the exactly-once table assertions (103-104); add the equivalent `PHOENIX_VARIABLES` exactly-once assertions if not present.
  - `tests/architecture/test_local_config_report.py`: Phoenix-only parametrization; move the masking/escaping fixtures (empty-value `LANGFUSE_SECRET_KEY`, masked-origin `LANGFUSE_BASE_URL`, escaping `LANGFUSE_PROJECT_ID` — lines 12, 30, 42-61, 101-103, 183-217) onto `PHOENIX_API_KEY`/`PHOENIX_COLLECTOR_ENDPOINT`/`PHOENIX_PROJECT_NAME` so the masking coverage survives.
  - `tests/architecture/test_local_phoenix.py:28`: drop `EAP_LANGFUSE_ENABLED=false\n` from the fixture .env (it would now fail as unknown).
  - `tests/architecture/test_phoenix_dev.py:36`: keep a credential-exclusion assertion — if it asserts `LANGFUSE_SECRET_KEY` exclusion, keep it (the scrub stays) or substitute another credential name consistently with the tools/phoenix_dev.py decision.
  - `tests/unit/test_phoenix.py`: remove `EAP_LANGFUSE_*`/`RMPREP_LANGFUSE_*` from `clean_environment` (35-38); delete `test_backend_selection_rejects_two_enabled_destinations` (113-125).
  - Delete `tests/unit/test_langfuse.py` (coverage ported in Task 1).
  - `docs/guide/guide.src.html`: remove the langfuse code-walkthrough hunks — storage-table row (~425), config-explain tracing mention (~908 → Phoenix-only wording), "Phoenix or Langfuse trace" (~1869 → "Phoenix trace"), and the whole Langfuse-integration walkthrough block (~1877-1918). Leave the rest of the guide for Tasks 8/13.
  - `docs/guide/build_guide.py`: if the langfuse.md walkthrough exclusion (790-795) breaks once nothing references it, leave it until Task 4 (it's inert); remove only if trivially safe.
- [ ] **Step 3: Regenerate all three artifacts** (`python -m ai_agent_lib_core.config` subcommands per `config/__main__.py`, then `docs/guide/build_guide.py`).
- [ ] **Step 4: Verify.** `grep -rn "langfuse" packages/ai-agent-lib-core/src --include="*.py" -il` → empty. Full suite, ruff, mypy, lint-imports, `pytest tests/architecture -q`. Expected test fallout you must chase to zero: any fixture .env in any test still carrying `EAP_LANGFUSE_*` now fails as unknown-variable — grep the test tree for `EAP_LANGFUSE` and clean every hit.
- [ ] **Step 5: Commit** `core: remove the langfuse integration; phoenix is the tracing backend`.

### Task 4: CLI generated surface goes Phoenix

**Files:**
- Delete: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/agent/docs/langfuse.md.jinja`
- Create: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/agent/docs/tracing.md.jinja`
- Modify: `templates/agent/README.md.jinja` (langfuse links → tracing doc / phoenix wording, lines ~9-11, 101-103), `templates/agent/tests/test___package___tracing.py.jinja` (if it references langfuse — read it), `packages/ai-agent-lib-cli/tests/unit/test_cli_commands.py` (file-set pin ~235: `docs/langfuse.md` → `docs/tracing.md`), `docs/guide/build_guide.py` (walkthrough exclusion rename, ~790-795)
- Delete + Create: `packages/ai-agent-lib-cli/tests/generated/test_langfuse_agents.py` → `test_phoenix_agents.py`

**Interfaces:**
- Produces: generated agents document Phoenix tracing (`docs/tracing.md`), and the generated-agent end-to-end tracing test runs on `PhoenixTracing`.

- [ ] **Step 1: Write `docs/tracing.md.jinja`** — a slim per-agent Phoenix quick-start: enable with `EAP_PHOENIX_ENABLED=true` in the service `.env` (tracing flows through `run_agent` — no code), the `var.phoenix_*` variable table (consume the labels `envfiles.variable_names()` now emits), where the trace link appears in responses, the content-capture caveat (`EAP_PHOENIX_CAPTURE_CONTENT` defaults false), and a pointer: local server via the library repo's `start-phoenix` launcher or a team-hosted collector over HTTPS + `PHOENIX_API_KEY`. Model the voice on the deleted `langfuse.md.jinja` (read it from git: `git show HEAD~1:packages/.../langfuse.md.jinja` if already deleted upstream — it is not; read it before deleting).
- [ ] **Step 2: Port `test_langfuse_agents.py` → `test_phoenix_agents.py`**: generated agent + InMemorySpanExporter via `PhoenixTracing`; keep the end-to-end coverage (JSON `/invoke` + SSE turns, trace ids in response envelopes, session correlation across two turns on one thread); assert Phoenix attribute keys (`session.id`, `agentlib.request_id` — read `_openinference.py` for exact names); assert the generated `.env.example` contains the `PHOENIX_VARIABLES` names and that `docs/tracing.md` renders with real values.
- [ ] **Step 3: README.md.jinja + tracing test template edits; file-set pin update; build_guide exclusion rename** (the generated workspace's companion-doc machinery: confirm `walkthrough_files()` handles the rename — read it).
- [ ] **Step 4: Run** `.venv/bin/python -m pytest packages/ai-agent-lib-cli -q` (generated suites render everything), plus `tests/architecture/test_generated_docs.py` (regenerate the guide if the walkthrough rename changed it). Full suite. Commit `cli: generated agents document and test phoenix tracing`.

## Phase 2 — rmprep finishes migrating

### Task 5: `app_extras` hook on the composition

**Files:**
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/http/composition.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_http_composition.py`

**Interfaces:**
- Produces: `AgentService.app_extras: Callable[[ServiceContainer, Callable[[], Any]], Mapping[str, Any]] | None = None` — called inside `build_agent_app` AFTER `lifecycle.prepare` is registered, with the container and a zero-arg getter returning the prepared graph; the returned mapping merges OVER `app_kwargs` (extras win). Docstring states: the getter is valid once readiness has passed, which every request implies.
- Consumed by: Task 6 (rmprep's inspector).

- [ ] **Step 1: Failing tests** (model on the file's existing `_Running`/fakes style):

```python
async def test_app_extras_receive_the_container_and_the_prepared_graph(...) -> None:
    seen: list[object] = []

    def extras(services: ServiceContainer, graph: Callable[[], Any]) -> dict[str, Any]:
        seen.append(services)
        seen.append(graph)
        return {}
    # AgentService(..., app_extras=extras); build app; after readiness, the
    # recorded getter returns the same graph object respond received.


async def test_app_extras_win_over_app_kwargs(...) -> None:
    # app_kwargs={"live_updates"-adjacent safe kwarg...} — use a real agent_app
    # kwarg observable from behavior or a sentinel the fakes can see; simplest:
    # pass admission= via both and assert the extras' instance is the one that
    # rejects a second concurrent request (or assert via a stub agent_app seam
    # consistent with existing tests).
```

Pick observable kwargs the existing composition tests already exercise (`outcomes`, `admission`); assert identity of the winning instance.

- [ ] **Step 2: Implement** — in `build_agent_app`, after the `prepare` registration and closures exist: `extras = dict(service.app_extras(services, lambda: graph)) if service.app_extras else {}`; final kwargs = `{**dict(service.app_kwargs), **extras}`. Document on the field (Attributes) and in `build_agent_app`'s args.
- [ ] **Step 3: Run focused + full core suite; ruff/mypy/lint-imports; commit** `http: app_extras hook gives composition access to the container and graph`.

### Task 6: rmprep collapses onto run_agent

**Files:**
- Modify: `examples/rmprep/agents/rmprep/src/rmprep/service.py`
- Delete: `examples/rmprep/agents/rmprep/src/rmprep/langfuse_settings.py`, `src/rmprep/langfuse_trial.py`
- Modify: `examples/rmprep/agents/rmprep/pyproject.toml` (drop `[tool.ai-agent-lib.langfuse]`), `.env.example` (langfuse block → phoenix block mirroring `phoenix_env_example()`)
- Delete + Create: `examples/rmprep/agents/rmprep/tests/test_langfuse_trial.py` → `tests/test_phoenix_trial.py`
- Modify: `examples/rmprep/agents/rmprep/tests/test_context_settings.py` (entrypoint test), `examples/rmprep/tests/` if anything pins service shape (grep), `examples/rmprep/README.md` (langfuse wording ~23-28 → Phoenix; Task 12 does the rest)

**Interfaces:**
- Consumes: Task 2's `agent_tracing`/retype, Task 5's `app_extras`.
- Produces: rmprep `service.py` of roughly this shape (preserve ALL business code; read the current file first — these are the seams):

```python
def _outcomes(config: ServiceConfig) -> OutcomeJournal | None: ...  # unchanged

def _extras(services: ServiceContainer, graph: Callable[[], Any]) -> dict[str, Any]:
    # local-env LocalTurnInspector built exactly as build_app does today
    # (model-trace path + jsonl audit path + GraphCheckpointPreview(graph)),
    # returned as {"inspector": ...} only when deployment env is local.

def _build_graph_with(contexts: ContextBuilder) -> Callable[[ServiceContainer], Awaitable[Any]]:
    async def _build_graph(services: ServiceContainer) -> Any: ...  # today's graph build, contexts injected
    return _build_graph

SERVICE = AgentService(
    application=APPLICATION,
    build_graph=_build_graph_with(ContextBuilder(load_context_budget(ENV_FILE))),  # NO — see main(); SERVICE keeps a default
    ...
)

def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped; a malformed context budget fails before anything starts."""
    try:
        contexts = ContextBuilder(load_context_budget(ENV_FILE))
    except ConfigurationError as error:
        return report_error(f"{APPLICATION}-serve", error, details=False)
    return run_agent(dataclasses.replace(SERVICE, build_graph=_build_graph_with(contexts)), argv)
```

Design constraints that bind this task: fail-fast on a bad `RMPREP_CONTEXT_*` value is PRESERVED (budget load happens in `main` before `run_agent`; non-zero exit; listener never starts); module-level `SERVICE` must not load the budget at import time (no import side effects — give `SERVICE` a `build_graph` that constructs its own default `ContextBuilder` lazily inside the coroutine, OR a module-level default built in `main` only; builder picks the cleanest shape that keeps `build_app(services, lifecycle, ...)` compat working for tests). Prefer core `read_question` over the local `_question` unless a test pins the exact message (grep rmprep tests for the wording first; report which).

- [ ] **Step 1: Read** `rmprep/service.py` fully, `tests/test_context_settings.py`, `tests/test_rmprep_service.py` (the inspector-composition pin), and the current `build_app` shim contract.
- [ ] **Step 2: TDD the entrypoint change** — rewrite `test_context_settings.py`'s entrypoint test (~64-86): malformed `RMPREP_CONTEXT_*` in the env file → `main([])` returns non-zero AND a monkeypatched `rmprep.service.run_agent` was never called; a valid budget → `run_agent` called once and its `AgentService.build_graph` injects the loaded budget (assert via building the graph with the test's stub container if feasible, else assert the replace() wiring by calling the composed `build_graph` and checking the ContextBuilder's budget value surfaces — the existing oversize-rejection test through `build_app` (~89-108) keeps covering enforcement).
- [ ] **Step 3: Migrate service.py** per the shape above; delete `main`'s old body, `_serve`, `configure_logging`/`start_telemetry` imports (they move into run_agent's domain); keep `build_app` compat shim (tests at ~89-108 and the langfuse-trial port use it) — the shim drops `app_extras` and rebuilds `outcomes`/`inspector` from its own parameters as the template's shim does.
- [ ] **Step 4: Port the trial tests** → `test_phoenix_trial.py`: keep the Phoenix halves of the big parametrized test (3 roots/1 session/checkpoints under `agentlib.`/tools by MCP name/5 model spans/llm.token_count usage/content gating/local JSONL count); port exporter-closed-on-container-failure (monkeypatch `PhoenixTracing` + `EAP_PHOENIX_ENABLED=true` env through the NEW entry path — `run_agent`'s `_serve`; drive via `main` with monkeypatched `ServiceContainer`); port broken-exporter + concurrent threads. Drop the langfuse settings/alias tests (the loader coverage lives in core `test_phoenix.py`; the alias mechanism is deleted).
- [ ] **Step 5: Shim deletions + pyproject + .env.example** (phoenix block content mirrors `phoenix_env_example()` with rmprep-appropriate values, e.g. project name).
- [ ] **Step 6: Verify the collapse**: `grep -n "start_telemetry\|configure_logging\|langfuse" examples/rmprep/agents/rmprep/src/rmprep/service.py` → empty. `.venv/bin/python -m pytest examples/rmprep -q` green; full suite; ruff/mypy/lint-imports. Commit `examples: rmprep serves through run_agent with phoenix tracing`.

## Phase 3 — UI

### Task 7: Phoenix-only trace links with legacy tolerance; UI feature docs

All UI commands from `packages/ai-agent-lib-ui` with `../../.venv-ui/bin/python`.

**Files:**
- Modify: `src/ai_agent_lib_ui/contracts/traces.py`, `src/ai_agent_lib_ui/contracts/conversations.py` (RunRecord.trace before-validator) or the cleanest equivalent, `src/ai_agent_lib_ui/adapters/http.py` + `application/question_answer.py` (live-path tolerance via one shared helper), `src/ai_agent_lib_ui/integrations/data_layer.py` (label why-comment + close() docstring merge — audit items), `tests/test_trace_links.py`, `tests/test_browser_protocol.py`, `README.md` (UI)

**Interfaces:**
- Produces: `TraceLink.provider: Literal["phoenix"]`; a module-level helper in `contracts/traces.py` — `tolerant_trace(payload: object) -> TraceLink | None` ("a trace link that no longer validates degrades to None; the answer never fails for its link") — used by the `RunRecord.trace` `mode="before"` validator AND the live ingestion paths.

- [ ] **Step 1: Read** `contracts/traces.py`, the RunRecord definition, and both live ingestion sites (`adapters/http.py` ~136-153 region, `application/question_answer.py`, any `_failed_run` path) to find where trace payloads are parsed.
- [ ] **Step 2: Failing tests**: (a) stored-row tolerance — build a RunRecord JSON with a langfuse-shaped trace (provider "langfuse", `/project/x/traces/<id>` URL), `model_validate_json` succeeds with `trace is None`, answer intact; (b) live tolerance — an agent response envelope carrying the langfuse trace produces an accepted answer with no trace link (drive through the same seam the existing malformed-trace test uses — find `test_malformed_trace_never_discards_answer` and parallel it); (c) phoenix links still validate end to end (existing tests, now single-provider); (d) keep the malformed-URL hardening matrix on phoenix URLs; (e) `test_browser_protocol.py:886` drops the langfuse parameter.
- [ ] **Step 3: Implement** — narrow the Literal, drop the langfuse route branch and label entry; add `tolerant_trace` + wire both paths; simplify `data_layer.py:51-53` to the phoenix-only label WITH the why-comment ("local Phoenix ships embedded as Agent Lab's trace viewer; the link text names the product, not the backend"); merge the stray `close()` comment into its docstring.
- [ ] **Step 4: UI README** — add the missing feature docs: answer feedback (thumbs up/down beneath accepted answers → `feedback.jsonl` beside `conversations.sqlite`, append-only, `delete` refused) in/near the sign-in-and-chat section `docs/evals.md` points at; `export_interactions` as the online-evals bridge (point at docs/evals.md §4-5); the import-linter contracts note ("contracts/application framework-freedom is CI-enforced; run lint-imports from this directory") in the code-map section; code-map rows for `contracts/feedback.py`, `adapters/interactions.py` (+ the other missing adapters: events, documents, inspection); update the trace-link wording (Langfuse mention at ~175, 184 → Phoenix); POSIX command spellings alongside the Windows ones (match the root README's "Or on POSIX" convention).
- [ ] **Step 5: Run** full UI suite + mypy from the UI dir + UI lint-imports + root `ruff check packages/ai-agent-lib-ui`. Commit `ui: phoenix-only trace links tolerate legacy rows; document feedback and export`.

## Phase 4 — Configuration clarity

### Task 8: Local-first configuration docs, generated

**Files:**
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/config/reference.py`, `config/bindings.py`, `docs/guide/guide.src.html` (variables-lesson intro ~1726-1732), `README.md` (one sentence), `docs/guide/configuration_examples.py` (only if labels shift)
- Create: `tests/architecture/test_local_only_documentation.py`
- Regenerate: `docs/variables.md`, `.env.example`, `docs/developer-guide.html`

**Interfaces:**
- Produces: variables.md preamble + generated "Profile defaults" table (section | local default | AWS default); bindings `details` strings that name both sides per section and flag local-only; an architecture test asserting the `EAP_DEPLOYMENT_ENV` local-only prose matches `di/defaults.py`'s `local_only` registrations.

- [ ] **Step 1: `reference.py`** — in `render_reference` add the preamble paragraph (local-first contract: "With nothing set, every provider is local: files, SQLite, and in-process engines — no cloud account. `EAP_PROFILE=aws` switches every section to its AWS provider; any single `EAP_<SECTION>_PROVIDER` opts one section in.") and a generated Profile defaults table reusing the existing `_profile_defaults` resolution (sections from `PROFILE_DEFAULTS`); `render_env_example` header gains the one-line version. NOTE the layering constraint: `config` may not import `di` — the table carries local/AWS provider names only; local-only flagging lives in the bindings prose (next step).
- [ ] **Step 2: `bindings.py`** — `_SECTION_DETAILS` (~148-158): every section's detail names both sides and flags the local-only one, in the existing voice (AUDIT: "local `jsonl` writes a file the one-way guard keeps local-only; `firehose` is the AWS option." — match IDENTITY/POLICY's existing phrasing); `EAP_DATA_SOURCES` details (~327-331) add: `duckdb_csv` is the local-only kind; `rest` and `redshift_data` deploy. Fix TELEMETRY detail (~226): `configure_telemetry()` → `start_telemetry()` (name both if accurate — read `observability/otel.py` first).
- [ ] **Step 3: New architecture test** — parse the `EAP_DEPLOYMENT_ENV` binding's details for the local-only adapter list (or better: add a structured constant in bindings like `LOCAL_ONLY_SUMMARY: tuple[str, ...]` that the details string interpolates, and assert THAT) and compare against the names registered `local_only=True` in `ServiceProviders.default()` (tests may import both config and di). Write the test first, watch it pass against current truth, and make it fail by design if either side drifts (e.g. temporarily edit, revert).
- [ ] **Step 4: Guide intro rewrite** (~1726-1732): Local = the zero-config default on any machine; AWS = per-variable opt-in; link the profiles table and the one-way guard. Root README: one local-first sentence in "Run your first agent" intro.
- [ ] **Step 5: Regenerate all three artifacts; run** `tests/architecture -q`, full suite, ruff/mypy. Commit `config: state the local-first contract in the generated reference`.

## Phase 5 — Documentation wave

Voice models for all doc tasks: `docs/identity.md`, `docs/evals.md`, `packages/ai-agent-lib-aws/README.md`. Every edited doc's links must resolve (relative paths checked by hand or a quick script); dual-OS convention: state once per doc ("Commands are shown for Command Prompt; on macOS/Linux use the .sh launchers and forward slashes") instead of duplicating every block, EXCEPT where the root README's existing "Or on POSIX" pattern already governs.

### Task 9: Hub and front door (docs/README.md, README.md)

- [ ] **docs/README.md**: drop the Windows-framing opener (line 3); task-table rows for identity.md and http-thread-admission.md; "Find the code" rows for `evaluation/` (core), `integrations/http` composition (AgentService/run_agent), UI `contracts/`; POSIX-ize the per-component test-command table (dual-OS convention line + forward-slash variants); surface `update --plan` where upgraders look (the row/bullet already present per audit — promote it out of the doc-authors section); "Current behavior and design material" adds the two 2026-10-18 design records + frontend-contracts.md; remove the Langfuse table row (14) and "optionally Langfuse or Phoenix" (65 → Phoenix); add anchors/links for anthropic-example.md and verified-anthropic-run.md (a "learn by tracing a real run" row).
- [ ] **README.md**: state the dual-OS convention once at the top of "Run Agent Lab" and collapse the six "Or on POSIX" pairs into single blocks with both spellings OR the stated convention (builder judgment; keep it shorter than today); trim the verified-run numbers (~243-247) to the link; "Explore the repository" rows for Agent Lab (`packages/ai-agent-lib-ui`) and the evals guide; contributor section notes the coverage gate exists in CI; keep all Phoenix mentions. Verify no stale Langfuse references (grep).
- [ ] **Verify + commit** `docs: hub and front door catch up and read dual-OS` (run `tests/architecture/test_generated_docs.py` + link spot-check).

### Task 10: Tutorial and local ops (getting-started.md, local-startup.md)

- [ ] **getting-started.md step 8**: rewrite around the real eval loop — run `agentlib eval helper`, read the report, `--accept` to set the baseline, what the regression gate fails on, link docs/evals.md for judges/online; step 9 leads with `agentlib update --plan` then `--diff`; recipes table + "Where to read more" gain evals rows.
- [ ] **local-startup.md**: the dual-OS pass — state the convention once, give .sh spellings for the launcher commands (`./setup-local.sh --model anthropic`, `./start-local.sh`, `cat .agentlib/ui-rmprep/access-code`, `./stop-local.sh`, `./start-phoenix.sh`), fix the venv-layout table to name both layouts (`.venv/Lib/site-packages` on Windows, `.venv/lib/python3.11/site-packages` on POSIX) or drop the inner path; drop `EAP_LANGFUSE_ENABLED=false` from the recommended block (~112) and the Langfuse alternative paragraph (~293-294, 316); turn the four-chain settings-precedence prose into a table.
- [ ] **Verify + commit** `docs: tutorial evals and dual-OS local startup`.

### Task 11: The tracing guide, evals pointers, release scope, ADR

- [ ] **Delete** `docs/langfuse.md`, `docs/langfuse-trial.md`.
- [ ] **phoenix-trial.md → the tracing guide**: remove dual-destination framing (~51-52, 235) and langfuse comparisons (~35 env line, 258-259, 265-266); add a short "Generated services" section up top: `EAP_PHOENIX_ENABLED=true` in the service `.env` is the whole integration (run_agent wires it), with the manual `agent_tracing` composition as the by-hand appendix (rewrite ~254-266 accordingly); fold the dated verified-run tables into `<details>` consistently; add the server-side story per the design (team-hosted HTTPS collector + `PHOENIX_API_KEY` via task-definition secret; not the secrets provider; ADR 0004 note); dual-OS commands (`start-phoenix.sh`); privacy/capture-content section stays and gains the inlined rule evals.md will point at.
- [ ] **evals.md**: fix the `--accept` wording ("runs the evals and promotes the passing report; on an intentional regression it skips only the baseline gate"); privacy section inlines the model-trace rule (content-bearing files, keep private, test data locally) and points at phoenix-trial.md's capture-content section instead of langfuse.md.
- [ ] **release-scope.md**: line 12 → Phoenix wording; add a short "Since the October review" block (or extend the matrix): offline/online evals + baselines, feedback capture + export, AgentService/run_agent composition, UI import contracts, coverage gate, POSIX launchers, `update --plan`, and the Langfuse removal with its migration note ("a `.env` still setting `EAP_LANGFUSE_*` fails startup as an unknown variable — delete the lines; `LANGFUSE_*`/`RMPREP_LANGFUSE_*` are inert; `run_agent` now honors `EAP_PHOENIX_ENABLED`, capture-content off by default"); `update --plan` in the migration section.
- [ ] **ADR 0004 amendment**: amend in place with a dated addendum section (Context unchanged; Decision gains: Langfuse removed 2026-10-18, Phoenix the only agent-trace backend, single-loader rule restated for `resolve_phoenix_settings` across startup/doctor/explain/launcher, binding-table registration still future); docs/adr/README.md untouched (same file).
- [ ] **Sweep inbound links** to the two deleted docs: `grep -rn "langfuse" docs packages examples README.md --include="*.md" -l` → fix every live doc (core README handled in Task 12; UI README done in Task 7; rmprep README Task 6/12); frozen records stay.
- [ ] **Verify + commit** `docs: phoenix is the tracing guide; release scope records the removal`.

### Task 12: Package READMEs

- [ ] **core README**: scope mentions the service composition (`AgentService`/`run_agent` in `integrations.http`) and links docs/evals.md; `otel` extra description drops the Langfuse clause; the Langfuse paragraph (~35-37) becomes a Phoenix pointer (phoenix-trial.md); fix "with your virtual environment active" (~12) to the repo's default-Python convention; split the nine-noun scope sentence.
- [ ] **CLI README**: command table gains `eval --accept` (one line: runs evals, promotes the passing report to `evals/baseline.json`; later runs gate on it) and `update --plan` (true dry run) / `--diff`; collapse the three update sections into one; quickstart drops the `.venv\Scripts\python.exe` activation divergence (use the default-Python convention); keep the Toolbox/DI narrative and the proposals caveats verbatim.
- [ ] **AWS README**: the one Command-Prompt line gains the convention sentence; nothing else.
- [ ] **rmprep README**: code-map row for `service.py` ("entry point: SERVICE on the shared run_agent; context budget loads fail-fast in main"); reduce the validation-rules duplication with local-startup.md to one canonical copy + link (builder picks which side holds it); confirm no langfuse references remain.
- [ ] **Verify + commit** `docs: package readmes match the composition and eval story`.

### Task 13: Developer guide prose (principles + evals)

- [ ] **guide.src.html #principles**: add the composition-root exemplar — a short `AgentService`/`run_agent` passage with a ~15-line example (application + build_graph + respond → `run_agent(SERVICE)`), stating what the service owns vs what the runner owns (config, logging, telemetry, tracing, lifecycle, serving) and linking the cross-cutting table; one bridging sentence where "pipeline" first appears ("the specification calls these interceptor chains; the guide says pipeline for the same fixed-order mechanism"); link docs/http-thread-admission.md as a worked port/adapter example and move/copy release-scope's `ServiceProviders.register` example into the testable-extensions passage (leave release-scope's copy; a link suffices if duplication reads worse — builder's call).
- [ ] **guide.src.html #evals**: catch up with baselines (`--accept`, regression gate), judges/calibration one-liner, online evals (feedback → export → sampled scoring), all pointing at docs/evals.md for depth.
- [ ] **Regenerate the guide; run doc tests; full suite. Commit** `docs: guide principles carry the composition exemplar and current evals`.

## Phase 6 — Comments and docstrings

### Task 14: The audit's remaining fixes

Files/items not already fixed by Tasks 2-7 (verify each against the current tree first — some were absorbed):

- [ ] `di/container.py`: name the audit `tracing: true` option consistently with bindings (~122-123); de-crypt the "permission plan" comment (~800: say it shares `di/access.selected_models` with the access planner); define "legacy order" once (~796) as the Section-enum order predating declared dependencies, and make `providers.py` (~237, 347) reference that definition.
- [ ] `integrations/http/agent.py`: reword the admission journal note (~622-623: the journal also reserves stream request IDs — duplicates refused — but a streamed run's final answer is not recoverable); docstring on `_recorded_answer` (~341); the "unauthorized" entry in its code set (~359-364): run the outcome-journal tests, check `_caller`'s flow, and either comment why it is defensively listed or remove it WITH a test proving the path (prefer the comment if ambiguous); one sentence on `agent_app`'s docstring for the `x-request-id` header contract.
- [ ] `integrations/langgraph/_agent_tracing.py`: rename the `_ToolSpans` `trial` param/attr to `tracing` (private, no shim); reword "no shared last-trace state" (~84); comment the 65/64 over-fetch truncation probe (~341-367); unit-comment `_CONTENT_LIMIT` (characters of serialized JSON); name the JSONL sink in the class docstring (the `EAP_MODEL_TRACE_PATH` model-trace sink); one-line docstring on `_model_event`.
- [ ] `evaluation/online.py`: rewrite the module docstring — interactions are exported from what production already saved (the UI's `export_interactions`) and scored here; the append-only request-path artifacts are the UI store and the feedback log, not these files.
- [ ] `commands/check.py`: split the four-clause `--accept` marker sentence (optional readability item — do it).
- [ ] Run the touched packages' suites + ruff/mypy. Commit `chore: docstrings match the docs vocabulary`.

## Phase 7 — Verification

### Task 15: Whole-change verification

- [ ] Full main suite; UI suite + UI mypy + UI lint-imports; `ruff check .` + `format --check .`; repo mypy (known noise only); `lint-imports`; `tests/architecture -q`; `build_guide.py --check`; coverage-gate command locally.
- [ ] `grep -rni "langfuse" . --include="*.py" --include="*.jinja" --include="*.toml" --include="*.md" --include="*.html" --include="*.example" | grep -v -e "docs/design/" -e "docs/superpowers/" -e "docs/Specification" -e ".claude/" -e "code-structure-review"` → remaining hits are ONLY: the two decided scrub-tuple entries (tools/local_config_report.py, tools/phoenix_dev.py, each commented) and frozen verified-run/dated records if any. Anything else is a miss — fix it.
- [ ] Generate a fresh fake-model agent in a temp dir (README quickstart commands) → `agentlib check` passes; `agentlib doctor` shows a phoenix row and no langfuse row; the generated workspace contains `docs/tracing.md` and its `.env.example` carries the `PHOENIX_*` block.
- [ ] Confirm `docs/variables.md` has no Langfuse section, has the Phoenix section, the local-first preamble, and the profile-defaults table; `.env.example` carries the Phoenix block.
- [ ] Commit anything the sweep fixed; report.

# Release 0.1 architecture and specification traceability

The [October specification](Specification.md) describes the target design. The
matrix below maps the twelve review priorities to the current release and
resolves the specification's contradictions using the [decision
records](adr/README.md); [Since the October review](#since-the-october-review)
records what was added or removed afterward. Future concerns in the
specification are not claims that this release implements them.

For the current local developer workflow, use the [documentation map](README.md).
Agent Lab adds saved chat, response inspection, and memo views in a separate UI
package. The agent HTTP layer can keep a local terminal-outcome journal for
read-only recovery; this is narrower than distributed business-operation
idempotency. Optional [Phoenix tracing](phoenix-trial.md), the only agent-tracing
backend, is shared through core.
The [October code review](design/code-structure-review-2026-10-13.md) records the
three reliability fixes and further suggested improvements. HTTP
[thread admission](http-thread-admission.md) protects one application instance;
multiple workers require injected shared coordination.

| Review priority | Current decision and implementation | Regression evidence |
| --- | --- | --- |
| 1. Image contents (§16) | A Dockerfile-specific allowlist admits only the selected service's source, queries, registries, policies and dependency lock. Environment files, caches, audit logs and databases are excluded even inside admitted directories. | CLI `test_cli_deploy.py`: generated allowlist checks and an opt-in Docker test inspecting the filtered build context and scanning image layers for planted secret sentinels. |
| 2. AWS ownership (§6–7) | Registries contain definitions. `BuildResources` shares an owned AWS session only inside one container; injected sessions remain caller-owned. Clients close after dependent adapters. | AWS `test_aws_lifecycle.py`; session tests. |
| 3. Lifecycle (§6) | Each adapter has an isolated task for construction and cleanup; the container orders reverse teardown. Concurrent close callers share cleanup; cancellation waits for cleanup, then propagates. Startup, background and cleanup failures are reported. | Core `test_di_container.py`: cancellation during start/close, live AnyIO cancel scopes, background failures, concurrent close and task affinity; `test_http_startup.py` checks cancelled startup. |
| 4. Consumer dependencies (§3, §15) | Generated Anthropic agents request core's `anthropic` extra; Bedrock agents request AWS's `bedrock` extra. | `test_cli_review_fixes.py`; `tools/check_consumer.py` installs wheels and generated services in fresh environments without repository import paths. |
| 5. Safe generation (§15) | Values use Python/TOML serializers. OpenAPI identifiers are normalized separately from remote query names; collisions are reported. Python, TOML, JSON and YAML are parsed before writes. | CLI generation regression tests, including quotes, multiline text, Unicode, keywords, collisions and invalid-file preflight. |
| 6. Request budgets (§8) | Live `RequestContext` objects pin their accounting between calls. Only inactive entries are evicted. The cache may exceed its target while requests remain alive. | Core `test_limits.py`: competing requests, traffic pressure, separate invocations and garbage collection. |
| 7. Explicit dependencies (§2, §6) | `ServicePort[T]` and dependency declarations are additive. The container validates missing/ambiguous dependencies and cycles before constructing anything; runtime member checks happen immediately after construction. Legacy string registration/access remains supported. | Core DI tests and strict type checking. |
| 8. Framework compatibility (§10, §14) | Framework-neutral contracts remain independent of LangChain. The integration has a typed native-model boundary and rejects unsupported tool/structured operations. All three model adapters run a shared framework conformance suite. | `LangGraphModelProviderContract`: native invocation, tool binding, structured output and error propagation with scripted SDK replies; separate adapter tests cover transport decoding. |
| 9. Composition (§4) | `RequestAuthenticator` and `McpBindings` own authentication and MCP assembly. `ServiceContainer` remains the lifecycle owner and stable public facade. | Existing identity, MCP, HTTP and graph integration tests exercise the same public methods. |
| 10. Readiness (§6, §16) | Generated agents register graph preparation with `ServiceLifecycle.prepare`. Their listener opens before adapter startup, validation and MCP discovery; `/readyz` stays unavailable until all finish. | Reference-agent service tests hold discovery open or fail it and check health/readiness. Serving tests exercise listener ordering. |
| 11. Supported configurations (§17) | Python/pip is the normal workflow. Unit gates run all extras on Windows/Linux; clean consumers test minimal core, minimal AWS, CLI, and generated fake/Anthropic/Bedrock agents against minimum/latest supported framework dependencies. Optional database schemas load without drivers. | CI workflow, `tools/framework-minimum.txt`, consumer script, schema/portable-path tests and import-boundary checks. Docker/OPA/Terraform/cloud tests remain opt-in. |
| 12. Specification (§1–19) | This matrix and the ADRs distinguish release behavior from the target design and record intentional deviations. | Architecture tests validate current operational docs; the target specification is excluded from the current environment-variable inventory. |

## Since the October review

The matrix above records the review priorities and their regression evidence.
These changes landed afterward; each is documented where it is used and covered by the tests named.

| Change | Current behavior | Regression evidence |
| --- | --- | --- |
| Offline and online evals, with baselines | `ai_agent_lib_core.evaluation` scores a hand-written dataset, or sampled production interactions, and gates the result with `report.require` and `require_no_regression`. `agentlib eval` runs a service's evals; `--accept` runs them itself and, if they pass, saves the report as `evals/baseline.json`. On an intentional regression it skips only the baseline gate. See [Measure an agent with evals](evals.md). | Core `test_evaluation*.py`; CLI `test_cli_check.py`. |
| Feedback capture and interaction export | Agent Lab appends thumbs up/down on accepted answers to `feedback.jsonl`. `export_interactions` turns saved conversations, joined with that feedback, into JSONL for online scoring. It is a Python entry point, not yet a CLI command. | UI `test_feedback.py`, `test_interactions_export.py`. |
| `AgentService` and `run_agent` composition | One composition root wires configuration, tracing, lifecycle, readiness and serving around an agent's graph. Generated agents, accounts-agent and rmprep start through it. `app_extras` adds app options that need the live container. | Core `test_http_composition.py`. |
| Agent Lab import contracts | import-linter contracts keep the UI's contracts free of every other module in its package, keep its application layer free of frameworks and adapters, and let only its integrations module import the browser framework. | `lint-imports` over the contracts in the UI package's `pyproject.toml`. |
| Coverage gate | CI requires 90% branch coverage on core's `contracts`, `config`, `di` and `pipeline` packages. | `.github/workflows/ci.yml`. |
| POSIX launchers | `setup-local.sh`, `start-local.sh`, `stop-local.sh` and `start-phoenix.sh` run the same tools as their `.bat` twins on macOS and Linux. | `tests/architecture/test_local_launchers.py`. |
| `agentlib update --plan` | A true dry run: it previews every change an update would make and writes nothing. | CLI `test_cli_diagnostics.py`. |
| Phoenix is the only agent-tracing backend | Langfuse support is removed: its integration, settings, guides and trace-link provider. `agent_tracing` is the single composition name, and `run_agent` wires it. [ADR 0004](adr/0004-tracing-configuration.md) records the decision. | Core `test_phoenix.py` and `test_http_composition.py`; CLI `test_phoenix_agents.py`; `tests/architecture/test_config_containment.py`. |
| Library variables are spelled `AC2_*` | `AC2_` replaces `EAP_` as the prefix of every library-owned variable, including the secrets family (`AC2_SECRET_<NAME>`), the Phoenix settings and Agent Lab's reads. The generated reference, templates, examples and guides use it. The old spellings keep working for one minor release and warn; see the migration note below. | Core `test_config_legacy_prefix.py`, `test_phoenix.py` and `test_http_composition.py`; CLI `test_cli_deploy.py`; UI `test_agent_environment_prefix.py`; `tests/architecture/test_config_containment.py` and `test_local_dev.py`. |

**Migrating to the `AC2_` prefix.**

- Rename `EAP_` variables to `AC2_` wherever you set them: `.env` files, task
  definitions and `deploy.env`, shell profiles. Only the prefix changes
  (`EAP_MODEL_PROVIDER` is now `AC2_MODEL_PROVIDER`). The `agentlib` command, the
  `.agentlib/` state folder, the `agentlib.*/v*` schema identifiers, span
  attributes and the deploy resource-name defaults (such as `eap-audit`) keep
  their names.
- The old `EAP_*` spellings keep working for one minor release and warn. A
  service logs one `WARNING` line per name that names its replacement (for
  example `EAP_MODEL_ID is deprecated; use AC2_MODEL_ID instead`); `agentlib
  doctor` (as a passing `warning` check) and `agentlib config explain` print the
  same line, Phoenix settings included. This covers every spelling the
  library reads: the bound variables, the `SECRET_<NAME>` family, the Phoenix
  settings, Agent Lab's settings and the local launchers.
- A value set in both spellings is safe only when the values are equal: equal
  values coexist silently. Different values fail startup with a message that
  names both spellings, because guessing which one you meant is the hazard. The
  same rule holds across `.env` files and the process environment.
- Unknown names fail in either prefix. An `AC2_*` name the library does not define
  stops startup, and so does an `EAP_*` name that is not an old spelling of a
  defined variable (the message suggests the closest `AC2_` name). A typo or a
  variable that was removed is never silently ignored.
- `agentlib deploy` passes names through as your `deploy.env` writes them, so a
  legacy spelling reaches the task definition unchanged. The plan lists each one
  under "Deprecated spellings" with its `AC2_` replacement and says that the old
  spelling stops working after one minor release. A new `deploy.env` is generated
  with `AC2_` names.

**Migrating from Langfuse.**

- A service that still sets a Langfuse variable (its enable, content-capture, or
  env-file setting) with either the `EAP_` or the `AC2_` prefix, in its `.env` or
  its process environment, fails startup with `unknown configuration variable`.
  Delete the lines, or unset the variables.
- Bare `LANGFUSE_*` and `RMPREP_LANGFUSE_*` names are inert: nothing reads them.
  Delete them too.
- `run_agent` now honors `AC2_PHOENIX_ENABLED` (spelled `EAP_PHOENIX_ENABLED` before
  the prefix change), so a generated service traces with no code.
  `AC2_PHOENIX_CAPTURE_CONTENT` defaults to `false`, so content stays out of traces
  unless you opt in. Before this release, generated agents and accounts-agent
  ignored the variable; only rmprep honored it. If `AC2_PHOENIX_ENABLED=true`, or
  the old `EAP_PHOENIX_ENABLED=true`, is already set in a service's environment
  (exported in a shell profile, or shared with the rmprep trial setup), that
  service starts exporting traces on upgrade with no `.env` change. Unset it where
  tracing is not wanted. See [Trace agents with Phoenix](phoenix-trial.md).
- Agent Lab conversations saved with a Langfuse trace link still open. The link is
  dropped and the answer and metrics are kept (UI `test_trace_links.py`).

## Using the new extension points

```python
from ai_agent_lib_core.contracts import AuditSink
from ai_agent_lib_core.kit import AUDIT, SECRETS, BuildContext, ServiceProviders


async def audit_factory(context: BuildContext) -> AuditSink:
    secrets = context.get(SECRETS)  # inferred as SecretsProvider
    token = await secrets.get_secret("audit_token")
    return MyAuditAdapter(token)


providers = ServiceProviders.default()
providers.register(AUDIT, "company", audit_factory, dependencies=(SECRETS,))
```

Factories declare singleton dependencies. Multiple named data sources and model
providers retain the existing `data_source` and `model_provider` accessors;
ambiguous singleton dependencies are rejected. Typed checks establish shape,
not behavior: custom adapters should also run the shipped contract suites.
Factory-created resources must clean up their own partially failed startup.
Ambient task-local side effects are not a dependency-injection interface.

## Installation and migration

Use your enterprise-approved Python and pip. Contributors run
`python tools/install_dev.py`; consumers install the CLI with pip and run
`python -m ai_agent_lib_cli install` inside a generated workspace. The local
batch setup keeps agent/MCP, Agent Lab, and Phoenix dependencies in separate
`.venv` package folders, loaded by the same default Python executable. Pip also
runs under that default Python; environment-local binaries are not used. See [local startup](local-startup.md) for model selection and setup.
Pip retains your team's package-source settings; an organization-provided
wheelhouse supports offline installation.
Native extensions still require a wheel compatible with that Python
and platform; pip cannot replace a missing platform wheel with pure Python.

When upgrading generated projects, run `agentlib update --plan` first. It lists
every file the update would replace or create and every file it would leave alone
because you edited it, and it writes nothing. Then run `agentlib update --diff`,
which preserves the files you edited. Existing deployment Dockerfiles are user-owned: review
the new template alongside the generated `Dockerfile.dockerignore`, replace the
old uv build steps, and create the per-service `requirements.lock` using the
deployment README. Unmodified generated files can be updated normally. The
runtime dependency lock is hash-checked; source-build requirements are resolved
separately by pip's isolated build environment. Optional uv metadata is retained
for teams already using it.

## Target-design work outside this release

| Specification area | Release status |
| --- | --- |
| §1 authentication non-goal versus §9 verification | The library verifies configured bearer credentials at its boundary and can exchange credentials. It does not host user sign-in, an identity directory or an authorization server. |
| §3/§15 Copier and workspace format | Scaffolding uses the existing Jinja renderer, `agentlib.toml` answers and JSON `agentlib.lock` hashes. No Copier dependency or second upgrade system is introduced. |
| §3 extras | Package `pyproject.toml` files are authoritative. OPA's HTTP client needs no `opa` extra; running the external OPA binary is optional. Bedrock/PostgreSQL are AWS extras. |
| §8 shared/distributed budgets | Budgets are process-local and tied to live request contexts. This is not a distributed spending or billing ledger. Token totals use reported usage after calls; they are not token reservations. |
| §7/§18 approvals, idempotency, long-term memory and deferred concern ports | Future design. This change does not introduce those services or claim their guarantees. |
| §16 deployment | Templates produce reviewable deployment artifacts. Desktop code never provisions infrastructure. Image, Terraform, OPA and live-cloud checks run only when explicitly selected on suitable hosts. |
| §19 platform choices | Organization-specific identity, regional models, storage retention and infrastructure ownership remain deployment decisions. |

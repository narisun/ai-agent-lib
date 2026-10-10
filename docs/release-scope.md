# Release 0.1 architecture and specification traceability

The [October specification](Specification.md) describes the target design. This
matrix defines the current release and resolves its contradictions using the
[decision records](adr/README.md). Future concerns in that document are not
claims that this release implements them.

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

Create a virtual environment with `python -m venv .venv`. In an activated
environment, contributors run `python tools/install_dev.py`; consumers install
the CLI with pip and run `python -m ai_agent_lib_cli install` inside a generated
workspace. If activation is restricted, use the environment's Python executable
directly. Pip obtains Python wheels; no separate uv or Docker installation is
required. Offline installation is possible with an organization-provided pip
wheelhouse. Native extensions still require a wheel compatible with that Python
and platform; pip cannot replace a missing platform wheel with pure Python.

Run `agentlib update --diff` when upgrading generated projects. It preserves
developer-modified files. Existing deployment Dockerfiles are user-owned: review
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

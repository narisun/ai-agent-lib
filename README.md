# ai-agent-lib

Build Python agents and shared tools with consistent configuration, permissions,
audit records, and tests. You write the application logic; ai-agent-lib connects
it to models, data sources, and the services around each request.

An **agent** uses a language model to answer questions and choose tools. A
**tool** is a function it can call. LangGraph runs the agent's steps; MCP
(Model Context Protocol) lets a separate server expose tools to several agents.

Start with the [getting-started tutorial](docs/getting-started.md). The
[developer guide](docs/developer-guide.html) explains the concepts, architecture,
troubleshooting, and configuration reference in more detail.

## Run your first agent

You need Python 3.11 or newer with pip, a copy of this repository, and access
to your approved package index or a folder of pre-downloaded Python wheels.
Local development needs no Docker, uv, cloud account, or administrator rights.
After installation, the default example and tests run without external services.

Open a terminal in the repository's top-level `ai-agent-lib/` folder. Choose
the commands for your terminal below. They create a virtual environment in
the library checkout and a separate application workspace named `tutorial/`
beside it. Use a different workspace name if that folder already exists.

A **virtual environment** keeps project packages separate from system Python.
These commands use its Python executable directly, so script activation is
unnecessary. If you already created this tutorial using the developer guide,
continue with [the next steps](docs/getting-started.md#2-understand-your-project).

### Windows PowerShell

```powershell
$Library = (Get-Location).Path
python -m venv .venv
$Python = Join-Path $Library ".venv\Scripts\python.exe"
& $Python -m pip install -e ./packages/ai-agent-lib-core -e ./packages/ai-agent-lib-cli
& $Python -m ai_agent_lib_cli init tutorial --dir .. --lib-path $Library --owner learning
Set-Location ../tutorial
& $Python -m ai_agent_lib_cli new agent helper --description "A practice assistant" --model fake
& $Python -m ai_agent_lib_cli install
& $Python -m pytest
& $Python -m helper "Say hello to Ada."
```

`$Python` holds the executable's full path; PowerShell's `&` runs it. If your
Python installation uses `py -3.11`, use `py -3.11 -m venv .venv` for the
environment creation step.

### macOS or Linux, using bash or zsh

```bash
LIBRARY="$PWD"
python3 -m venv .venv
PYTHON="$LIBRARY/.venv/bin/python"
"$PYTHON" -m pip install -e ./packages/ai-agent-lib-core -e ./packages/ai-agent-lib-cli
"$PYTHON" -m ai_agent_lib_cli init tutorial --dir .. --lib-path "$LIBRARY" --owner learning
cd ../tutorial
"$PYTHON" -m ai_agent_lib_cli new agent helper --description "A practice assistant" --model fake
"$PYTHON" -m ai_agent_lib_cli install
"$PYTHON" -m pytest
"$PYTHON" -m helper "Say hello to Ada."
```

**Expected result:** the tests pass and the agent prints:

```text
fake: Say hello to Ada.
```

The fake model repeats your question. It does not decide to call tools.
The tutorial adds a scripted test that tells the model which tool to call,
then shows how to configure a real model when you are ready.

`--lib-path` connects the workspace to this library checkout. `install` uses
the current Python's pip to install the workspace services and development
tools. Run it again after adding a service or changing its dependencies.

Continue with [adding a tool and testing it](docs/getting-started.md#3-add-a-tool-test-first).
Keep the terminal open: the tutorial reuses the Python variable and workspace.

## Where to make a change

Your generated application lives in `tutorial/`. The library lives in
`ai-agent-lib/`. They have different responsibilities:

| You want to… | Start here |
| --- | --- |
| Change what a tool does | `tutorial/agents/helper/src/helper/tools.py` |
| Change the agent's steps or prompt | `tutorial/agents/helper/src/helper/graph.py` |
| Write a behavior test | `tutorial/agents/helper/tests/` |
| Allow a specific action | `tutorial/policies/agentlib/rules/data.yaml`, with a sample in `tutorial/tests/policy-samples.yaml` |
| Choose a model or adapter | `tutorial/agents/helper/.env` and the service's `pyproject.toml` dependencies |
| Share tools through MCP | [Add an MCP server](docs/getting-started.md#5-add-a-shared-tool-server) |
| Change shared library behavior | The packages in this repository; see [contributing](#working-on-the-library) below |

An **adapter** implements a service, such as a model or a data source.
Application code receives services through `ServiceContainer`, rather than
creating cloud clients inside business functions. This is dependency
injection: tests can supply fakes through the same interfaces.

## Run and check the application

From `tutorial/`, in the same PowerShell terminal:

```powershell
& $Python -m ai_agent_lib_cli doctor helper
& $Python -m ai_agent_lib_cli check
& $Python -m ai_agent_lib_cli run helper
```

On macOS/Linux, replace `& $Python` with `"$PYTHON"`. In a new terminal,
[restore the interpreter variable](docs/getting-started.md#using-a-new-terminal)
before running commands.

- `doctor` checks the configured services. With cloud adapters, it can contact
  external services.
- `check` runs lint, formatting, type checks, tests, and policy samples.
- `run helper` starts an HTTP service. It keeps running until you press Ctrl+C.
  To ask one question from the command line instead, use
  `& $Python -m helper "Say hello to Ada."`.

The [HTTP example](docs/getting-started.md#6-optional-call-the-agent-over-http)
uses Python's standard library to send a request. The service exposes
`/healthz` for liveness and `/readyz` for readiness. Readiness waits for
service validation and graph/MCP preparation. Streaming sends graph-step
updates, not individual model tokens.

## Explore the repository

| Path | What it contains |
| --- | --- |
| [Core package](packages/ai-agent-lib-core/README.md) | Contracts, configuration, service composition, local adapters, model/tool checks, framework integrations, and test helpers |
| [AWS package](packages/ai-agent-lib-aws/README.md) | AWS adapters and their optional dependencies |
| [CLI package](packages/ai-agent-lib-cli/README.md) | The `agentlib` command, also available as `python -m ai_agent_lib_cli` |
| [Reference agent](examples/accounts-agent/README.md) | A working LangGraph agent |
| [Reference MCP server](examples/accounts-mcp/README.md) | Shared tools, named queries, and sample CSV data |
| [Policies](policies/README.md) | Rules and the OPA policy bundle |
| [Architecture tests](tests/architecture) | Checks that enforce package boundaries and keep documentation current |
| [Configuration reference](docs/variables.md) | Supported environment variables |
| [Identity guide](docs/identity.md) | User identity and delegated service calls |

The repository root holds shared development settings; it is not an
installable distribution. The packages under `packages/` are installed
individually or through the pip-based helpers.

## Working on the library

Use this workflow when changing the library itself. Open a terminal at the
library root. If you followed the quickstart, return there with
`Set-Location $Library` in PowerShell or `cd "$LIBRARY"` in bash/zsh.

Create the virtual environment if it does not already exist, and select its
interpreter:

```powershell
# Windows PowerShell, from ai-agent-lib/
python -m venv .venv
$Python = Join-Path (Get-Location).Path ".venv\Scripts\python.exe"
& $Python tools/install_dev.py
```

```bash
# macOS/Linux, from ai-agent-lib/
python3 -m venv .venv
PYTHON="$PWD/.venv/bin/python"
"$PYTHON" tools/install_dev.py
```

`tools/install_dev.py` uses pip to install the local packages, examples,
optional dependencies, and development tools. Then run these checks from
the library root, replacing `& $Python` with `"$PYTHON"` on macOS/Linux:

```powershell
& $Python -m ruff check .
& $Python -m ruff format --check .
& $Python -m mypy
& $Python -c "from importlinter.cli import lint_imports; lint_imports()"
& $Python -m pytest
& $Python docs/guide/build_guide.py --check
```

Lint catches suspicious code patterns; mypy checks types. Import-linter
checks package dependency boundaries. Its Python entry point above runs the
same check as the `lint-imports` shortcut without relying on that shortcut
being on `PATH`.

Follow the [code and documentation conventions](docs/developer-guide.html#code-conventions)
when contributing. They explain where code belongs, how to name components, and
how to document ownership, failures, and examples without changing public APIs.

The default tests block connections to external hosts while allowing
loopback for local tests and Python's event loop. Tests that need OPA,
Terraform, Docker, a database, or cloud credentials are separate, opt-in
checks for suitable machines. See [CI](.github/workflows/ci.yml) and the
[release scope](docs/release-scope.md) for those checks and supported configurations.

When editing the developer guide, change `docs/guide/guide.src.html`, then
regenerate the HTML and run its tests:

```powershell
& $Python docs/guide/build_guide.py
& $Python -m pytest tests/architecture/test_generated_docs.py
```

Keep the generated HTML with the source change. Configuration reference
tables come from the binding definitions in
`packages/ai-agent-lib-core/src/ai_agent_lib_core/config/bindings.py`.

## Deployment and release scope

The [deployment walkthrough](docs/getting-started.md#10-optional-prepare-a-deployment)
generates AWS Fargate deployment files for review. Applying infrastructure
and building images belongs on a machine or CI runner with the required
tools and permissions; the desktop tutorial remains Python/pip-based.

The [specification](docs/Specification.md) describes the target design.
The [release scope](docs/release-scope.md) records what is implemented,
and the [architecture decisions](docs/adr/README.md) explain why. Check these
before depending on features such as human approvals, idempotency, long-term
memory, or distributed spending limits, which remain future work.

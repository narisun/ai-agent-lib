# agentcraft2-sdk

Build Python agents and shared tools with consistent configuration, permissions,
audit records, and tests. You write the application logic; agentcraft2-sdk connects
it to models, data sources, and the services around each request.

An **agent** uses a language model to answer questions and choose tools. A
**tool** is a function it can call. LangGraph runs the agent's steps; MCP
(Model Context Protocol) lets a separate server expose tools to several agents.

Choose the path that matches your task:

| I want to… | Start here | What I will have |
| --- | --- | --- |
| Test an existing agent in a browser | [Run Agent Lab](#run-agent-lab) | rmprep, its MCP server, chat, memo versions, and trace links |
| Build my first agent | [Run your first agent](#run-your-first-agent), then the [tutorial](docs/getting-started.md) | A generated Python workspace with runnable code and tests |
| Understand the design | [Developer guide: principles](docs/developer-guide.html#principles) | How LangGraph, interfaces, dependency injection, and environment providers fit together |
| Change the library or simulator | [Working on the library](#working-on-the-library) and the [code map](docs/README.md#find-the-code) | The right package, environment, and checks for a change |

The [documentation index](docs/README.md) separates setup instructions, reference
material, and design proposals. The current checkout includes Agent Lab as a local
development preview; use the [release scope](docs/release-scope.md) for support boundaries.

## Run Agent Lab

Agent Lab is the browser simulator for testing agents. Use your team's Python
3.11+ and pip setup, approved package index, and a writable repository checkout.
Open **Command Prompt** (Windows) or a **Bash terminal** (macOS/Linux) in the repository root.
Commands are shown for Command Prompt. On macOS/Linux, run the `.sh` launcher with
the same name and arguments, for example `./setup-local.sh --dev` for
`setup-local.bat --dev`.

**First time:** follow [local setup and model settings](docs/local-startup.md#1-set-up-once).
Use `setup-local.bat --model anthropic` or `setup-local.bat --model bedrock`,
then configure the selected provider in the root `.env`. If that file already
selects your model, `setup-local.bat` detects it. Phoenix tracing is optional.

All services use the same approved Python. Agent/MCP, Agent Lab, and Phoenix keep
separate `.venv` package folders. The launchers use the default Python and its
pip module, rather than executables inside those folders. The generated
`fake` model echoes questions for a basic connectivity check.
Setup installs runtime packages. Use `setup-local.bat --dev` to also install
the scaffolding CLI and the rmprep/Agent Lab development tools.

**Optional: enable Phoenix traces.** Install its packages once with the same
default Python and pip:

```bat
start-phoenix.bat setup
```

Add these settings to the root `.env`, keeping your model settings and API key:

```dotenv
AC2_TELEMETRY=off
AC2_PHOENIX_ENABLED=true
AC2_PHOENIX_CAPTURE_CONTENT=true
PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006
PHOENIX_PROJECT_NAME=rmprep-trial
```

Content capture records model messages and tool results; use approved test data.
`start-local.bat` will start Phoenix at <http://127.0.0.1:6006> and keep traces in
local SQLite storage. The pinned Phoenix 20.21.1 can print a SQLAlchemy
`Column-expression-level unary distinct()` warning; it is left visible and does not
block tracing ([details](docs/phoenix-trial.md#remaining-upstream-warning)). Follow the
[Phoenix guide recipe](docs/developer-guide.html#phoenix-local) to check a
conversation. The [verified three-turn run](docs/phoenix-trial.md#verified-local-run-october-16-2026)
confirmed Anthropic follow-ups, MCP calls, metrics and checkpoints.

**Each testing session:**

```bat
start-local.bat
```

Check the printed environment path, model and masked settings. Wait for **All
selected services are ready**, then open [Agent Lab](http://127.0.0.1:8765).
Read its login code in a second Command Prompt or Bash terminal at the repository
root (on macOS/Linux, `cat .agentcraft2/ui-rmprep/access-code`):

```bat
type .agentcraft2\ui-rmprep\access-code
```

Select **Choose agent → Start chat with rmprep**. With Anthropic configured, ask:

1. `Prepare a meeting memo for Jones Dairy Farm (JONE-0022) as of 2026-08-03.`
2. In the same chat: `Explain the covenant breaches and cash conversion cycle change. How was its wallet share calculated? Use the saved evidence.`

Text streams into a provisional draft, then the final answer shows token usage
and response time. **Memos** opens saved versions beside chat. With Phoenix enabled,
**Open trace in Agent Lab** opens the response's model input/output, MCP calls,
metrics and checkpoint references. **Logs** provides local diagnostic inspection.

When finished, run in the second Command Prompt or Bash terminal:

```bat
stop-local.bat
```

Wait for **Stopped gracefully**. Conversations, memo versions, traces and
checkpoints remain available next time you start. Restart after changing code
or settings. Ctrl+C in the launcher window also stops its stack.

The [local testing guide](docs/local-startup.md) includes expected answers, client
switching, older memos, startup checks and troubleshooting. To connect your own
agent, use the [Agent Lab package guide](packages/agentcraft2-ui/README.md).

## Run your first agent

You need Python 3.11 or newer with pip, a copy of this repository, and access
to your approved package index or a folder of pre-downloaded Python wheels.
The tutorial uses a fake model that runs in the Python process and local
providers for configuration, permissions, audit records, and conversation state.
Local is the default for every setting, so no cloud infrastructure is needed;
AWS is a per-variable opt-in described in the
[configuration reference](docs/variables.md).

Open a terminal in the repository's top-level `ai-agentcraft2-sdk/` folder. Choose
the commands for your terminal below. They use your existing Python and create
an application workspace named `tutorial/` beside the library checkout. Use a different workspace name if that folder already exists.

Packages install into the selected Python's configured location through its pip. If you already created
this tutorial using the developer guide,
continue with [the next steps](docs/getting-started.md#2-understand-your-project).

### Windows Command Prompt (cmd.exe)

In **Command Prompt**, run the commands one line at a time from the library
root. `set "PYTHON=..."` stores the Python command or executable path; `"%PYTHON%"` runs
it. Keep the quotes so paths containing spaces work.

```bat
set "LIBRARY=%CD%"
set "PYTHON=python"
"%PYTHON%" -m pip install -e ./packages/agentcraft2-core -e ./packages/agentcraft2-cli
"%PYTHON%" -m agentcraft2_cli init tutorial --dir .. --lib-path "%LIBRARY%" --owner learning
cd /d ..\tutorial
"%PYTHON%" -m agentcraft2_cli new agent helper --description "A practice assistant" --model fake
"%PYTHON%" -m agentcraft2_cli install
"%PYTHON%" -m pytest
"%PYTHON%" -m helper "Say hello to Ada."
```

If your team supplies a specific executable, set `PYTHON` to its full path.
Use `python -m pip`, so pip runs under that approved Python.

### macOS or Linux, using bash or zsh

```bash
LIBRARY="$PWD"
PYTHON=python3
"$PYTHON" -m pip install -e ./packages/agentcraft2-core -e ./packages/agentcraft2-cli
"$PYTHON" -m agentcraft2_cli init tutorial --dir .. --lib-path "$LIBRARY" --owner learning
cd ../tutorial
"$PYTHON" -m agentcraft2_cli new agent helper --description "A practice assistant" --model fake
"$PYTHON" -m agentcraft2_cli install
"$PYTHON" -m pytest
"$PYTHON" -m helper "Say hello to Ada."
```

**Expected result:** the tests pass and the agent prints:

```text
fake: Say hello to Ada.
```

The fake model echoes your question. The tutorial uses scripted model replies
to test tool calls, then shows how to configure a real model.

`--lib-path` connects the workspace to this library checkout. `install` uses
the current Python's pip to install the workspace services and development
tools. Run it again after adding a service or changing its dependencies.

Continue with [adding a tool and testing it](docs/getting-started.md#3-add-a-tool-test-first).
Keep the terminal open: the tutorial reuses the Python variable and workspace.

## Where to make a change

Your generated application lives in `tutorial/`. The library lives in
`ai-agentcraft2-sdk/`. They have different responsibilities:

| You want to… | Start here |
| --- | --- |
| Change what a tool does | `tutorial/agents/helper/src/helper/tools.py` |
| Change the agent's steps or prompt | `tutorial/agents/helper/src/helper/graph.py` |
| Write a behavior test | `tutorial/agents/helper/tests/` |
| Allow a specific action | `tutorial/policies/agentcraft2/rules/data.yaml`, with a sample in `tutorial/tests/policy-samples.yaml` |
| Choose a model or adapter | `tutorial/agents/helper/.env` and the service's `pyproject.toml` dependencies |
| Share tools through MCP | [Add an MCP server](docs/getting-started.md#5-add-a-shared-tool-server) |
| Change shared library behavior | The packages in this repository; see [contributing](#working-on-the-library) below |

An **adapter** implements an interface for a service, such as a model or a
data source. `ServiceContainer` builds the configured adapters and supplies
them to application code. This is dependency injection: the graph depends on
interfaces, so tests can supply fakes and deployments can select other providers.

## Run and check the application

From `tutorial/`, in the same Command Prompt window:

```bat
"%PYTHON%" -m agentcraft2_cli doctor helper
"%PYTHON%" -m agentcraft2_cli check
"%PYTHON%" -m agentcraft2_cli run helper
```

On macOS/Linux, replace `"%PYTHON%"` with `"$PYTHON"`. In a new terminal,
[restore the interpreter variable](docs/getting-started.md#using-a-new-terminal)
before running commands.

- `doctor` checks the configured services. With cloud adapters, it can contact
  external services.
- `check` runs lint, formatting, type checks, tests, and policy samples.
- `run helper` starts an HTTP service. It keeps running until you press Ctrl+C.
  To ask one question from the command line instead, use
  `"%PYTHON%" -m helper "Say hello to Ada."`.

The [HTTP example](docs/getting-started.md#6-optional-call-the-agent-over-http)
uses Python's standard library to send a request. The service exposes
`/healthz` for liveness and `/readyz` for readiness. Readiness waits for
service validation and graph/MCP preparation. Generated agents support live
model drafts through `POST /invoke` with `Accept: text/event-stream`, followed
by one accepted result. The separate `/invoke/stream` route sends graph-step
updates for debugging. See [live responses and metrics](packages/agentcraft2-ui/README.md#live-responses-and-metrics).

## Inspect model input, output, and conversation history

To inspect an agent's model calls, add this to its `.env` and restart:

```dotenv
AC2_MODEL_TRACE_PATH=.agentcraft2/model-trace.jsonl
```

The file is written under that service's `.agentcraft2/` folder. It records the
messages sent to the provider, tool definitions, responses, and retry attempts.
Capture is opt-in and retains conversation content; use test data and keep the
file private. Recognizable credentials are masked.

The [debugging Q&A](docs/developer-guide.html#faq) explains how to read the trace,
continue a conversation, and inspect checkpoint state. Its
[runnable ReAct example](docs/developer-guide.html#react-context) follows a tool
call and a second question through the generated graph using scripted replies.

Every new workspace also includes the [verified Anthropic walkthrough](docs/verified-anthropic-run.md),
selected-field trace samples, and a JSONL reader. The walkthrough follows a real
run call by call and shows how a tool result and the saved first turn reach the
model on the next call.

From your generated workspace, run the scripted example:

```bat
"%PYTHON%" -m helper.examples.trace_react
"%PYTHON%" examples/read_trace.py agents/helper/.agentcraft2/react-demo.jsonl --last 6
```

For a live run, follow [Anthropic setup](docs/anthropic-example.md) to add its
dependency extra and configure your model ID and API key. Then run:

```bat
"%PYTHON%" -m helper.examples.trace_anthropic
```

It sends two questions on the same thread, prints the model trace and checkpoint
snapshots, and accepts `--thread` to continue a saved conversation. Other agents
have these examples under their own package names; their READMEs give exact commands.

## Explore the repository

| Path | What it contains |
| --- | --- |
| [Core package](packages/agentcraft2-core/README.md) | Contracts, configuration, service composition, local adapters, model/tool checks, framework integrations, and test helpers |
| [AWS package](packages/agentcraft2-aws/README.md) | AWS adapters and their optional dependencies |
| [CLI package](packages/agentcraft2-cli/README.md) | The `agentcraft2` command, also available as `python -m agentcraft2_cli` |
| [Reference agent](examples/accounts-agent/README.md) | A working LangGraph agent |
| [Reference MCP server](examples/accounts-mcp/README.md) | Shared tools, named queries, and sample CSV data |
| [Relationship manager preparation](examples/rmprep/README.md) | Router, memo and Q&A nodes over 33 supplied CSVs, with governed MCP evidence, source references and checkpoints |
| [Agent Lab package](packages/agentcraft2-ui/README.md) | `packages/agentcraft2-ui`: the browser simulator with chat, memo versions, trace links, answer feedback, and online-eval export of saved chats |
| [Policies](policies/README.md) | Rules and the OPA policy bundle |
| [Architecture tests](tests/architecture) | Checks that enforce package boundaries and keep documentation current |
| [Configuration reference](docs/variables.md) | Supported environment variables |
| [Identity guide](docs/identity.md) | User identity and delegated service calls |
| [Evals guide](docs/evals.md) | Offline evals with baselines (`agentcraft2 eval`) and online scoring of sampled, saved chats |

The repository root holds shared development settings. Install the packages
under `packages/` using pip or the development helper below.

## Working on the library

Use this workflow when changing the library itself. Open a terminal at the
library root. If you followed the quickstart, return there with
`cd /d "%LIBRARY%"` in Command Prompt or `cd "$LIBRARY"` in bash/zsh.

Select your approved Python, then install the contributor dependencies:

```bat
rem Windows Command Prompt, from ai-agentcraft2-sdk/
set "PYTHON=python"
"%PYTHON%" tools/install_dev.py
```

```bash
# macOS/Linux, from ai-agentcraft2-sdk/
PYTHON=python3
"$PYTHON" tools/install_dev.py
```

`tools/install_dev.py` uses pip to install the local packages, examples,
optional dependencies, and development tools. Then run these checks from
the library root, replacing `"%PYTHON%"` with `"$PYTHON"` on macOS/Linux:

```bat
"%PYTHON%" -m ruff check .
"%PYTHON%" -m ruff format --check .
"%PYTHON%" -m mypy
"%PYTHON%" -c "from importlinter.cli import lint_imports; lint_imports()"
"%PYTHON%" -m pytest
"%PYTHON%" docs/guide/build_guide.py --check
```

Ruff checks code style and formatting, mypy checks types, and import-linter
checks package dependency boundaries. Pytest runs the tests; the final command
checks that the generated developer guide matches its source.

Follow the [code and documentation conventions](docs/developer-guide.html#code-conventions)
when contributing. They explain where code belongs, how to name components, and
how to document ownership, failures, and examples.

The default tests block connections to external hosts while allowing
loopback for local tests and Python's event loop. Tests that need OPA,
Terraform, Docker, a database, or cloud credentials are separate, opt-in
checks for suitable machines. CI also gates coverage: the core `contracts`,
`config`, `di`, and `pipeline` packages must keep 90% branch coverage. See
[CI](.github/workflows/ci.yml) and the
[release scope](docs/release-scope.md) for those checks and supported configurations.

When editing the developer guide, change `docs/guide/guide.src.html`, then
regenerate the HTML and run its tests:

```bat
"%PYTHON%" docs/guide/build_guide.py
"%PYTHON%" -m pytest tests/architecture/test_generated_docs.py
```

Keep the generated HTML and companion Markdown/sample files with the source change.
The walkthroughs and trace reader come from the CLI templates, so the library
docs and newly generated tutorials stay in sync. Configuration reference
tables come from the binding definitions in
`packages/agentcraft2-core/src/agentcraft2_core/config/bindings.py`.

## Deployment and release scope

The [deployment walkthrough](docs/getting-started.md#10-optional-prepare-a-deployment)
uses the Python CLI to generate AWS Fargate deployment files for review.
To build images and apply infrastructure, use a build machine or CI runner
with a container engine, Terraform, and the required registry and AWS permissions.

The [specification](docs/Specification.md) describes the target design.
The [release scope](docs/release-scope.md) records supported behavior and
operational limits. The [architecture decisions](docs/adr/README.md) explain
the design choices.

For Agent Lab's current setup and features, use its
[package guide](packages/agentcraft2-ui/README.md). The
[documentation index](docs/README.md#current-behavior-and-design-material) links
the frontend design, implementation plan, and original Chainlit trial separately.

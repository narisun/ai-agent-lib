# Getting started

Choose the walkthrough for your task:

| Task | Start here |
| --- | --- |
| Test rmprep in Agent Lab with Anthropic and Phoenix | [Local startup and testing](local-startup.md): setup, root `.env`, login, memo/follow-ups, traces, and stop/restart. |
| Build your own agent and tools | Continue below to create the `tutorial` workspace. |

The batch scripts run the checked-in rmprep example. This tutorial uses the
generated agent's service `.env` and Python commands. Use the
[documentation map](README.md) for configuration and contributor references.

Build an agent named `helper`, add a tool, and test its behavior and
permissions. Then add shared tools through an MCP server. This walkthrough
uses the same `tutorial/` workspace as the [developer guide](developer-guide.html).
If you started in the README or developer guide, continue with the next
unfinished step in that workspace.

This tutorial assumes familiarity with Python functions, classes, imports,
and tests. Library terms are explained as you encounter them. For the
design behind the code, read the guide's
[introduction to principles and patterns](developer-guide.html#principles).

Follow steps 1–5 for the local walkthrough. After that, choose what you need:
[HTTP](#6-optional-call-the-agent-over-http),
[your own data](#7-optional-generate-tools-over-your-own-data),
[a real model](#8-optional-use-and-evaluate-a-real-model), or
[deployment](#10-optional-prepare-a-deployment). If a step fails, see
[troubleshooting](#when-something-goes-wrong).

## What you need

- Python 3.11 or newer with pip. Check with `python --version` and
  `python -m pip --version`. On macOS/Linux, the command may be `python3`;
  on Windows, it may be `py -3.11`.
- A copy of this repository. Open a terminal in its top-level `ai-agent-lib/`
  folder, where you can see `packages/` and `docs/`.
- Access to your approved package index, or a team-provided wheelhouse
  (a folder of pre-downloaded Python packages).

On Windows, use **Command Prompt (cmd.exe)**. The local walkthrough runs with
Python and pip. It uses a fake model, sample data, and local providers for
permissions, audit records, and conversation state. Each optional section
lists the additional access or tools it needs.

## 1. Create and run an agent

An **agent** uses a language model to answer questions and choose tools.
A **tool** is a Python function the agent can call. Start with one agent
and its built-in greeting tool.

Choose the commands for your terminal. They use your existing Python and
create an application workspace named `tutorial/` beside the library checkout.
Pip installs into your Python's configured package location. Use a different
workspace name if that folder already exists.

### Windows Command Prompt (cmd.exe)

In **Command Prompt**, run these commands one line at a time from the library
root. Keep this window open for the following steps:

```bat
set "LIBRARY=%CD%"
set "PYTHON=python"
"%PYTHON%" -m pip install -e ./packages/ai-agent-lib-core -e ./packages/ai-agent-lib-cli
"%PYTHON%" -m ai_agent_lib_cli init tutorial --dir .. --lib-path "%LIBRARY%" --owner learning
cd /d ..\tutorial
"%PYTHON%" -m ai_agent_lib_cli new agent helper --description "A practice assistant" --model fake
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m pytest
"%PYTHON%" -m helper "Say hello to Ada."
```

If your team supplies a specific executable, set `PYTHON` to its full path.
Use the same approved Python for installation, tests, and startup.

### macOS or Linux, using bash or zsh

Run from the library root and keep this terminal open:

```bash
LIBRARY="$PWD"
PYTHON=python3
"$PYTHON" -m pip install -e ./packages/ai-agent-lib-core -e ./packages/ai-agent-lib-cli
"$PYTHON" -m ai_agent_lib_cli init tutorial --dir .. --lib-path "$LIBRARY" --owner learning
cd ../tutorial
"$PYTHON" -m ai_agent_lib_cli new agent helper --description "A practice assistant" --model fake
"$PYTHON" -m ai_agent_lib_cli install
"$PYTHON" -m pytest
"$PYTHON" -m helper "Say hello to Ada."
```

**Expected result:** the tests pass and the final command prints:

```text
fake: Say hello to Ada.
```

The fake model echoes your question. In step 3, scripted model replies will
exercise a tool call and verify its result.

Here is what the setup commands did:

| Command or option | Purpose |
| --- | --- |
| `python -m pip install -e …` | Installs the library packages in editable mode, so source changes are available as you edit them |
| `init tutorial` | Creates the application workspace |
| `--lib-path` | Selects this library checkout as the workspace's library source |
| `new agent helper` | Writes the agent, configuration, and tests |
| `install` | Uses the current Python's pip to install workspace services and development tools |
| `python -m pytest` | Runs the generated tests |
| `python -m helper` | Asks the generated agent one question |

Run `"%PYTHON%" -m ai_agent_lib_cli install` again whenever you add a service
or change its dependencies.

### Command convention for the rest of this tutorial

Run commands from `tutorial/`. Examples use Command Prompt's `"%PYTHON%"` prefix;
on macOS/Linux, replace it with `"$PYTHON"`. Both select the same approved
Python used during setup. `set "PYTHON=..."` stores the executable's path;
`%PYTHON%` reads that variable. Keep the surrounding quotes when running it
so paths containing spaces work. `cd /d` changes both the folder and drive.

### Using a new terminal

In each new terminal, open `tutorial/` and set the interpreter path:

```bat
rem Windows Command Prompt, from tutorial/
set "LIBRARY=%CD%\..\ai-agent-lib"
set "PYTHON=python"
```

```bash
# macOS/Linux, from tutorial/
LIBRARY="$(cd ../ai-agent-lib && pwd)"
PYTHON=python3
```

These paths assume your checkout is named `ai-agent-lib` and sits beside
`tutorial`. Adjust that folder name if needed.

## 2. Understand your project

You now have two folders: `ai-agent-lib/` contains the library;
`tutorial/` contains your application. Most changes to the agent belong in
the application workspace.

| Path inside `tutorial/` | What it is for |
| --- | --- |
| `agents/helper/src/helper/tools.py` | Tool functions |
| `agents/helper/src/helper/graph.py` | The agent's steps, prompt, and model/tool connections |
| `agents/helper/src/helper/service.py` | HTTP startup and request handling |
| `agents/helper/tests/` | Tests of the agent and service |
| `agents/helper/.env` | Local settings; keep credentials out of Git |
| `agents/helper/.env.example` | Shareable settings used by the generated configuration tests |
| `agents/helper/pyproject.toml` | This service's dependencies |
| `registry/` | Registered agents and MCP tools |
| `policies/agentlib/rules/data.yaml` | Rules that decide which actions are allowed |
| `tests/policy-samples.yaml` | Requests the rules should allow or deny |
| `agentlib.toml` | The answers used to generate the workspace |
| `agentlib.lock` | Generated-file hashes used to preserve your edits when updating templates |

A **graph** is a set of steps and the connections between them. LangGraph
runs these steps. `ServiceContainer` supplies configured services to the
graph; `RequestContext` carries the caller, application, request ID, and
conversation ID. The [guide's glossary](developer-guide.html#idea) explains
these terms with a request walkthrough.

## 3. Add a tool, test first

Add `farewell`, which returns `Goodbye, Ada!`. Check three things separately:
the function works, the graph uses it, and the actual permission rule allows it.

### A. Write a test for the function

Create `agents/helper/tests/test_farewell.py`:

```python
from helper.tools import farewell


def test_farewell_trims_the_name() -> None:
    assert farewell("  Ada  ") == "Goodbye, Ada!"
```

```bat
"%PYTHON%" -m pytest agents/helper/tests/test_farewell.py
```

**Expected failure:** Python cannot import `farewell`, because you have not
written it yet.

### B. Implement the function and connect it

Append this function to `agents/helper/src/helper/tools.py`:

```python
def farewell(name: str) -> str:
    """Return a goodbye for a person, given their name."""
    return f"Goodbye, {name.strip()}!"
```

In `agents/helper/src/helper/graph.py`, replace the existing tools import:

```python
from helper.tools import farewell, greet
```

Inside `build_graph`, replace its `services.tools` line with the following.
Keep the indentation of the line you replace:

```python
tools = services.tools([greet, farewell], read_only=["greet", "farewell"])
```

`services.tools` wraps functions with permission checks, limits, and audit
recording. `read_only` says these functions do not change external state,
so a transient failure can be retried safely. Do not mark a payment or a
write operation read-only.

Run the function test again. It should pass.

### C. Add and test the permission

Append this entry to the existing `samples:` list in
`tests/policy-samples.yaml`, matching the indentation of the other entries:

```yaml
  - name: helper says goodbye
    action: tool.call
    application: helper
    resource: farewell
    expect: allow
    reason: helper-calls-its-own-tools
```

```bat
"%PYTHON%" -m ai_agent_lib_cli policy test
```

**Expected failure:** the new sample is denied because no rule names
`farewell` yet.

In `policies/agentlib/rules/data.yaml`, find the existing rule with
`id: helper-calls-its-own-tools`. Change its `resources` from `[greet]`
to `[greet, farewell]`. Edit that rule in place; do not add a duplicate.
It should look like:

```yaml
  - id: helper-calls-its-own-tools
    actions: [tool.call]
    applications: [helper]
    resources: [greet, farewell]
```

Run `"%PYTHON%" -m ai_agent_lib_cli policy test` again. It should pass.

### D. Check that the graph calls the tool

Create `agents/helper/tests/test_farewell_graph.py`:

```python
from ai_agent_lib_core import Principal, RequestContext
from ai_agent_lib_core.pipeline import frame_untrusted
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes, calls_tool

from helper import APPLICATION, ask


async def test_agent_uses_farewell() -> None:
    fakes = Fakes(
        model=FakeChatModelProvider([calls_tool("farewell", name="Ada"), "I said goodbye to Ada."])
    )
    context = RequestContext(
        principal=Principal(subject="learner", tenant="tutorial"),
        application=APPLICATION,
        request_id="farewell-test",
        thread_id="conversation-1",
    )

    async with fakes.container() as services:
        reply = await ask(services, context, "Say goodbye to Ada.")

    assert reply == "I said goodbye to Ada."
    second_call = fakes.model.models[0].calls[1]
    assert second_call[-1].content == frame_untrusted("farewell", "Goodbye, Ada!")
    assert [record.event for record in fakes.audit.records] == [
        "model.call",
        "tool.call",
        "model.call",
    ]
```

The scripted model requests the tool, receives its result, and gives the
final answer. Checking the second model call proves the result reached it.
`frame_untrusted` labels the tool's output as untrusted data for the model.

`async with` manages startup and cleanup of the test services. The generated
pytest configuration supports asynchronous tests.

The graph test uses fake permissions to isolate the workflow. The policy
sample in step C checks the actual rule. Run both to verify behavior and access:

```bat
"%PYTHON%" -m pytest agents/helper
"%PYTHON%" -m ai_agent_lib_cli policy test
"%PYTHON%" -m ai_agent_lib_cli check
```

**Expected result:** tests and policy samples pass. `check` also runs lint,
formatting, and type checks. If pasted code needs formatting, run
`"%PYTHON%" -m ruff format .` and retry.

## 4. Check and change configuration

An **adapter** implements a service, such as a fake model or a database
connection. Configuration selects adapters without changing the graph.
Your local settings are in `agents/helper/.env`:

```dotenv
EAP_PROFILE=local
EAP_DEPLOYMENT_ENV=local
EAP_MODEL_PROVIDER=fake
```

Check these entries and keep the file's other generated settings.

- `EAP_PROFILE` chooses defaults: `local` or `aws`.
- `EAP_DEPLOYMENT_ENV` says where this process runs: `local`, `dev`, or `prod`.
  Non-local environments reject development-only adapters.
- `EAP_MODEL_PROVIDER` chooses the model implementation. `fake` runs in process.

Process environment variables override `.env`; `.env` overrides profile
defaults. Relative file paths are resolved from the folder containing
`.env`. Restart a service after editing its settings.

```bat
"%PYTHON%" -m ai_agent_lib_cli config explain helper
"%PYTHON%" -m ai_agent_lib_cli config options model fake
"%PYTHON%" -m ai_agent_lib_cli doctor helper
```

`explain` shows values and their sources, with secrets masked. `options`
lists accepted keys and defaults. `doctor` builds and validates the selected
services; with cloud adapters, that can contact external services.

For JSON options, use double quotes around keys and strings, and keep the
value on one line. An unknown option produces a configuration error.

## 5. Add a shared tool server

MCP (Model Context Protocol) lets a separate server provide tools to agents.
Use it when tools or data access should be shared. A function inside the
agent is enough for a tool that only that agent needs.

From `tutorial/`, add a sample server named `directory`:

```bat
"%PYTHON%" -m ai_agent_lib_cli new mcp directory --description "The people directory"
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m ai_agent_lib_cli link helper directory
"%PYTHON%" -m pytest
"%PYTHON%" -m ai_agent_lib_cli policy test
```

The server lives in `mcp-servers/directory/`. It has sample CSV data, named
queries, tool functions, and tests. A **named query** defines the allowed query
in advance. The tool supplies values for its declared parameters.

`link` updates registrations and permissions and adds a test of the agent
and server together. That test manages an in-process server and uses a
scripted model to call its tools.

**Expected result:** the expanded test suite and policy samples pass.

After linking, start the server before asking the agent a question. In one
terminal, with the Python variable set and the working directory at `tutorial/`:

```bat
"%PYTHON%" -m ai_agent_lib_cli run directory
```

Wait for the server's `ready` log event. In a second terminal,
[set the Python variable](#using-a-new-terminal), then run:

```bat
"%PYTHON%" -m helper "Who is in the payments team?"
```

**Expected result:** the fake model prints `fake: Who is in the payments team?`.
The generated connection test verifies calls to the server's tools. Keep the
server running for the HTTP example, or press Ctrl+C to stop it.

When a tool's arguments change, review the change and update its **schema
pin**, the hash of its expected input definition:

```bat
"%PYTHON%" -m ai_agent_lib_cli registry pin directory
"%PYTHON%" -m ai_agent_lib_cli check
```

## 6. Optional: call the agent over HTTP

HTTP lets another program call the agent. If you completed step 5, keep
`directory` running in its own terminal. Start the agent in another:

```bat
"%PYTHON%" -m ai_agent_lib_cli run helper
```

Wait for the agent's `ready` log event. Save this script as `try_http.py` in
`tutorial/`. It sends a request using Python's standard library:

```python
import json
from urllib.request import Request, urlopen

with urlopen("http://127.0.0.1:8000/readyz", timeout=10) as response:
    print(response.read().decode())

body = {"input": {"question": "Say hello to Ada."}, "thread_id": "demo"}
request = Request(
    "http://127.0.0.1:8000/invoke",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
with urlopen(request, timeout=30) as response:
    print(json.load(response))
```

In a separate terminal with the interpreter variable restored, run:

```bat
"%PYTHON%" try_http.py
```

**Expected result:** readiness succeeds and the response includes an
`output` containing `fake: Say hello to Ada.`, plus `thread_id` and
`request_id`. A thread identifies a conversation; a request ID identifies
one call.

`/healthz` reports whether the HTTP listener is alive. `/readyz` reports
whether services and graph/MCP preparation are complete. A 503 readiness
response means you should check startup logs and required services.

Press Ctrl+C in each service terminal to stop it. For streaming graph-step
updates and authenticated requests, see the
[HTTP reference](developer-guide.html#service-flags).

## 7. Optional: generate tools over your own data

Choose one source below. The CLI proposes queries and generates a server
with tests. Before using real data, review which fields the tools return,
how those fields are classified, and which roles may access them.

### CSV files

For a small practice dataset, create `inputs/claims/claims.csv` in
`tutorial/` with these contents:

```csv
id,team,amount,email
1,payments,25,ada@example.com
2,support,40,lin@example.com
3,payments,15,sam@example.com
```

Preview the proposal, then generate the server:

```bat
"%PYTHON%" -m ai_agent_lib_cli new mcp claims-mcp --from-csv ./inputs/claims --propose
"%PYTHON%" -m ai_agent_lib_cli new mcp claims-mcp --from-csv ./inputs/claims --description "Tools over practice claims"
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m pytest mcp-servers/claims-mcp
"%PYTHON%" -m ai_agent_lib_cli doctor claims-mcp
```

`--propose` prints proposed tools for review. The second command copies the CSV
files into the server's `data/` folder and writes query definitions under
`queries/`. Each file is a table. `--per-table` limits how many tools are proposed;
repeat `--query` to keep selected query names from the proposal.

The generated tests check calls and permissions. Add assertions for the
results your application actually needs. For this dataset, inspect
`mcp-servers/claims-mcp/tests/test_claims_mcp.py` and the generated query files.

The agent can use this server after you link it:

```bat
"%PYTHON%" -m ai_agent_lib_cli link helper claims-mcp
"%PYTHON%" -m pytest tests
"%PYTHON%" -m ai_agent_lib_cli check
```

Start `claims-mcp` as well as `directory` before running the linked agent.
The generated tests manage their own in-process servers. If you later change
the tool arguments, review them and run
`"%PYTHON%" -m ai_agent_lib_cli registry pin claims-mcp`.

### A REST API described by OpenAPI

If you have an OpenAPI 3 document, save it as `inputs/rates.yaml` or replace
that path below with your document's path:

```bat
"%PYTHON%" -m ai_agent_lib_cli new mcp rates-mcp --from-openapi ./inputs/rates.yaml --propose
"%PYTHON%" -m ai_agent_lib_cli new mcp rates-mcp --from-openapi ./inputs/rates.yaml --description "Tools over the rates API"
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m pytest mcp-servers/rates-mcp
```

The CLI creates tools for supported GET operations returning JSON records.
Generation reads the OpenAPI document. Tests use local responses from
`tests/api_responses.json`; replace its sample responses with representative
test data. Read that server's README before configuring API authentication
or running diagnostics that contact the real service.

### Redshift tables

This path needs permission to read the Redshift database catalogue and AWS
credentials available to the Python AWS SDK. Run it on a development machine
or CI host configured with your team's approved AWS access.

The checkout-based `install` command includes the local AWS package. Run
the following after replacing `your-workgroup` and `your-profile` with your
approved values; adjust the schema and database names as needed:

```bat
"%PYTHON%" -m ai_agent_lib_cli new mcp sales-mcp --from-redshift sales --database dev --workgroup your-workgroup --aws-profile your-profile --description "Tools over sales tables"
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m pytest mcp-servers/sales-mcp
```

Generation reads table and column metadata and creates sample CSV files for
local tests. These tests verify tool integration and permissions. Add tests
with representative data and expected results to verify your queries. The
generated README explains how to configure queries against Redshift.

### Review who can see the data

The sample directory and CSV generators create rules for `analyst` and
`manager` roles. Analysts receive row limits and masking of columns identified
as personal; managers see the allowed columns without those masks. Review
the actual rules, since classifications and masking depend on the source.

Add allowed and denied samples to `tests/policy-samples.yaml`, and assertions
about returned rows and masked columns to the server's tests. Then run:

```bat
"%PYTHON%" -m ai_agent_lib_cli policy test
"%PYTHON%" -m pytest
```

If the deployment uses OPA (Open Policy Agent), also run
`"%PYTHON%" -m ai_agent_lib_cli policy test --opa` on a machine where that
policy engine is installed.

## 8. Optional: use and evaluate a real model

A real model needs credentials, the provider's Python dependencies, and
a model ID available to your account. Its calls can incur charges.

First inspect the supported settings:

```bat
"%PYTHON%" -m ai_agent_lib_cli config options model anthropic
"%PYTHON%" -m ai_agent_lib_cli config options model bedrock
```

Choose one provider and update `agents/helper/pyproject.toml`:

- For Anthropic, add `anthropic` to the existing core extras. The generated
  dependency becomes `ai-agent-lib-core[anthropic,jwt,mcp,otel,serve]`.
- For Bedrock, keep the existing core dependency and add
  `ai-agent-lib-aws[bedrock]` to the `dependencies` list.

Keep any version constraints your project already uses. An **extra** is the
optional dependency group in brackets. Install the updated dependencies:

```bat
"%PYTHON%" -m ai_agent_lib_cli install
```

In `agents/helper/.env`, set `EAP_MODEL_PROVIDER` to your choice and
`EAP_MODEL_ID` to your enabled model ID. Preserve the other settings.
Provide credentials through your team's approved method. For local Anthropic
use, the environment-backed secret provider accepts
`EAP_SECRET_ANTHROPIC_API_KEY`. For AWS, the SDK can use your configured
`AWS_PROFILE` and `AWS_REGION`. Do not commit credentials.

Start any MCP servers linked to the agent, then check and ask:

```bat
"%PYTHON%" -m ai_agent_lib_cli config explain helper
"%PYTHON%" -m ai_agent_lib_cli doctor helper
"%PYTHON%" -m helper "Say goodbye to Ada."
```

The model can now choose tools. Its exact wording may vary. The generated
configuration tests read `.env.example` and use scripted replies to keep
the test suite repeatable.

An **evaluation** checks real-model response quality against example
questions. Review `agents/helper/evals/cases.jsonl` and
`agents/helper/tests/test_helper_eval.py`, then run explicitly:

```bat
"%PYTHON%" -m ai_agent_lib_cli eval helper
```

These tests are excluded from the default run. With a fake model, the
generated evaluation skips. With a real model, it uses network access and
can incur charges. Inspect individual failures as well as the overall score.

## 9. Keep generated files up to date

After upgrading the library, start from a committed or backed-up workspace
and run:

```bat
"%PYTHON%" -m ai_agent_lib_cli update --diff
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m ai_agent_lib_cli check
```

**`update --diff` applies changes** to unmodified generated files. It preserves
files you edited and prints their proposed template changes for you to merge.
Review the resulting diff before committing. Keep `agentlib.lock`: its hashes
distinguish generated files from
files you have changed.

## 10. Optional: prepare a deployment

Use the Python CLI to generate deployment files. To build container images
and apply infrastructure, use a build machine or CI runner with a container
engine, Terraform, and permission to publish images and manage the AWS resources.

From `tutorial/`, create the deployment settings file:

```bat
"%PYTHON%" -m ai_agent_lib_cli deploy helper
```

On the first run this writes `agents/helper/deploy.env` with example values.
Edit it with your platform team's non-local settings before continuing.
Use secret references for credentials. Add any required extras
to the service's dependencies; for example, Bedrock needs the AWS `bedrock`
extra and a PostgreSQL checkpoint store needs its `postgres` extra.

```bat
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m ai_agent_lib_cli deploy helper --plan
"%PYTHON%" -m ai_agent_lib_cli deploy helper
```

The plan explains the adapters, permissions, supporting containers, and
warnings. The second deployment command writes Terraform and image-build
files. Read `deploy/helper/README.md` for the next steps: locking dependencies,
building images, and applying infrastructure on the deployment host.

For shared deployments, configure real authentication, narrow permissions,
and the correct `EAP_DEPLOYMENT_ENV`. See the
[deployment guide](developer-guide.html#deploy) and
[release scope](release-scope.md) for supported behavior and operational limits.

## Common questions and how-to recipes

To see the messages sent to the model and its responses, add this to
`agents/helper/.env`, then restart the agent:

```dotenv
EAP_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl
```

Run your questions with the same `--thread` value to continue a conversation.
Inspect `agents/helper/.agentlib/model-trace.jsonl` for each model call's messages,
tool definitions, response, and attempt number. This opt-in file contains
conversation content; use test data and keep it private. Remove the setting and
restart to stop capture.

New agents include runnable examples. From the workspace root, use
`"%PYTHON%" -m helper.examples.trace_react` for the scripted tool loop. Follow
[Anthropic setup](anthropic-example.md) before running
`"%PYTHON%" -m helper.examples.trace_anthropic` with a real model.
The [annotated verified run](verified-anthropic-run.md) includes captured JSON
and explains the tool result and checkpoint messages that became model input.

Use the developer guide's Q&A for these tasks:

| You want to… | Recipe |
| --- | --- |
| Enable and read model diagnostics | [Inspect model input and output](developer-guide.html#model-debugging) |
| Understand the model–tool loop and restored history | [Run the scripted ReAct example](developer-guide.html#react-context) |
| Follow a real two-question conversation | [Inspect the verified Anthropic run](developer-guide.html#verified-anthropic) |
| Continue or start a conversation | [Use scoped checkpoint threads](developer-guide.html#checkpoint-questions) |
| Investigate missing context, repeated tool calls, or an empty trace | [Common questions](developer-guide.html#faq) |

## When something goes wrong

| Symptom | First check |
| --- | --- |
| `No module named helper` | Use the interpreter from setup and rerun the workspace's `install` command. |
| pip cannot find a package | Check the approved index or wheelhouse, Python version, and compatible wheels. |
| A setting seems ignored | Run `"%PYTHON%" -m ai_agent_lib_cli config explain helper`; a process variable may override `.env`. Restart after changes. |
| The agent answers with `fake:` | That is the default model's expected behavior. Use scripted tests for tools or configure a real model. |
| Connection refused after linking a server | Start each linked MCP server and check its address and port. |
| A request is denied | Compare caller, application, action, and resource with the intended rule. Add a policy sample before changing permissions. |
| Liveness works but readiness returns 503 | Check startup logs, configuration, and MCP dependencies. |
| A schema pin differs | Review the changed tool arguments, update the pin, and rerun tests. |

Expected library errors include diagnostic fields and a suggested fix.
Unexpected Python exceptions can still occur: keep the traceback when
reporting a bug, and remove credentials and sensitive data from reports.

## Where to read more

| For | Read |
| --- | --- |
| Concepts, architecture, testing, and reference tables | [Developer guide](developer-guide.html) |
| All CLI commands and flags | [CLI reference](developer-guide.html#cli) |
| Repository layout and contributing | [README](../README.md) |
| Test rmprep in Agent Lab with its MCP server, using Command Prompt batch files | [Local startup scenarios](local-startup.md) |
| Environment variables | [Configuration reference](variables.md) |
| User identity and service delegation | [Identity guide](identity.md) |
| AWS adapters and dependencies | [AWS package](../packages/ai-agent-lib-aws/README.md) |
| Supported behavior and operational limits | [Release scope](release-scope.md) |

# Getting started

Build an agent named `helper`, add a tool, and test its behavior and
permissions. Then add shared tools through an MCP server. This walkthrough
uses the same `tutorial/` workspace as the [developer guide](developer-guide.html).
If you already completed a step there or in the README, continue from the
next step rather than generating the project again.

You need basic Python knowledge: functions, imports, and running a command
in a terminal. New library terms are explained as you encounter them.

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

Python and pip are sufficient for the local tutorial. You do not need
Docker, uv, a cloud account, administrator rights, or permission to activate
PowerShell scripts. After installing packages, the local tests and fake
model need no external services. Dependencies with native code still need
wheels compatible with your Python version and operating system if you
cannot compile them.

## 1. Create and run an agent

An **agent** uses a language model to answer questions and choose tools.
A **tool** is a Python function the agent can call. Start with one agent
and its built-in greeting tool.

Choose the commands for your terminal. They create a virtual environment
inside the library checkout and an application workspace named `tutorial/`
beside it. A virtual environment keeps project packages separate from system
Python. Use a different workspace name if that folder already exists.

### Windows PowerShell

Run from the library root and keep this terminal open:

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

If you use the Windows Python launcher, replace the environment creation
command with `py -3.11 -m venv .venv`. The remaining commands use the
environment's interpreter directly; you do not need to activate scripts.

### macOS or Linux, using bash or zsh

Run from the library root and keep this terminal open:

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

**Checkpoint:** the tests pass and the final command prints:

```text
fake: Say hello to Ada.
```

The fake model echoes your question. It does not decide to call the greeting
tool. In step 3, a scripted test will tell it exactly which tool to call.

Here is what the setup commands did:

| Command or option | Purpose |
| --- | --- |
| `python -m venv .venv` | Creates an isolated place for Python packages |
| `pip install -e …` | Installs the library packages in editable mode, so source changes are available without reinstalling them |
| `init tutorial` | Creates the application workspace |
| `--lib-path` | Uses this local library checkout instead of a published version |
| `new agent helper` | Writes the agent, configuration, and tests |
| `install` | Uses the current Python's pip to install workspace services and development tools |
| `pytest` | Runs the generated tests |
| `python -m helper` | Asks the generated agent one question |

Run `install` again whenever you add a service or change its dependencies.

### Command convention for the rest of this tutorial

Run commands from `tutorial/`. Examples use PowerShell's `& $Python` prefix;
on macOS/Linux, replace it with `"$PYTHON"`. Both point to the same virtual
environment used during setup. PowerShell's `&` runs the executable stored
in a variable; you do not type it in bash or zsh.

### Using a new terminal

Shell variables do not carry over to a new terminal. First open that terminal
in `tutorial/`, then restore the interpreter path:

```powershell
# Windows PowerShell, from tutorial/
$Library = (Resolve-Path ../ai-agent-lib).Path
$Python = Join-Path $Library ".venv\Scripts\python.exe"
```

```bash
# macOS/Linux, from tutorial/
LIBRARY="$(cd ../ai-agent-lib && pwd)"
PYTHON="$LIBRARY/.venv/bin/python"
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
| `agentlib.lock` | Generated-file hashes used when updating templates; not a dependency lock |

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

```powershell
& $Python -m pytest agents/helper/tests/test_farewell.py
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

```powershell
& $Python -m ai_agent_lib_cli policy test
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

Run `& $Python -m ai_agent_lib_cli policy test` again. It should pass.

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
`frame_untrusted` marks the tool's output as data, rather than new instructions.

`async with` starts and closes the test services. `await` waits for the
agent's work without blocking other asynchronous tasks. The generated
pytest configuration already supports these asynchronous tests.

**The graph test uses fake permissions.** The policy sample in step C checks
the actual rule. A passing graph test alone does not prove that configuration
will permit the call.

```powershell
& $Python -m pytest agents/helper
& $Python -m ai_agent_lib_cli policy test
& $Python -m ai_agent_lib_cli check
```

**Checkpoint:** tests and policy samples pass. `check` also runs lint,
formatting, and type checks. If pasted code needs formatting, run
`& $Python -m ruff format .` and retry. The command-line agent still echoes
questions until you select a real model.

## 4. Check and change configuration

An **adapter** implements a service, such as a fake model or a database
connection. Configuration selects adapters without changing the graph.
Your local settings are in `agents/helper/.env`:

```dotenv
EAP_PROFILE=local
EAP_DEPLOYMENT_ENV=local
EAP_MODEL_PROVIDER=fake
```

These are selected lines, not a replacement for the whole file. Keep its
other generated settings.

- `EAP_PROFILE` chooses defaults: `local` or `aws`.
- `EAP_DEPLOYMENT_ENV` says where this process runs: `local`, `dev`, or `prod`.
  Non-local environments reject development-only adapters.
- `EAP_MODEL_PROVIDER` chooses the model implementation. `fake` needs no account.

Process environment variables override `.env`; `.env` overrides profile
defaults. Relative file paths are resolved from the folder containing
`.env`, not from your terminal. Restart a service after editing its settings.

```powershell
& $Python -m ai_agent_lib_cli config explain helper
& $Python -m ai_agent_lib_cli config options model fake
& $Python -m ai_agent_lib_cli doctor helper
```

`explain` shows values and their sources, with secrets masked. `options`
lists accepted keys and defaults. `doctor` builds and validates the selected
services; with cloud adapters, that can contact external services.

For JSON options, use double quotes around keys and strings, and keep the
value on one line. Unknown options are rejected rather than ignored.

## 5. Add a shared tool server

MCP (Model Context Protocol) lets a separate server provide tools to agents.
Use it when tools or data access should be shared. A function inside the
agent is enough for a tool that only that agent needs.

From `tutorial/`, add a sample server named `directory`:

```powershell
& $Python -m ai_agent_lib_cli new mcp directory --description "The people directory"
& $Python -m ai_agent_lib_cli install
& $Python -m ai_agent_lib_cli link helper directory
& $Python -m pytest
& $Python -m ai_agent_lib_cli policy test
```

The server lives in `mcp-servers/directory/`. It has sample CSV data, named
queries, tool functions, and tests. A **named query** is defined in advance
and called with parameters; the model is not given arbitrary SQL access.

`link` updates registrations and permissions and adds a test of the agent
and server together. That test uses an in-process server and a scripted
model, so you do not need to start either service to run the tests.

**Checkpoint:** the expanded test suite and policy samples pass.

After linking, start the server before asking the agent a question. In one
terminal, with the Python variable set and the working directory at `tutorial/`:

```powershell
& $Python -m ai_agent_lib_cli run directory
```

In a second terminal, [restore the Python variable](#using-a-new-terminal), then:

```powershell
& $Python -m helper "Who is in the payments team?"
```

The fake model still echoes the question. The connection test, rather than
that echoed answer, proves that the agent can use a server tool. Press
Ctrl+C in the server terminal when finished.

When a tool's arguments change, review the change and update its **schema
pin**, the hash of its expected input definition:

```powershell
& $Python -m ai_agent_lib_cli registry pin directory
& $Python -m ai_agent_lib_cli check
```

## 6. Optional: call the agent over HTTP

HTTP lets another program call the agent. If you completed step 5, keep
`directory` running in its own terminal. Start the agent in another:

```powershell
& $Python -m ai_agent_lib_cli run helper
```

Save this complete script as `try_http.py` in `tutorial/`. It uses Python's
standard library, so it needs no additional package or command-line tool:

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

```powershell
& $Python try_http.py
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
with tests. Review the proposals and the generated rules before using real
data: column-name guesses are not a complete data-classification review.

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

```powershell
& $Python -m ai_agent_lib_cli new mcp claims-mcp --from-csv ./inputs/claims --propose
& $Python -m ai_agent_lib_cli new mcp claims-mcp --from-csv ./inputs/claims --description "Tools over practice claims"
& $Python -m ai_agent_lib_cli install
& $Python -m pytest mcp-servers/claims-mcp
& $Python -m ai_agent_lib_cli doctor claims-mcp
```

`--propose` writes nothing. Generation copies the CSV files into the server's
`data/` folder and writes query definitions under `queries/`. Each file is
a table. `--per-table` limits how many tools are proposed; repeat `--query`
to keep selected query names from the proposal.

The generated tests check calls and permissions. Add assertions for the
results your application actually needs. For this dataset, inspect
`mcp-servers/claims-mcp/tests/test_claims_mcp.py` and the generated query files.

The agent can use this server after you link it:

```powershell
& $Python -m ai_agent_lib_cli link helper claims-mcp
& $Python -m pytest tests
& $Python -m ai_agent_lib_cli check
```

Start `claims-mcp` as well as `directory` before running the linked agent.
The generated tests need neither server running. If you later change the
tool arguments, review them and run
`& $Python -m ai_agent_lib_cli registry pin claims-mcp`.

### A REST API described by OpenAPI

If you have an OpenAPI 3 document, save it as `inputs/rates.yaml` or replace
that path below with your document's path:

```powershell
& $Python -m ai_agent_lib_cli new mcp rates-mcp --from-openapi ./inputs/rates.yaml --propose
& $Python -m ai_agent_lib_cli new mcp rates-mcp --from-openapi ./inputs/rates.yaml --description "Tools over the rates API"
& $Python -m ai_agent_lib_cli install
& $Python -m pytest mcp-servers/rates-mcp
```

The CLI creates tools for supported GET operations returning JSON records.
Generation does not call the API. Tests use `tests/api_responses.json`
inside the generated server; replace its sample responses with representative
test data. Read that server's README before configuring API authentication
or running diagnostics that contact the real service.

### Redshift tables

This optional path needs an AWS account with permission to read the database
catalogue and credentials available to the Python AWS SDK. Use your team's
approved credential setup. If that requires a sign-in tool you cannot install,
run this step on an approved development or CI host.

The checkout-based `install` command includes the local AWS package. Run
the following after replacing `your-workgroup` and `your-profile` with your
approved values; adjust the schema and database names as needed:

```powershell
& $Python -m ai_agent_lib_cli new mcp sales-mcp --from-redshift sales --database dev --workgroup your-workgroup --aws-profile your-profile --description "Tools over sales tables"
& $Python -m ai_agent_lib_cli install
& $Python -m pytest mcp-servers/sales-mcp
```

Generation reads table and column metadata, not table rows. It creates local
stand-in CSV files for testing. Those made-up rows verify the integration,
not the correctness of results from your real database. The generated README
explains the configuration for querying Redshift.

### Review who can see the data

The sample directory and CSV generators create rules for `analyst` and
`manager` roles. Analysts receive row limits and masking of columns identified
as personal; managers see the allowed columns without those masks. Review
the actual rules, since classifications and masking depend on the source.

Add allowed and denied samples to `tests/policy-samples.yaml`, and assertions
about returned rows and masked columns to the server's tests. Then run:

```powershell
& $Python -m ai_agent_lib_cli policy test
& $Python -m pytest
```

If the deployment uses OPA (Open Policy Agent), also run
`& $Python -m ai_agent_lib_cli policy test --opa` on a machine where that
separate policy engine is available. OPA is not required for this local tutorial.

## 8. Optional: use and evaluate a real model

A real model needs credentials, the provider's Python dependencies, and
a model ID available to your account. Its calls can incur charges.

First inspect the supported settings:

```powershell
& $Python -m ai_agent_lib_cli config options model anthropic
& $Python -m ai_agent_lib_cli config options model bedrock
```

Choose one provider and update `agents/helper/pyproject.toml`:

- For Anthropic, add `anthropic` to the existing core extras. The generated
  dependency becomes `ai-agent-lib-core[anthropic,jwt,mcp,otel,serve]`.
- For Bedrock, keep the existing core dependency and add
  `ai-agent-lib-aws[bedrock]` to the `dependencies` list.

Keep any version constraints your project already uses. An **extra** is the
optional dependency group in brackets; changing configuration alone does
not install it.

```powershell
& $Python -m ai_agent_lib_cli install
```

In `agents/helper/.env`, set `EAP_MODEL_PROVIDER` to your choice and
`EAP_MODEL_ID` to your enabled model ID. Preserve the other settings.
Provide credentials through your team's approved method. For local Anthropic
use, the environment-backed secret provider accepts
`EAP_SECRET_ANTHROPIC_API_KEY`. For AWS, the SDK can use your configured
`AWS_PROFILE` and `AWS_REGION`. Do not commit credentials.

Start any MCP servers linked to the agent, then check and ask:

```powershell
& $Python -m ai_agent_lib_cli config explain helper
& $Python -m ai_agent_lib_cli doctor helper
& $Python -m helper "Say goodbye to Ada."
```

The model can now choose tools. Its exact wording may vary. The generated
configuration tests still read `.env.example` and use scripted replies, so
your ordinary test suite stays repeatable.

An **evaluation** checks real-model response quality against example
questions. Review `agents/helper/evals/cases.jsonl` and
`agents/helper/tests/test_helper_eval.py`, then run explicitly:

```powershell
& $Python -m ai_agent_lib_cli eval helper
```

These tests are excluded from the default run. With a fake model, the
generated evaluation skips. With a real model, it uses network access and
can incur charges. Inspect individual failures as well as the overall score.

## 9. Keep generated files up to date

After upgrading the library, start from a committed or backed-up workspace
and run:

```powershell
& $Python -m ai_agent_lib_cli update --diff
& $Python -m ai_agent_lib_cli install
& $Python -m ai_agent_lib_cli check
```

**`update --diff` is not a dry run.** Unmodified generated files are updated.
Files you edited are preserved, and `--diff` shows the template changes for
those files so you can merge them yourself. Review the resulting diff before
committing. Keep `agentlib.lock`: its hashes distinguish generated files from
files you have changed.

## 10. Optional: prepare a deployment

Local development remains a Python/pip workflow. Building container images
and applying infrastructure needs a suitable machine or CI runner with the
required tools and permissions.

From `tutorial/`, create the deployment settings file:

```powershell
& $Python -m ai_agent_lib_cli deploy helper
```

On the first run this writes `agents/helper/deploy.env` with example values.
Edit it with your platform team's non-local settings before continuing.
Use secret references rather than secret values. Add any required extras
to the service's dependencies; for example, Bedrock needs the AWS `bedrock`
extra and a PostgreSQL checkpoint store needs its `postgres` extra.

```powershell
& $Python -m ai_agent_lib_cli install
& $Python -m ai_agent_lib_cli deploy helper --plan
& $Python -m ai_agent_lib_cli deploy helper
```

The plan explains the adapters, permissions, supporting containers, and
warnings. The command generates Terraform and image-build files; it does
not provision AWS resources. Read `deploy/helper/README.md` for dependency
locking, image building, and infrastructure steps on the deployment host.

For shared deployments, configure real authentication, narrow permissions,
and the correct `EAP_DEPLOYMENT_ENV`. See the
[deployment guide](developer-guide.html#deploy) and
[release scope](release-scope.md) before relying on a feature's guarantees.

## When something goes wrong

| Symptom | First check |
| --- | --- |
| `No module named helper` | Use the interpreter from setup and rerun the workspace's `install` command. |
| pip cannot find a package | Check the approved index or wheelhouse, Python version, and compatible wheels. |
| A setting seems ignored | Run `config explain helper`; a process variable may override `.env`. Restart after changes. |
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
| Environment variables | [Configuration reference](variables.md) |
| User identity and service delegation | [Identity guide](identity.md) |
| AWS adapters and dependencies | [AWS package](../packages/ai-agent-lib-aws/README.md) |
| Current behavior versus the target design | [Release scope](release-scope.md) |

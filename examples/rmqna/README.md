# rmqna

A workspace for Python agents and shared MCP tools built on `ai-agent-lib`.
Owner: commercial-banking.

Run the commands below from **this workspace's root folder**, where
`agentlib.toml` lives. Local development needs Python 3.11 or newer and pip.
Installing packages requires access to your approved package index or a
folder of Python wheels. The default tests use scripted models and local fixtures.

## Select Python

Use your team's approved Python and pip for this workspace's agents and MCP servers.
Packages install into that Python's configured location. The commands run pip as
a Python module. On Windows, use **Command Prompt (cmd.exe)** and run
one line at a time. `set` stores a variable; `%PYTHON%` reads it. Keep the quotes
around paths so they work when a folder name contains spaces.

Agent Lab, if used, runs with the same Python and a separate package folder and calls these agents
over HTTP. Follow its package README to register your generated agent; the
library checkout's launcher starts the included rmprep example. That
launcher's root `.env` configures rmprep. For this workspace, put model settings
in each agent's `.env` and use the service commands below.

This workspace uses the library checkout recorded in `agentlib.toml`. Select
your approved Python (use its full path if your team supplies one):

**Windows Command Prompt (cmd.exe):**

```bat
set "LIBRARY=../.."
set "PYTHON=python"
"%PYTHON%" -c "import sys; print(sys.executable)"
```

**macOS/Linux, using bash or zsh:**

```bash
LIBRARY='../..'
PYTHON=python3
"$PYTHON" -c "import sys; print(sys.executable)"
```

If this checkout moved, update its path in `agentlib.toml` and the variable
above. Install the CLI once using the selected Python:

```bat
"%PYTHON%" -m pip install -e "%LIBRARY%/packages/ai-agent-lib-core" -e "%LIBRARY%/packages/ai-agent-lib-cli"
```

```bash
"$PYTHON" -m pip install -e "$LIBRARY/packages/ai-agent-lib-core" -e "$LIBRARY/packages/ai-agent-lib-cli"
```

For every new terminal, open the workspace root and select the same Python
again. The commands below use Command Prompt's `"%PYTHON%"`. In bash/zsh, replace
that prefix with `"$PYTHON"` to use the selected Python.

## Install and check

```bat
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m ai_agent_lib_cli check
```

`install` uses this Python's pip to install the services and development tools.
Run it again after adding a service or changing dependencies. `check` runs lint,
formatting, type checks, tests, and policy samples. To run only the tests:

```bat
"%PYTHON%" -m pytest
```

## Add services

These commands create a server and an agent, then connect them. Choose names
that are available in `agentlib.toml`. To work on an existing service, start
with its README under `agents/` or `mcp-servers/`.

```bat
"%PYTHON%" -m ai_agent_lib_cli new mcp hello-mcp
"%PYTHON%" -m ai_agent_lib_cli new agent hello-agent --model fake
"%PYTHON%" -m ai_agent_lib_cli link hello-agent hello
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m ai_agent_lib_cli check
```

For your own data, add `--from-csv`, `--from-openapi`, or `--from-redshift` to
`new mcp`, followed by the folder, file, or schema. Add `--propose` to review
the proposed tools first. Redshift discovery needs access to that database.

The generators write code, tests, registry entries, and policy rules. The fake
model echoes input when run interactively; scripted test replies exercise tool
calls. Start a behavior change with a failing test, then implement it and rerun
the checks.

## Inspect model input, output, and conversation history

Read the [verified Anthropic walkthrough](docs/verified-anthropic-run.md) to
follow a real greeting tool call and a second question on the same thread.
The model received 2, 4, then 6 messages as the tool result and saved history
joined its input. These included samples can be read locally:

```bat
"%PYTHON%" examples\read_trace.py docs\samples\verified-anthropic\model-trace.jsonl
"%PYTHON%" -m json.tool docs\samples\verified-anthropic\checkpoint-history.json
```

Each generated agent includes two runnable examples. Open its README under
`agents/` for the exact commands and Anthropic setup instructions:

- `examples.trace_react` uses scripted model replies to demonstrate the tool
  loop and checkpoints, with the services stopped.
- `examples.trace_anthropic` uses the agent's configured Anthropic model and
  SQLite checkpoints. It asks two questions, prints their history and trace,
  and accepts `--thread` to continue a saved conversation.

For example, an agent called `hello-agent` uses the module
`hello_agent.examples.trace_anthropic`. Anthropic calls use API tokens;
its README links to instructions for the dependency, model ID, and key.
The examples write diagnostics into that agent's `.agentlib/` folder.

## Run and troubleshoot

Open `agents/<name>/README.md` or `mcp-servers/<name>/README.md` for the exact
startup order. Each server keeps its terminal busy until you press Ctrl+C.
Start an agent's required MCP servers before starting the agent.

For the example agent above:

```bat
"%PYTHON%" -m ai_agent_lib_cli doctor hello-agent
"%PYTHON%" -m ai_agent_lib_cli graph hello-agent
"%PYTHON%" -m ai_agent_lib_cli policy test
```

`doctor` checks configured dependencies and explains failures. With cloud
adapters it can contact external services. `graph` prints a Mermaid diagram.
The policy command checks which actions your rules allow and deny.

Run `"%PYTHON%" -m ai_agent_lib_cli --help` to see other commands, including
`config`, `update`, `eval`, and `deploy`. Evals use the configured real model;
they are excluded from the default test run.

## Configure CI

CI means running the same checks on GitHub after a push or pull request.
See [GitHub's secrets guide](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
for private-index credentials. Secrets may be unavailable to fork pull requests;
the platform maintainer must account for that when configuring index access.
**CI is not configured yet.** This workspace installs the library from a
desktop path. GitHub's machines cannot access that folder, so the generated
workflow stops at **Configure the library source for CI**. Complete the steps
below to make the library available to the runner.

To use a release published to your team's approved Python package index:

1. Confirm that the core, CLI, and any required AWS packages are published.
   Choose an available release and use it in place of `0.1.0` in these examples.
2. Replace the `[library]` section of `agentlib.toml` with the following,
   using your chosen version and removing the old `path` entry:

   ```toml
   [library]
   source = "index"
   version = "==0.1.0"
   ```

3. In the root `pyproject.toml`, pin the `ai-agent-lib-cli` development
   dependency to the same release. In each service's `pyproject.toml`, pin
   its library dependencies too, keeping their extras. For example,
   `ai-agent-lib-core[jwt,mcp,otel,serve]==0.1.0`.
4. Edit `.github/workflows/check.yml`: remove the step named **Configure the
   library source for CI**, and pin its CLI installation to your release:

   ```yaml
   - run: python -m pip install "ai-agent-lib-cli==0.1.0"
   ```

5. Configure index access on the runner. For a private index, store the
   complete authenticated index URL in a GitHub Actions secret named
   `PIP_INDEX_URL`; add this under the `check` job alongside `runs-on`.
   Do not put credentials in the workflow file:

   ```yaml
   env:
     PIP_INDEX_URL: ${{ secrets.PIP_INDEX_URL }}
   ```

6. Test the index installation on your team's clean CI runner before pushing.
   This verifies that the published packages include everything the workspace
   needs. Then run `python -m ai_agent_lib_cli check` on the runner.

To use a library checkout in CI, ask a platform maintainer to configure the
runner to check out a fixed revision, install its packages with pip, and set
the path in the runner's `agentlib.toml` to that checkout. Remove the setup gate
after both the package installation and workspace path are configured.

## What is here

| Path | What it contains |
| --- | --- |
| `agents/` | One folder per agent, with its own README |
| `mcp-servers/` | One folder per MCP server, with its own README |
| `registry/agents.yaml` | Agents and the MCP servers each may call |
| `registry/mcp-tools.yaml` | Servers, tools, and pins of their input schemas |
| `policies/agentlib/rules/data.yaml` | Allow rules; unmatched requests are denied |
| `tests/` | Workspace tests and tests of agents together with servers |
| `examples/read_trace.py` | Read a model trace as formatted JSON |
| `docs/verified-anthropic-run.md` | Annotated real model calls and checkpoint snapshots |
| `docs/samples/verified-anthropic/` | Selected-field samples from the verified run |
| `tests/policy-samples.yaml` | Examples the rules must allow or deny |
| `.github/workflows/check.yml` | CI checks; complete the setup above before relying on them |
| `agentlib.toml` | Workspace generation inputs and the library installation source |
| `agentlib.lock` | Generated-file fingerprints used by the update command; commit this file |
| `deploy/` | Deployment files, after running the deployment generator |

Every service has a committed `.env.example` used by its tests and an ignored
`.env` for local settings. Relative paths in those files resolve from the file's
folder. Audit logs and checkpoints go in each service's `.agentlib/` folder.
Services share the workspace's registry and policy files.

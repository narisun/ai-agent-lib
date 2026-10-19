# rmqna-data

A greeting and a people directory, over sample data.

This MCP server exposes tools for agents to call. The library checks caller
identity, registered tools, permissions, and guardrails, then records an audit
event. Data queries also pass through the library's permission and result checks.

## Before you start

Run commands from the **workspace root**, where `agentlib.toml` lives.
Follow [Select Python](../../README.md#select-python) in the workspace README
to set `PYTHON` to your approved Python executable. Select it again in
every new terminal. These commands use Windows Command Prompt; in bash/zsh replace
`"%PYTHON%"` with `"$PYTHON"` after setting `PYTHON` as that README describes.

Install the workspace once, and again after changing dependencies:

```bat
"%PYTHON%" -m ai_agent_lib_cli install
```

## Available tools

| Tool | What it does |
| --- | --- |
| `rmqna-data.greet` | A plain tool: returns a greeting |
| `rmqna-data.people_by_team` | Runs the named query `people_by_team` over `data/people.csv` |

## Test it

```bat
"%PYTHON%" -m pytest mcp-servers/rmqna-data
```

The tests use local CSV data.
They apply the workspace's policy rules to check what each role may see:

| Role | Tool call | Data |
| --- | --- | --- |
| `manager` | allowed | every column |
| `analyst` | allowed | `email` is masked, at most 50 rows |
| anything else | denied | none |

**Expected result:** the tests pass for allowed and denied callers. To change
behavior, add a test in `tests/test_rmqna_data.py` that sets the caller's roles,
calls the tool, and asserts the expected result.

## Run it

Open a terminal at the workspace root and select Python as described above.
Start this server **before** any agent that calls its tools:

```bat
"%PYTHON%" -m ai_agent_lib_cli run rmqna-data
```

Leave this terminal running. Wait for the `ready` log event before starting
an agent in another terminal. The MCP endpoint is
`http://127.0.0.1:8300/mcp`; `/healthz` reports liveness and `/readyz`
reports readiness. Press Ctrl+C in the server terminal to stop it.

To check readiness from another terminal, select Python there and run:

```bat
"%PYTHON%" -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8300/readyz', timeout=10).status)"
```

**Expected result:** a ready server prints `200`. A connection error means the
listener is unavailable; HTTP 503 means it has not become ready. Read the server's
output or run `"%PYTHON%" -m ai_agent_lib_cli doctor rmqna-data` to diagnose a failure.

On this machine a caller without a token is the development identity in
`AC2_IDENTITY_OPTIONS`. Local state is written under
`mcp-servers/rmqna-data/.agentlib/`.

## What to read

These file paths are relative to this service folder:

| File | Shows |
| --- | --- |
| `src/rmqna_data/server.py` | Tool functions and their library integrations |
| `queries/people_by_team.sql` | A named query: SQL with a declared interface |
| `data/people.csv` | The sample data. Each CSV file is a table |
| `.env.example` | How the service is configured locally |

## Add a tool over your own data

1. Put a CSV file in `data/` and a query in `queries/`. The query's header
   declares its parameters, its row limit and its classification.
2. Write a failing test that calls the new tool as a role and says what that
   role should see.
3. Add the tool in `src/rmqna_data/server.py` and list it under `rmqna-data`
   in the workspace's `registry/mcp-tools.yaml`.
4. From the workspace root, run
   `"%PYTHON%" -m ai_agent_lib_cli registry pin rmqna-data` to pin its arguments.
5. Update the workspace's `policies/agentlib/rules/data.yaml` for any new roles
   or columns to mask, and add samples in `tests/policy-samples.yaml`.
6. Run `"%PYTHON%" -m ai_agent_lib_cli check` from the workspace root.

The queries are written in the dialect of the production database and run
unchanged over CSV files locally.

## Prepare a deployment

From the workspace root:

```bat
"%PYTHON%" -m ai_agent_lib_cli deploy rmqna-data
```

The first run creates `mcp-servers/rmqna-data/deploy.env`. Fill in the deployment
settings and add the provider dependencies reported by the CLI to the service's
`pyproject.toml`. Install them, review the plan, then generate the deployment files:

```bat
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m ai_agent_lib_cli deploy rmqna-data --plan
"%PYTHON%" -m ai_agent_lib_cli deploy rmqna-data
```

**Expected result:** `deploy/rmqna-data/` contains Terraform and image-build
files. Follow its README to build images and apply infrastructure on a build
machine or CI runner with a container engine, Terraform, and the required
registry and AWS permissions.

# Getting started

From nothing to an agent and an MCP server over your own data, each with
tests you can run offline. About half an hour.

You work test first throughout: say what should happen in a test, watch it
fail, then make it pass. The workspace `agentlib` creates is set up for that.

## What you need

- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/).
- A checkout of this repository. `<lib>` below is its path.

No account and no network connection are needed after the packages are
installed. The model is a stand-in until you choose a real one in step 8.

## 1. Create a workspace

```bash
uv run --project <lib> --package ai-agent-lib-cli agentlib init my-platform
cd my-platform
uv sync --all-packages
```

A workspace holds every agent and MCP server of a team, with one registry and
one rules file they all read. From here on, `agentlib` runs from the
workspace's own environment as `uv run agentlib`.

## 2. Add a hello-world server and agent

```bash
uv run agentlib new mcp hello-mcp
uv run agentlib new agent hello-agent --mcp hello
uv sync --all-packages
uv run pytest
```

18 tests pass. You now have:

| Path | What it is |
| --- | --- |
| `mcp-servers/hello-mcp/` | An MCP server with a plain tool and a tool over `data/people.csv` |
| `agents/hello-agent/` | A LangGraph agent with one tool of its own, allowed to call the server |
| `registry/` | Which agents and tools exist. A tool that is not listed cannot be called |
| `policies/agentlib/rules/data.yaml` | Who may do what. A request no rule matches is denied |
| `tests/` | The agent and the server together, and sample requests for the rules |

## 3. Check it and run it

```bash
uv run agentlib doctor
```

`doctor` builds each service from its `.env` file and checks everything it is
configured to use. When something is wrong it names it and says how to fix
it. Run it first whenever a service does not start.

Start the server in one terminal and ask the agent in another:

```bash
uv run agentlib run hello-mcp            # terminal 1
uv run hello-agent "Say hello to Ada."   # terminal 2
```

The answer is `fake: Say hello to Ada.`: the stand-in model repeats the
question. Everything around the model is real. `agents/hello-agent/.agentlib/audit.jsonl`
has a record of the call, with who asked and which rule allowed it.

To serve the agent over HTTP instead:

```bash
uv run agentlib run hello-agent          # POST /invoke on port 8000
curl -s localhost:8000/invoke -H 'content-type: application/json' \
  -d '{"input": {"question": "Say hello to Ada."}, "thread_id": "demo"}'
```

Ctrl-C stops a service. A service logs one line of JSON per event; none of
them holds what was asked or answered.

## 4. Change the agent, test first

Give the agent a second tool that says goodbye.

**Say what it should do.** In `agents/hello-agent/tests/test_hello_agent.py`,
import the tool that does not exist yet, next to `greet`, and add a test:

```python
from hello_agent.tools import farewell, greet

...


def test_farewell_says_goodbye_by_name() -> None:
    assert farewell("Ada") == "Goodbye, Ada!"
```

```bash
uv run pytest agents/hello-agent      # fails: there is no farewell yet
```

**Write it.** In `agents/hello-agent/src/hello_agent/tools.py`:

```python
def farewell(name: str) -> str:
    """Return a goodbye for a person, given their name."""
    return f"Goodbye, {name.strip()}!"
```

and give it to the agent in `graph.py`:

```python
from hello_agent.tools import farewell, greet
...
    tools = services.tools([greet, farewell], read_only=["greet", "farewell"])  # touchpoint 1
```

The test passes. The agent still may not use the tool: no rule allows it.

**Say who may use it.** Add a sample request to `tests/policy-samples.yaml`:

```yaml
  - name: hello-agent says goodbye
    action: tool.call
    application: hello-agent
    resource: farewell
    expect: allow
    reason: hello-agent-calls-its-own-tools
```

```bash
uv run agentlib policy test           # fails: deny (no_matching_rule)
```

**Allow it.** In `policies/agentlib/rules/data.yaml`, name the tool in the
agent's rule:

```yaml
  - id: hello-agent-calls-its-own-tools
    actions: [tool.call]
    applications: [hello-agent]
    resources: [greet, farewell]
```

`uv run agentlib policy test` passes, and so does `uv run pytest`.

The same four moves work for every change: a test for the code, a sample for
the rule.

## 5. A server over your own data

Pick the option that matches where the data is. Each one proposes the tools,
writes them with their tests, and masks columns that look personal for the
`analyst` role. Add `--propose` to any of them to see what would be written
without writing it.

### CSV files

```bash
uv run agentlib new mcp claims-mcp --from-csv ~/exports/claims --propose
uv run agentlib new mcp claims-mcp --from-csv ~/exports/claims
```

Each file becomes a table and is copied into the server's `data/` folder. For
each table you get a lookup by the column that identifies a row, and a filter
for each column that sorts rows into a few groups. `--per-table` changes how
many, and `--query NAME` keeps only the ones you name.

### A REST API

```bash
uv run agentlib new mcp rates-mcp --from-openapi ~/specs/rates.yaml
```

There is one tool for each GET operation of the OpenAPI 3 document that
answers with JSON records. The API is not called. The tests answer for it
from `tests/api_responses.json`, which is written from the document's schemas
and examples: put in answers that look like the real ones. The server's
README says how to give it the API's token.

### Redshift tables

```bash
uv add --dev ai-agent-lib-aws
aws sso login --profile <profile>
uv run agentlib new mcp sales-mcp --from-redshift sales \
  --database dev --workgroup <workgroup> --aws-profile <profile>
```

Only the catalogue is read: table and column names, never a row. The server
runs locally over stand-in CSV files with the tables' columns and two made-up
rows each, so you can test without the database. Deployed, the same query
files run on Redshift; the server's README has the setting for that.

### Then, for any of them

```bash
uv sync --all-packages
uv run pytest mcp-servers/claims-mcp
uv run agentlib doctor claims-mcp
```

The generated tests say which columns each role sees. They do not know what
the rows should be. That is your first test: open
`mcp-servers/claims-mcp/tests/test_claims_mcp.py`, add what a call should
return, and change the query in `queries/` until it does.

The proposals are a starting point. Delete the tools you do not want: the
tool in `server.py`, its query file, its tests and its entry in
`registry/mcp-tools.yaml`. After changing a tool's arguments, pin it again:

```bash
uv run agentlib registry pin claims
```

## 6. Decide who sees what

A new server gets three rules: analysts and managers may call its tools,
managers see every column, analysts see the masked columns as `***` and at
most 50 rows. Everyone else is denied.

The guess at what is personal comes from column names such as `email`,
`phone` and `date_of_birth`. Check it. To mask another column, add it to
`mask_columns` in the server's analyst rule, and say so first in a test:

```python
    assert table["masked_columns"] == ["email", "home_city"]
```

`uv run agentlib policy test --opa` decides the samples with OPA as well,
using the platform's Rego bundle, and fails if the two engines disagree. It
needs the `opa` program. Run it before a change to the rules is merged:
deployed services ask OPA.

## 7. Let an agent use a server

```bash
uv run agentlib link hello-agent claims
uv sync --all-packages
uv run pytest tests
```

`link` registers the server for the agent, adds the rule and a sample for it,
and writes `tests/test_hello_agent_with_claims_mcp.py`: both services in one
process, a scripted model that calls the server's tool, and a check of what
came back for the caller the agent acted for. The agent's caller is still the
one whose roles decide what the server shows.

## 8. A real model

In `agents/hello-agent/.env` (yours, not committed):

```bash
EAP_MODEL_PROVIDER=bedrock
EAP_MODEL_ID=<model-id>
AWS_PROFILE=<profile>
AWS_REGION=<region>
```

or `anthropic` with `EAP_SECRET_ANTHROPIC_API_KEY`. The two AWS settings are
the SDK's own names; sign in first with `aws sso login --profile <profile>`.
Then:

```bash
uv run agentlib config explain hello-agent   # every setting and where it came from
uv run agentlib doctor hello-agent
uv run hello-agent "Say hello to Ada."
```

The tests do not change: they read `.env.example` and script the model, so
they stay offline and repeatable.

## 9. Keep up with the library

```bash
uv run agentlib update --diff
```

A generated file you have not changed is replaced with what the templates
write now. A file you changed is left alone and listed, with the difference.
Commit `agentlib.lock`: it is how `update` tells the two apart.

## Where to read more

| For | Read |
| --- | --- |
| Every `agentlib` command | [`packages/ai-agent-lib-cli/README.md`](../packages/ai-agent-lib-cli/README.md) |
| What the generated code is made of | [`README.md`](../README.md) |
| Every configuration variable | [`variables.md`](variables.md) |
| Signing users in with Microsoft Entra ID | [`identity.md`](identity.md) |
| Running on AWS | [`packages/ai-agent-lib-aws/README.md`](../packages/ai-agent-lib-aws/README.md) |

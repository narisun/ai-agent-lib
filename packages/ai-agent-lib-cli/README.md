# ai-agent-lib-cli

The `agentlib` command. It creates a local development workspace and adds
agents and MCP servers to it. What it writes is working code with its tests:
a hello-world you can run and test before you change a line, and a place to
start every later change with a failing test.

Nothing it writes needs an account or a network connection.

## Five minutes

From a checkout of the library, with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv run --project <path-to-ai-agent-lib> --package ai-agent-lib-cli agentlib init my-platform
cd my-platform
uv sync --all-packages                             # the workspace gets its own environment
uv run agentlib new mcp hello-mcp                  # an MCP server over sample data
uv run agentlib new agent hello-agent --mcp hello  # an agent that may call it
uv sync --all-packages                             # install the two new services
uv run pytest                                      # 18 tests, all offline
```

Then run them:

```bash
uv run hello-mcp                         # terminal 1: http://127.0.0.1:8100/mcp
uv run hello-agent "Say hello to Ada."   # terminal 2
```

The agent starts on the `fake` model, which echoes the question. Select a real
model in `agents/hello-agent/.env` when you want one.

## Commands

| Command | What it does |
| --- | --- |
| `agentlib init NAME` | Creates the workspace folder: a uv workspace with shared registry and rules files |
| `agentlib new mcp NAME` | Adds an MCP server: one plain tool, one governed query over a sample CSV file, tests |
| `agentlib new agent NAME [--mcp SERVER]` | Adds a LangGraph agent with one tool, a command line, an HTTP entry point, tests |
| `agentlib link AGENT SERVER` | Lets an existing agent call an existing server, and adds a test of the two together |
| `agentlib registry pin SERVER` | Pins the input schema of every tool of a server in the tool registry |
| `agentlib init --answers agentlib.toml` | Generates a whole workspace again from its recorded answers |

At a terminal a missing answer is asked for, one question at a time. Given as
options, or with no terminal, nothing is asked. `agentlib <command> --help`
lists the options.

Exit codes: 0 done, 1 the command was understood and could not be done, 2 the
command line was wrong. A problem is one line on standard error.

## What a workspace looks like

```text
my-platform/
  pyproject.toml              the uv workspace, test and lint settings
  agentlib.toml               the answers this workspace was generated from
  registry/agents.yaml        which agents exist, and which servers each may call
  registry/mcp-tools.yaml     which servers and tools exist, with schema pins
  policies/agentlib/rules/data.yaml   what every service allows
  agents/hello-agent/         src/, tests/, .env.example, .env, README.md
  mcp-servers/hello-mcp/      src/, tests/, data/, queries/, .env.example, .env, README.md
  tests/                      the workspace as a whole, and agents with their servers
```

Every service reads the same registry and rules files, so an agent and the
servers it calls agree on who may do what.

## The tests it writes

Three kinds, from fastest to most real. All run with no network.

| Test | What is real | Use it to |
| --- | --- | --- |
| `agents/<a>/tests/test_<a>.py` | Your code. Everything else is a fake | Drive tool and graph logic with a scripted model |
| `test_<a>_as_configured.py`, `mcp-servers/<s>/tests/test_<s>.py` | Your code, the rules file, the registries, the CSV data, the audit log | See what a role is allowed and shown, and catch a missing rule |
| `tests/test_<a>_with_<s>.py` | Both services, each with its own configuration | See what the agent's caller gets from the server |

The generated MCP server shows the pattern for governed data: a manager sees
every column, an analyst sees `email` masked, anyone else is denied, and each
of those is a test.

## What the commands will not do

- **Overwrite your work.** A file that exists and differs stops the command
  before anything is written. `--force` overwrites. A service's `.env` is
  never overwritten, even with `--force`.
- **Change a rule.** New rules are appended to the rules file. A rule that is
  there, and every comment, stays as it is.
- **Keep comments in the registries.** The two registry files are rewritten
  when an entry is added, linked or pinned. Their values are kept.

## Where a workspace gets the library

From the checkout the command runs from, by path, until the library is on a
package index. Then: `agentlib init NAME --lib-version ">=0.1"`, or delete the
`[tool.uv.sources]` table in the workspace's `pyproject.toml`.

## Not built yet

`agentlib doctor`, `config explain`, `run`, `graph`, `policy test` and
`update`, and the readers that propose queries from a CSV folder, an OpenAPI
document or the Redshift catalogue.

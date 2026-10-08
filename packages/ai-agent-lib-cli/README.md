# ai-agent-lib-cli

The `agentlib` command. It creates a local development workspace and adds
agents and MCP servers to it. What it writes is working code with its tests:
a hello-world you can run and test before you change a line, and a place to
start every later change with a failing test.

Nothing it writes needs an account or a network connection to run and test.
When a service is ready for AWS, `agentlib deploy` writes what runs it there.

New here? [`docs/getting-started.md`](../../docs/getting-started.md) walks
through all of it, test first.

## Five minutes

From a checkout of the library, with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv run --project <path-to-ai-agent-lib> --package ai-agent-lib-cli agentlib init my-platform
cd my-platform
uv sync --all-packages                             # the workspace gets its own environment
uv run agentlib new mcp hello-mcp                  # an MCP server over sample data
uv run agentlib new agent hello-agent --mcp hello  # an agent that may call it
uv sync --all-packages                             # install the two new services
uv run pytest                                      # every test, all offline
```

Then check the workspace and run the two services:

```bash
uv run agentlib doctor                   # is everything each service needs usable?
uv run agentlib run hello-mcp            # terminal 1: http://127.0.0.1:8100/mcp
uv run hello-agent "Say hello to Ada."   # terminal 2: one question
uv run agentlib run hello-agent          # or serve the agent: POST /invoke on port 8000
```

The agent starts on the `fake` model, which echoes the question. Select a real
model in `agents/hello-agent/.env` when you want one.

## Commands

| Command | What it does |
| --- | --- |
| `agentlib init NAME` | Creates the workspace folder: a uv workspace with shared registry and rules files |
| `agentlib new mcp NAME` | Adds an MCP server: one plain tool, one governed query over a sample CSV file, tests |
| `agentlib new mcp NAME --from-csv DIR` | Adds an MCP server over your own CSV files, with tools proposed from what is in them |
| `agentlib new mcp NAME --from-openapi FILE` | Adds an MCP server over a REST API, with one tool for each GET operation that answers with records |
| `agentlib new mcp NAME --from-redshift SCHEMA` | Adds an MCP server over Redshift tables. Reads the catalogue only; runs locally on stand-in data |
| `agentlib new agent NAME [--mcp SERVER]` | Adds a LangGraph agent with one tool, a command line, an HTTP entry point, tests |
| `agentlib link AGENT SERVER` | Lets an existing agent call an existing server, and adds a test of the two together |
| `agentlib registry pin SERVER` | Pins the input schema of every tool of a server in the tool registry |
| `agentlib init --answers agentlib.toml` | Generates a whole workspace again from its recorded answers |
| `agentlib doctor [SERVICE]` | Builds each service from its `.env` and checks everything it is configured to use. Says what is wrong and how to fix it |
| `agentlib config explain SERVICE` | Lists every setting of a service and where its value came from. Secrets are masked |
| `agentlib run SERVICE [ARGS]` | Starts an agent's HTTP entry point or an MCP server, from any folder of the workspace |
| `agentlib graph AGENT` | Prints the agent's graph as a Mermaid diagram |
| `agentlib policy test [--opa]` | Decides the sample requests in `tests/policy-samples.yaml` and compares with what each expects |
| `agentlib update [--diff]` | Brings generated files up to what this version writes, leaving the files you changed alone |
| `agentlib config options [PORT [NAME]] [--schema]` | Lists every adapter, and each adapter's options with types, defaults and meanings; `--schema` prints JSON Schema for an editor |
| `agentlib check [--opa] [--keep-going]` | Runs what CI runs: ruff, the format check, mypy, pytest and the policy samples. Names the step that failed |
| `agentlib eval [SERVICE]` | Runs the evals of one service or all: its tests marked `eval`, with the real model in its `.env` |
| `agentlib deploy SERVICE [--plan]` | Writes the Terraform and Dockerfiles that run the service on ECS Fargate, from the settings in its `deploy.env`; every IAM permission comes from the adapter that needs it. `--plan` shows it all and writes nothing |

At a terminal a missing answer is asked for, one question at a time. Given as
options, or with no terminal, nothing is asked. `agentlib <command> --help`
lists the options.

Exit codes: 0 done, 1 the command was understood and could not be done, 2 the
command line was wrong. A problem goes to standard error as one line saying
what went wrong, then what was expected, what was found and the fix.

## What a workspace looks like

```text
my-platform/
  pyproject.toml              the uv workspace, test and lint settings
  agentlib.toml               the answers this workspace was generated from
  registry/agents.yaml        which agents exist, and which servers each may call
  registry/mcp-tools.yaml     which servers and tools exist, with schema pins
  agentlib.lock               what each generated file held when it was written
  policies/agentlib/rules/data.yaml   what every service allows
  tests/policy-samples.yaml   requests the rules must allow or deny
  .github/workflows/check.yml the gate CI runs, on Linux and Windows
  agents/hello-agent/         src/, tests/, evals/, .env.example, .env, README.md
  mcp-servers/hello-mcp/      src/, tests/, data/, queries/, .env.example, .env, README.md
  tests/                      the workspace as a whole, and agents with their servers
  deploy/                     after 'agentlib deploy': Terraform and Dockerfiles
```

Every service reads the same registry and rules files, so an agent and the
servers it calls agree on who may do what.

## The tests it writes

From fastest to most real. All but the eval run with no network.

| Test | What is real | Use it to |
| --- | --- | --- |
| `agents/<a>/tests/test_<a>.py` | Your code. Everything else is a fake | Drive tool and graph logic with a scripted model |
| `test_<a>_as_configured.py`, `mcp-servers/<s>/tests/test_<s>.py` | Your code, the rules file, the registries, the CSV data, the audit log | See what a role is allowed and shown, and catch a missing rule |
| `test_<a>_service.py` | The agent behind its HTTP entry point, called in process | Check sign-in, `/invoke` and `/invoke/stream` |
| `tests/test_<a>_with_<s>.py` | Both services, each with its own configuration | See what the agent's caller gets from the server |
| `test_<a>_eval.py` (marked `eval`) | The real model, over `evals/cases.jsonl` | Measure the agent; runs only with `agentlib eval` |

The generated MCP server shows the pattern for governed data: a manager sees
every column, an analyst sees `email` masked, anyone else is denied, and each
of those is a test.

## A server over your own data

`new mcp` with a data option reads what you point it at, proposes tools, runs
each proposal once through the real data source, and writes the ones that
work: the query files, the tools, their registry entries with pins, rules,
policy samples and tests. `--propose` shows the proposals and writes nothing.

| Option | Reads | Proposes | Locally the server runs on |
| --- | --- | --- | --- |
| `--from-csv DIR` | Every `.csv` file: columns, types, how many values repeat | Per table: a lookup by the column that identifies a row, and a filter per column that sorts rows into a few groups | Your files, copied into `data/` |
| `--from-openapi FILE` | An OpenAPI 3 document, JSON or YAML. The API is not called | One query per GET operation that answers with JSON records and takes plain values | The real API when run; `tests/api_responses.json` in tests |
| `--from-redshift SCHEMA` | Table and column names from the catalogue. No row | Per table: a lookup by a column named like a key, or the whole table | Stand-in CSV files with the tables' columns and two made-up rows |

More options: `--per-table N` (how many per table, default 3), `--query NAME`
(keep only these; repeatable), `--source NAME` (the data source's name),
`--base-url URL` (OpenAPI: where the API is, if not the document's first
server), and for Redshift `--database`, `--workgroup` or `--cluster`,
`--db-user`, `--secret-arn`, `--table`, `--aws-profile`, `--aws-region`.
`--from-redshift` needs `ai-agent-lib-aws` installed beside the command:
`uv add --dev ai-agent-lib-aws`.

What to know about the proposals:

- **They are a starting point.** Few and plain on purpose. Edit the query
  files, delete the tools you do not want.
- **Masking is a guess from column names** such as `email`, `phone` and
  `date_of_birth`. Those columns are masked for the `analyst` role and make a
  query `confidential`. Check the guess; a personal column with another name
  is not caught.
- **Names that need quoting are left out**, with a note: a file, table or
  column whose name is not lower-case letters, digits and underscores, or is
  a word of the query language such as `order`.
- **Values from your data end up in generated files.** The CSV reader puts a
  few common values of grouping columns into tool descriptions, and one value
  per parameter into the tests and `agentlib.toml`. Values of columns that
  look personal are never used.
- **The generated tests check columns and masks, not rows.** What the rows
  should be is the first test you write.
- **OpenAPI: optional parameters without a default are left out,** as are
  operations that need a header, a body or a list parameter. The notes say
  which.
- **Redshift: the queries name tables without a schema.** The server's README
  says what to set for that, and has the data source setting for the deployed
  service.

The plan a server was generated from is recorded in `agentlib.toml`, so
`link` and `update` need neither the data nor the API.

## Checking a workspace

`agentlib doctor` changes nothing and calls no model. For the workspace it
checks that the registries and the rules file are valid and agree with each
other, and that the schema pin of every tool matches the running code. For
each service it resolves the settings, builds every adapter the service
selects and runs the adapter's own startup check. A failed check names the
adapter, says what is wrong and gives the fix; the exit code is then 1.
`--no-pins` skips starting the servers.

`agentlib config explain SERVICE` answers "why is it using that?": one line
per setting, with the value and whether it came from a variable (the `.env`
file or the environment), the profile or the default.

## Testing the rules

`tests/policy-samples.yaml` holds requests with the decision each must get:
who asks (application, roles), for what (action, resource, classification),
and `expect: allow` or `deny`, optionally with the rule that must decide it.
The commands add samples for what they generate. Add one for every rule you
write, before you write the rule.

```bash
uv run agentlib policy test          # decided by the rules engine the services use locally
uv run agentlib policy test --opa    # also by OPA with the platform's Rego bundle; both must agree
```

`--opa` needs the `opa` program on the path. It is the check that the rules
you tested locally decide the same way on the server side.

## Updating generated code

`agentlib.lock` records a digest of every generated file. `agentlib update`
uses it to tell your files from untouched ones: an untouched file is replaced
with what the templates write now, a file you changed is left alone and
listed, and `--diff` shows what the templates would change in it. A file you
deleted is not brought back. Commit the lock file.

## What the commands will not do

- **Overwrite your work.** A file that exists and differs stops the command
  before anything is written. `--force` overwrites. A service's `.env` is
  never overwritten, even with `--force`.
- **Change a rule.** New rules are appended to the rules file. A rule that is
  there, and every comment, stays as it is.
- **Keep comments in the registries.** The two registry files are rewritten
  when an entry is added, linked or pinned. Their values are kept.
- **Touch the registries, rules, samples or `.env` files on update.**
  `agentlib update` only writes source, tests and documentation.
- **Deploy anything.** `agentlib deploy` writes files. It never calls AWS and
  never runs `terraform`; after its first run it rewrites only the files it
  derives (`settings.tf`, `permissions.tf`, the reference module and the OPA
  image), never yours.

## Working on agentlib

| Module | What it does |
| --- | --- |
| `app.py`, `commands/` | The command line: `create` writes, `look` reads and runs, `rules` tries the policy, `check` runs the gate and evals, `deploy` writes the deployment |
| `toolbox.py` | Everything a command uses to reach outside the process: the formatter, the pin reader, running a service, OPA, AWS, prompts |
| `scaffold.py`, `governance.py`, `render.py` | What is written: templates, registry entries, rules and samples, all-or-nothing writes |
| `dataplan.py`, `readers.py`, `read_*.py`, `proposals.py` | From your data to the queries a server offers |
| `diagnostics.py`, `policy.py`, `processes.py` | Checking, deciding samples, running generated code |
| `deploy.py` | From a service's `deploy.env` to its plan, permissions and Terraform |

A command never reaches for a tool itself: it is given a `Toolbox`. A test
gives it one with fakes, so no test patches a module:

```python
from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.testing import toolbox_for_tests

assert main(["new", "mcp", "hello-mcp"], toolbox=toolbox_for_tests(pins={})) == 0
```

## Where a workspace gets the library

From the checkout the command runs from, by path, until the library is on a
package index. Then: `agentlib init NAME --lib-version ">=0.1"`, or delete the
`[tool.uv.sources]` table in the workspace's `pyproject.toml`.


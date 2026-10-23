# ai-agent-lib-cli

The CLI creates workspaces, agents, and MCP servers with runnable Python code,
configuration, and tests. Install and run generated services using Python and pip.

Installing the package adds the `agentcraft2` command, and `ac2` as its short name.
Both run the same program, so `ac2 doctor` and `agentcraft2 doctor` are the same
command. The examples below use `python -m ai_agent_lib_cli`, which also works.

For your first project, follow the [getting-started tutorial](../../docs/getting-started.md).
To test the included rmprep agent in Agent Lab, use the repository's
[startup guide](../../docs/local-startup.md). The commands below create
your own workspace.

## Create a workspace

Open **Command Prompt** in the library checkout, using your team's Python and
approved pip package source. `set "PYTHON=..."` stores the Python command;
`"%PYTHON%"` runs it. If your team supplies a specific executable, set `PYTHON`
to its full path. Choose another name if `my-platform` already exists:

```bat
set "LIBRARY=%CD%"
set "PYTHON=python"
"%PYTHON%" -m pip install -e packages/ai-agent-lib-core -e packages/ai-agent-lib-cli
"%PYTHON%" -m ai_agent_lib_cli init my-platform --dir .. --lib-path "%LIBRARY%" --owner my-team
cd /d ..\my-platform
"%PYTHON%" -m ai_agent_lib_cli new agent hello-agent --description "A practice assistant" --model fake
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m pytest
"%PYTHON%" -m hello_agent "Say hello to Ada."
```

The tests use scripted replies; the configured fake model echoes your question.
Next, add shared tools with [the MCP tutorial](../../docs/getting-started.md#5-add-a-shared-tool-server).

In the same window, check the service and start its HTTP endpoint:

```bat
"%PYTHON%" -m ai_agent_lib_cli doctor hello-agent
"%PYTHON%" -m ai_agent_lib_cli run hello-agent
```

Keep that window open; stop with **Ctrl+C**. In a new window, set `PYTHON`
again and change to your workspace root before running commands. The `agentcraft2`
shorthand below is equivalent to `"%PYTHON%" -m ai_agent_lib_cli`.

On macOS/Linux, use a bash or zsh terminal. Write `LIBRARY="$PWD"` and
`PYTHON=python3` for the two `set` lines, `"$PYTHON"` for `"%PYTHON%"`, and
`cd ../my-platform` for `cd /d ..\my-platform`; paths use `/`.

## Commands

| Command | What it does |
| --- | --- |
| `agentcraft2 init NAME` | Creates the workspace folder with shared registry and rules files |
| `agentcraft2 install [--no-dev]` | Installs all workspace services and their dependencies using the current Python's pip |
| `agentcraft2 new mcp NAME` | Adds an MCP server: one plain tool, one governed query over a sample CSV file, tests |
| `agentcraft2 new mcp NAME --from-csv DIR` | Adds an MCP server over your own CSV files, with tools proposed from what is in them |
| `agentcraft2 new mcp NAME --from-openapi FILE` | Adds an MCP server over a REST API, with one tool for each GET operation that answers with records |
| `agentcraft2 new mcp NAME --from-redshift SCHEMA` | Adds an MCP server over Redshift tables. Reads the catalogue only; runs locally on stand-in data |
| `agentcraft2 new agent NAME [--mcp SERVER]` | Adds a LangGraph agent with one tool, a command line, an HTTP entry point, tests |
| `agentcraft2 link AGENT SERVER` | Lets an existing agent call an existing server, and adds a test of the two together |
| `agentcraft2 registry pin SERVER` | Pins the input schema of every tool of a server in the tool registry |
| `agentcraft2 init --answers agentcraft2.toml` | Generates a whole workspace again from its recorded answers |
| `agentcraft2 doctor [SERVICE]` | Builds each service from its `.env` and checks everything it is configured to use. Says what is wrong and how to fix it |
| `agentcraft2 config explain SERVICE` | Lists every setting of a service and where its value came from. Secrets are masked |
| `agentcraft2 run SERVICE [ARGS]` | Starts an agent's HTTP entry point or an MCP server, from any folder of the workspace |
| `agentcraft2 graph AGENT` | Prints the agent's graph as a Mermaid diagram |
| `agentcraft2 policy test [--opa]` | Decides the sample requests in `tests/policy-samples.yaml` and compares with what each expects |
| `agentcraft2 update [--plan] [--diff]` | Brings generated files up to what this version writes, leaving the files you changed alone. `--plan` previews every change and writes nothing; `--diff` shows what the templates would change in the files you changed (without `--plan`, `--diff` also applies the update). See [Updating generated code](#updating-generated-code) |
| `agentcraft2 config options [PORT [NAME]] [--schema]` | Lists every adapter, and each adapter's options with types, defaults and meanings; `--schema` prints JSON Schema for an editor |
| `agentcraft2 check [--opa] [--keep-going]` | Runs what CI runs: ruff, the format check, mypy, pytest and the policy samples. Names the step that failed |
| `agentcraft2 eval [SERVICE] [--accept]` | Runs the evals of one service or all: its tests marked `eval`, with the real model in its `.env`. `--accept` runs one service's evals and, if they pass, promotes the report to `evals/baseline.json`; later runs gate on it. See [Measure an agent with evals](../../docs/evals.md) |
| `agentcraft2 deploy SERVICE [--plan]` | Writes the Terraform and Dockerfiles that run the service on ECS Fargate, from the settings in its `deploy.env`; every IAM permission comes from the adapter that needs it. `--plan` shows it all and writes nothing |

At a terminal a missing answer is asked for, one question at a time. Given as
options, or with no terminal, nothing is asked. `agentcraft2 <command> --help`
lists the options.

Exit codes: 0 done, 1 the command was understood and could not be done, 2 the
command line was wrong. A problem goes to standard error as one line saying
what went wrong, then what was expected, what was found and the fix.

## What a workspace looks like

```text
my-platform/
  pyproject.toml              dependencies, test and lint settings
  agentcraft2.toml               the answers this workspace was generated from
  registry/agents.yaml        which agents exist, and which servers each may call
  registry/mcp-tools.yaml     which servers and tools exist, with schema pins
  agentcraft2.lock               what each generated file held when it was written
  policies/agentcraft2/rules/data.yaml   what every service allows
  tests/policy-samples.yaml   requests the rules must allow or deny
  .github/workflows/check.yml the gate CI runs, on Linux and Windows
  agents/hello-agent/         src/, tests/, evals/, .env.example, .env, README.md
  mcp-servers/hello-mcp/      src/, tests/, data/, queries/, .env.example, .env, README.md
  tests/                      the workspace as a whole, and agents with their servers
  deploy/                     after 'agentcraft2 deploy': Terraform and Dockerfiles
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
| `test_<a>_eval.py` (marked `eval`) | The real model, over `evals/cases.jsonl` | Measure the agent; runs only with `agentcraft2 eval` |

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
`python -m pip install ai-agent-lib-aws`.

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
  per parameter into the tests and `agentcraft2.toml`. Values of columns that
  look personal are never used.
- **The generated tests check columns and masks, not rows.** What the rows
  should be is the first test you write.
- **OpenAPI: optional parameters without a default are left out,** as are
  operations that need a header, a body or a list parameter. The notes say
  which.
- **Redshift: the queries name tables without a schema.** The server's README
  says what to set for that, and has the data source setting for the deployed
  service.

The plan a server was generated from is recorded in `agentcraft2.toml`, so
`link` and `update` need neither the data nor the API.

## Checking a workspace

`agentcraft2 doctor` validates configuration and starts configured adapters for their
checks. It calls no model; provider startup can create local state or contact
configured services. For the workspace it
checks that the registries and the rules file are valid and agree with each
other, and that the schema pin of every tool matches the running code. For
each service it resolves the settings, builds every adapter the service
selects and runs the adapter's own startup check. A failed check names the
adapter, says what is wrong and gives the fix; the exit code is then 1.
`--no-pins` skips starting the servers.

`agentcraft2 config explain SERVICE` answers "why is it using that?": one line
per setting, with the value and whether it came from a variable (the `.env`
file or the environment), the profile or the default.

## Testing the rules

`tests/policy-samples.yaml` holds requests with the decision each must get:
who asks (application, roles), for what (action, resource, classification),
and `expect: allow` or `deny`, optionally with the rule that must decide it.
The commands add samples for what they generate. Add one for every rule you
write, before you write the rule.

```bash
python -m ai_agent_lib_cli policy test          # decided by the rules engine the services use locally
python -m ai_agent_lib_cli policy test --opa    # also by OPA with the platform's Rego bundle; both must agree
```

`--opa` needs the `opa` program on the path. It is the check that the rules
you tested locally decide the same way on the server side.

## Updating generated code

`agentcraft2.lock` records a digest of every generated file. `agentcraft2 update`
uses it to tell your files from untouched ones: an untouched file is replaced
with what the templates write now, a file you changed is left alone and
listed, and a file you deleted is not brought back. Commit the lock file.

After upgrading the library, start from a committed or backed-up workspace and
run from the workspace root:

```bat
agentcraft2 update --plan
agentcraft2 update --diff
agentcraft2 install
agentcraft2 check
```

**`--plan` is the dry run.** It lists every file the update would replace or
create, and every file it would leave alone because you edited it, and writes
nothing. Add `--diff` (`update --plan --diff`) to also see how each edited file
differs from what the templates now write.

**`update --diff` without `--plan` applies changes.** It replaces the untouched
files, leaves the ones you edited alone, and prints their proposed template
changes for you to merge. Review the resulting changes before running
`agentcraft2 install` and `agentcraft2 check`.

## What the commands will not do

- **Overwrite your work.** A file that exists and differs stops the command
  before anything is written. `--force` overwrites. A service's `.env` is
  never overwritten, even with `--force`.
- **Change a rule.** New rules are appended to the rules file. A rule that is
  there, and every comment, stays as it is.
- **Keep comments in the registries.** The two registry files are rewritten
  when an entry is added, linked or pinned. Their values are kept.
- **Touch the registries, rules, samples or `.env` files on update.**
  `agentcraft2 update` only writes source, tests and documentation.
- **Deploy anything.** `agentcraft2 deploy` writes files. It never calls AWS and
  never runs `terraform`; after its first run it rewrites only the files it
  derives (`settings.tf`, `permissions.tf`, the reference module and the OPA
  image), never yours.

## Working on agentcraft2

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
package index. Then create a workspace with
`agentcraft2 init NAME --lib-version ">=0.1"`. The recorded library source in
`agentcraft2.toml` controls `agentcraft2 install`; optional `[tool.uv.sources]`
metadata is only used by uv.

## Install a generated workspace with pip

Use `python -m ai_agent_lib_cli install` after adding services or changing their
dependencies. It resolves all members and the development tools together in the
current Python environment. `--no-dev` omits the development group. Keep Agent
Lab in its [separate UI environment](../ai-agent-lib-ui/README.md#1-prepare-the-ui-packages).

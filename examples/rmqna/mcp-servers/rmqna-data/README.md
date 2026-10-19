# rmqna-data

Read-only SQL over the relationship-manager data, behind a validator and a contained engine.

This MCP server exposes three tools for the rmqna agent to call. The agent writes the SQL; this
server decides whether it may run, runs it, and returns the rows. The library checks caller
identity, registered tools, permissions and guardrails, then records an audit event. The SQL is not
a named query, so the library's data-query decisions do not apply here: the controls that take
their place are recorded in [`docs/sql-governance.md`](../../docs/sql-governance.md), and this page
says how to use the server.

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
| `rmqna-data.execute_sql(sql)` | Runs one read-only SELECT over the 31 loaded tables and returns `{columns, rows, row_count, truncated, elapsed_ms}` |
| `rmqna-data.search_column_values(table, column, search)` | Returns up to 25 distinct stored values of a column that contain the text, ignoring case: `{values, truncated}` |
| `rmqna-data.get_schema()` | Returns the schema document as `{schema}`: the same file the agent's prompt carries |

A statement that is refused or fails is **not** a tool error. It comes back as
`{error_kind, message}`, so the agent can read the message and repair its SQL.

| `error_kind` | When |
| --- | --- |
| `not_a_select` | Anything but one SELECT: a write, DDL, PRAGMA, ATTACH, COPY, SET, `SELECT ... INTO`, and PIVOT or UNPIVOT in any form |
| `multiple_statements` | More than one statement in the input |
| `set_operation` | `UNION`, `EXCEPT` or `INTERSECT`, anywhere in the statement |
| `unknown_table` | A table that is not loaded (also a schema prefix, or an unknown column in `search_column_values`) |
| `forbidden_function` | A file, environment or catalog function, any function in FROM, or a row or string generator anywhere (`range`, `generate_series`, `unnest`, `repeat`, `list_resize`, `rpad`, `lpad`, `printf`, `format`, `equi_width_bins`) |
| `parse_error` | Text sqlglot cannot read as SQL, a LIMIT that is not a plain row count, or a statement too tangled to plan (over 16 joined sources in a SELECT, over 32 joins in a statement, or over 32 CTE and subquery references) |
| `execution_error` | DuckDB failed on the statement (the message is DuckDB's own text), or the result is over 2 MB once serialized |
| `timeout` | The statement ran longer than 30 seconds and was interrupted |

A result is cut at 200 rows (`truncated: true`) and refused over 2 MB. Tables are named bare: `FROM accounts`. A statement that times out is interrupted, but DuckDB's planner and a single huge value cannot be interrupted, so one can keep running briefly after the caller is told; `docs/sql-governance.md` says what that means.

## Test it

```bat
"%PYTHON%" -m pytest mcp-servers/rmqna-data
```

The tests run DuckDB for real over the workspace's `data/` folder, and the server in process with
the workspace's policy rules:

| Suite | Shows |
| --- | --- |
| `tests/test_rmqna_data_validate.py` | What the validator lets through and refuses, one refusal kind at a time |
| `tests/test_rmqna_data_engine.py` | The tables loaded, the row cap, the timeout, the lock on the engine, the value lookup, and that the engine's column types are the ones the schema document states |
| `tests/test_rmqna_data.py` | The tools as an agent meets them: registration and pins, who may call them, refusals as results, and what the audit log and the server log do not hold |

| Role | Tool call |
| --- | --- |
| `analyst`, `manager` | allowed |
| anything else | denied |

**Expected result:** the tests pass. To see a new refusal, add a case to the parametrized list in
`tests/test_rmqna_data.py::test_a_refusal_is_a_normal_result_the_agent_can_read`.

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

The server's log has one line per call: the tool, its duration, row and column counts, whether
the result was cut, and the error kind. It never holds SQL or a search text. The SQL is on the
agent's tool-call span in Phoenix, and in the model trace.

## What to read

These file paths are relative to this service folder:

| File | Shows |
| --- | --- |
| `src/rmqna_data/server.py` | The three tools and their library integrations; the one place the engine is built |
| `src/rmqna_data/validate.py` | The validator: what may run, and the refusal each rule gives |
| `src/rmqna_data/engine.py` | The engine: loading, the lock, the row cap, the timeout, the value lookup |
| `src/rmqna_data/settings.py` | Where the data folder and the schema document are |
| `.env.example` | How the service is configured locally |

The data is the workspace's `data/` folder, one table per file, except `data_model.csv` and
`rm_scenarios.csv`, which are never loaded. The schema document is the agent package's
`prompts/schema_v1.md`; the committed copy is rendered by `python -m tools.build_schema`.

## Change a tool

1. Write a failing test first: a validator case in `tests/test_rmqna_data_validate.py`, an engine
   case in `tests/test_rmqna_data_engine.py`, or a tool case in `tests/test_rmqna_data.py`.
2. Change `validate.py`, `engine.py` or `server.py`. A new refusal rule needs a new `error_kind`
   only if the agent should treat it differently; the current kinds are used by the agent's prompt.
3. If a tool's arguments changed, pin it again from the workspace root:
   `"%PYTHON%" -m ai_agent_lib_cli registry pin rmqna-data`.
4. Run `"%PYTHON%" -m ai_agent_lib_cli check` from the workspace root.

## Deployment

This server is local-only. It loads the workspace's `data/` folder and the agent's schema document
by their place in the repository, and runs SQL a model wrote; the controls that make that
acceptable are for synthetic data on a developer's machine (`docs/sql-governance.md` says what
would have to change). `agentlib deploy` is not set up for it.

# rmqna: relationship-manager questions answered with SQL

An RM asks a quantitative question about the book ("which clients' utilization rose last quarter?",
"what is our weighted pipeline by product?"). The agent writes one read-only DuckDB SELECT over the
bank's tables, a server validates and runs it, and the agent answers from the rows, saying which
tables, filters and as-of date it used. The data is the same synthetic snapshot rmprep uses: 46
clients and 31 tables of relationships, credit, deposits, payments, service issues, pipeline and
market rates, frozen at **2026-06-30**. It contains real company names; the figures are supplied
examples, not verified facts about those companies.

```text
Question -> rmqna (agent) -> model -> execute_sql / search_column_values -> rmqna-data -> DuckDB
                                ^                                                          |
                                +------------ rows, or {error_kind, message} -------------+
```

- **`agents/rmqna`** is one LangGraph agent. Its behaviour is its prompt, not extra graph nodes:
  clarify only when the answer would change, look a typed name up before filtering on it, write the
  SQL, repair from the server's error at most twice, look once more at an empty result, then answer
  with a `Basis:` line. It declines writes, questions the tables cannot answer, decisions, and bulk
  listings of personal data. The prompt carries the schema document and twelve worked examples, one
  static block, so the provider can cache it.
- **`mcp-servers/rmqna-data`** offers three tools: `execute_sql`, `search_column_values` and
  `get_schema`. It decides whether SQL may run and runs it in a contained in-memory engine.

## Model-written SQL is a deliberate departure

The library governs data through named, reviewed queries (rmprep does). rmqna lets the model write
the statement, so the server, not the model, is the boundary: an AST allowlist (one SELECT, known
tables, a function denylist, no set operations), a 200-row and 2 MB cap, a 30-second timeout, a
memory limit, external access and disk spill switched off, and content-free server logs, on top of
the library's tool-level policy, audit and pins. What this gives up, why it is acceptable for
synthetic data on one machine, and what graduating it would take are in the ADR,
[`docs/sql-governance.md`](docs/sql-governance.md). Read it before changing the server, and before
pointing the engine at anything real.

## 1. Install

Use Python 3.11 or newer in a virtual environment. From this workspace's folder, with the library
checkout two levels up (see `agentlib.toml`):

```bash
python -m pip install -e ../../packages/ai-agent-lib-core -e ../../packages/ai-agent-lib-cli
python -m ai_agent_lib_cli install
python -m ai_agent_lib_cli check
```

`install` installs both services and the development tools; `check` runs lint, format, types, the
tests and the policy samples. Every command below runs from this folder, and works the same in
Command Prompt (set a variable there with `set NAME=value`, in bash with `export NAME=value`).
The tests and `check` use scripted models and the local data; none needs a key or a network.

## 2. Run it

Set the model in `agents/rmqna/.env` (`AC2_MODEL_ID`, and `ANTHROPIC_API_KEY` or your approved
credential mechanism). Running the agent calls the real model and uses billable tokens.

```bash
python -m ai_agent_lib_cli run rmqna-data     # terminal 1: MCP server on :8300, wait for "ready"
python -m ai_agent_lib_cli run rmqna          # terminal 2: agent on :8200 (also: rmqna-serve)
python -m rmqna "Which clients have a covenant in breach?" --thread demo    # terminal 3, one question
```

The agent's own README, [`agents/rmqna/README.md`](agents/rmqna/README.md), has the HTTP request
and the live chat UI. With no model and no servers, `python -m rmqna.examples.trace_react` runs the
real server and engine in process under a scripted model and writes its trace to
`agents/rmqna/.agentlib/react-demo/`; read it with `python examples/read_trace.py <file>`.

Good first questions: *Which clients are using more than 80% of their credit lines?*, *What is our
weighted pipeline for the next 90 days, by product?*, *What deals does Wawona have open?* (a name
lookup) and *How many open service tickets does Vermont have?* (two clients match, so it asks).

## 3. Debug the SQL in Phoenix

The model writes the SQL as the argument of its `execute_sql` call, so the call's trace span is where
a wrong answer is traced to a wrong statement. No code is needed, only settings.

1. Start Phoenix on your machine with the library's launcher (`start-phoenix.bat` on Windows,
   `./start-phoenix.sh` elsewhere: `setup` once, then `start`; see `docs/phoenix-trial.md` at the
   library root). It serves `http://127.0.0.1:6006`.
2. In `agents/rmqna/.env`, turn tracing on **and** content capture on, then restart the agent:

   ```dotenv
   AC2_PHOENIX_ENABLED=true
   AC2_PHOENIX_CAPTURE_CONTENT=true
   PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006
   PHOENIX_PROJECT_NAME=rmqna
   ```

3. Ask a question and open the project `rmqna` in Phoenix. Each question is one trace whose root is
   `rmqna.turn`. Filter the spans by `name == 'rmqna-data.execute_sql'` to see every statement the
   agent sent. On such a span, **Input** is `{"sql": "..."}`, **Output** is the framed result
   (columns and rows, or `{error_kind, message}`), and `agentlib.mcp_server` is `rmqna-data`. A
   repair reads as two consecutive spans: the first's output is the error, the next one's input is
   the changed statement. The `model` spans before and after hold the same call in their messages.
   `agentlib.model_calls` and `agentlib.tool_calls` on the root count them.

Content capture is **all or nothing for the process**. With `AC2_PHOENIX_CAPTURE_CONTENT=false` (the
default) a trace has names, timings, token counts and ids, and no SQL on any span. With `true` it also
has every model message, which here means the whole prompt (instructions, schema document and
examples, about 27,000 tokens) on every model span, and every tool result, which means client names
and figures. There is no setting that captures the SQL alone. That is acceptable for this synthetic
data on a developer's machine; do not turn it on for real data, and turn it off when you finish.

`AC2_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl` is the alternative with no Phoenix at all: it
records every model request and response, tool-call arguments (the SQL) included, to a JSONL file
under the agent's `.agentlib/`, whatever the Phoenix settings say. It holds content too, so keep it
private. `agents/rmqna/docs/tracing.md` has the rest of the Phoenix settings. The server's own log
never holds SQL by design: one line per call with its duration, row and column counts, truncated
flag and error kind.

## 4. Measure the agent with the evals

`agents/rmqna/evals/cases.jsonl` is a golden set of 33 synthetic questions: joins along the foreign
keys, aggregations, windows measured from 2026-06-30, name lookups, results that are legitimately
empty, two ambiguities the agent should ask about, and refusals. None is one of the prompt's worked
examples (a test checks). A case with a `reference_sql` is scored by **execution result**, not by SQL
text: the case's statement and the agent's last statement run through the same engine and the
results are compared (rows in any order, a column by its name or, when the agent aliased it, by its
values and in the reference's relative order, numbers within 1e-9, a date equals its midnight
timestamp, extra columns ignored). A case without one expects a clarifying question or a refusal, so
it passes only if the agent sent no SQL, and its `answer_contains` checks the words. Two metrics come
out: `result_match` and `answer_contains`.

```bash
python -m ai_agent_lib_cli eval rmqna            # real model, tools in process; no server to start
python -m ai_agent_lib_cli eval rmqna --accept   # re-runs the whole eval (a second billable live run)
                                                 # and saves that fresh passing report as the baseline
```

The model is real and live (set `AC2_MODEL_ID` and the key in `agents/rmqna/.env`; without a real
provider the eval skips itself); the `rmqna-data` tools are connected in process through the
library's `InProcessMcpConnector`, over the real engine. A run asks all 33 questions with a model
call or several each, so it costs tokens.

- A run writes `agents/rmqna/.agentlib/eval-report.json` (scores per case) and
  `.agentlib/eval-outputs.json` (the answer and every statement the agent sent, per case). Both are
  private to your machine. For a failed case, compare its statements with the case's `reference_sql`.
- There is no `evals/baseline.json` yet. A baseline depends on the model, so the first one is minted
  by a maintainer's live `eval rmqna --accept` and committed. After that every run also fails when a
  metric drops more than 0.05 below the baseline; `--accept` again moves the bar on purpose. The
  absolute bars (`result_match` 0.5, `answer_contains` 0.8) are starting values in
  `agents/rmqna/tests/test_rmqna_eval.py`: raise them once a baseline run shows what the agent reaches.
- The eval itself is tested on every CI run, with a scripted model:
  `tests/test_eval_harness.py` (the task, the scorers and a table of results that do and do not
  match) and `tests/test_eval_cases.py` (every `reference_sql` runs on the engine and returns what
  its case pins, follows the schema document's conventions, and is disjoint from the examples).
- To add a case, write the question and a `reference_sql` that returns at most 20 rows (the agent's
  own `LIMIT 20` must not cut it) and does not end on a tie, then run
  `python -m pytest tests/test_eval_cases.py`. It names the case and shows what the statement
  returned, to put in `expected.result`. Write window questions as an RM would; the snapshot date
  is 2026-06-30.

## 5. What is here

| Path | What it contains |
| --- | --- |
| `agents/rmqna/` | The agent: `src/rmqna/prompts/` (instructions, schema document and examples, versioned), `graph.py`, `service.py`, `evals/`, tests and its own README |
| `mcp-servers/rmqna-data/` | The server: `validate.py` (what may run), `engine.py` (how it runs), `server.py` (the three tools), tests and its own README |
| `data/` | The CSV snapshot, one file per table, read in place. `data_model.csv` is the dictionary and is never loaded |
| `tools/` | `build_schema.py` and `schema_notes.toml`: renders the committed schema document from the data and the notes |
| `docs/sql-governance.md` | The ADR: the controls, the tests that pin them, the residual risks |
| `registry/`, `policies/` | The agent's allowed servers, the tools and their pins, and the rules (analysts and managers may call the tools; everyone else is denied) |
| `tests/` | Workspace tests: the prompt's examples run on the real engine, what the prompt says about the server is true, agent and server together, the eval harness and its cases |
| `examples/read_trace.py` | Reads a model trace as formatted JSON |

To change what the agent does, copy the prompt file to a new version (`system_v2.md`), change the
copy and move the pin in `src/rmqna/prompts/__init__.py`; then run `check`. To change what the server
allows, start from a failing test in `mcp-servers/rmqna-data/tests/` and read the ADR first. The
local development identity has the `analyst` role; change `AC2_IDENTITY_OPTIONS` in the agent's `.env`
to see the policy deny another.

rmqna is local only: the server loads this workspace's `data/` folder and the agent's schema document
by their place in the repository, and `agentlib deploy` is not set up for it.

# rmqna — relationship-manager Q&A over SQL: design

19 October 2026. Maintainer request: a new agent + MCP server pair called
**rmqna** (relationship manager question and answer), created with the
`agentlib` CLI "similar to rmprep". The MCP server uses a **copy** of the
rmprep data folder; the CSVs plus `data_model.csv` become schema
descriptions and relationships; **the agent writes SQL**, the MCP server
executes it and returns rows, the model answers the user's question, and
**Phoenix captures the generated SQL** for debugging. Industry text-to-SQL
practice was researched first (six-angle review, synthesized in the rmqna
research report; its ten decisions are cited as R1–R10 below).

## Intent

Relationship managers ask ad-hoc quantitative questions ("which clients'
utilization rose last quarter?", "weighted pipeline by product?") that the
35 fixed rmprep queries cannot anticipate. rmqna answers them by
translating the question to SQL over the same banking/CRM snapshot (31
queryable tables: the 33 copied CSVs minus `rm_scenarios`, with
`data_model` kept as metadata only) — a deliberate, contained departure
from the library's named-query philosophy, built as an example
workspace, not a change to the library.

## Goals

1. A new `examples/rmqna` workspace scaffolded by the agentlib CLI (init,
   new mcp, new agent), with its own copy of the rmprep `data/` folder,
   its own registry, policies, and `.env` files — a sibling of rmprep,
   not a tenant of it.
2. The rmqna agent turns an RM question into **DuckDB SQL** using a schema
   document rendered from `data_model.csv` (descriptions, PK/UK/FK
   relationships, enumerated values), executes it through the MCP server,
   and answers with provenance: tables used, filters applied, the as-of
   basis, and the SQL on request.
3. The MCP server is the **containment boundary** (R4): it validates every
   statement (single read-only SELECT, known tables only, no escape
   hatches), caps rows, times out, and runs against an in-memory DuckDB
   with external access disabled. A hostile or malformed statement is
   refused with a structured error the agent can repair from.
4. Phoenix shows the generated SQL on the agent's tool-call span when
   content capture is on — using the existing tracing seam, no new
   tracing machinery (R10).
5. Two testing layers, clearly separated: ordinary pytest tests run
   hermetically on scripted/fake models (engine, validator, agent flow),
   while the eval suite proper (library `run_eval`, ~30 synthetic golden
   cases, execution-result scoring) runs the **real model** — the
   library's own eval convention (`pytest -m eval` skips on the fake
   provider) — and gates regressions against a committed baseline.
6. The whole repo stays green: `agentlib check` in the new workspace,
   repo-wide pytest/mypy/ruff/import-linter/architecture tests, and the
   library's own packages unchanged except where this design names them.

## Non-goals / anti-goals

- **No change to the library's data governance contract.** The
  `DataSource` port keeps its "a caller never supplies query text"
  promise (contracts/data.py:202-207, Specification.md:399); the
  `DataSourceContract` suite is untouched. Free-form SQL lives in
  example-local code, like rmprep's own composite tools.
- No deployable free-form SQL backend: rmqna is local-only by
  construction (DuckDB over CSVs, like `duckdb_csv`). Redshift is out of
  scope.
- No multi-agent decomposition pipelines, trained selectors, or LLM
  self-critique stages (R3): one generation call plus a bounded repair
  loop.
- No schema-linking/pruning stage (R2): 33 tables fit in context; the
  schema document ships whole.
- No embedding-based few-shot retrieval in v1: the curated example pairs
  are few enough to embed statically in the prompt (designer's call; the
  library has no retrieval infrastructure and R6's measured gains come
  from the examples themselves).
- No write tools, consistent with the Specification's read-only stance.
- Anti-goal: silently wrong answers. Ambiguity gets one clarifying
  question; empty results get one re-examination and then an explicit
  caveat naming the filters applied — never a bare "none" (R5, R9).
- Anti-goal: answer-key leakage. `rm_scenarios.csv` is never loaded into
  the engine, never in the schema document, never in a tool payload —
  the same containment rmprep enforces for its golden data.

## Constraints (from the codebase, verified by inventory)

- Import contracts: example code is outside import-linter roots but under
  repo-wide strict mypy and ruff; it may import `duckdb`, `sqlglot`, and
  library modules directly.
- Every MCP tool must be registered with a schema_sha256 pin
  (`agentlib registry pin rmqna-data`); tool calls are governed by the
  default-deny rules file + samples; new rules are appended with
  allow/deny samples.
- Generated workspaces: tests read `.env.example`, never `.env`; scripted
  or fake model only in committed tests; pytest-socket loopback-only.
- The start-local launcher and its architecture tests hardcode the rmprep
  stack; rmqna does not join it in v1 (open question 1 below).
- Service names ≤ 28 chars, rule IDs ≤ 63; ports: rmprep holds 8000/8100.

## Decided design

### D1 — scaffold and layout

`agentlib init rmqna` under `examples/`, then
`agentlib new mcp rmqna-data --port 8300` (the hello-world sample plan —
its generated sample tool and query are then replaced by the custom
server, exactly the rmprep pattern) and
`agentlib new agent rmqna --model anthropic --mcp rmqna-data --port 8200`.
Ports 8200/8300 keep the rmprep stack co-runnable. `cp -R
examples/rmprep/data examples/rmqna/data`, then **delete
`rm_scenarios.csv` from the copy** (it is rmprep's eval answer key; rmqna
ships its own golden cases instead). The workspace joins the root repo's
pytest/mypy coverage the same way rmprep does (root pyproject testpaths
and mypy_path extended; designer's call — mirror rmprep's entries).

### D2 — schema document, rendered from data_model.csv (R1)

A build script in the workspace (`tools/build_schema.py`, runnable as a
module) renders `agents/rmqna/src/rmqna/prompts/schema/v1.md` from the
data copy:

- Per table: name, domain, a one-line role, then columns as
  `name type [PK|UK|FK→target] [nullable] — description`. The dictionary
  is strictly column-level, so table roles, domain overviews and metric
  definitions (D4) are **hand-authored** in a committed notes file the
  build script renders (`tools/schema_notes.toml`), written by the
  builder from the column prose and rmprep's reviewed queries, and
  maintainer-reviewable in one place (designer's call). Author-time
  consultation of rmprep's 35 queries and `rm_scenarios.csv` for metric
  semantics is allowed; the runtime containment rule below is absolute.
- A `[Relationships]` section per domain listing every FK edge from the
  dictionary (`column → table.column`), with the standing rule stated
  once: *join only along these declared paths*.
- Value grounding, profiled from the CSVs with DuckDB at build time:
  complete distinct-value lists for low-cardinality columns (≤ 24
  distinct values — status codes, stages, currencies, grades), 3 samples
  otherwise. Columns whose dictionary description marks them as free-text
  identifiers of people (contact names, emails, phones) are **excluded
  from sampling** (structure still documented).
- `rm_scenarios` and `data_model` itself are excluded from the document
  and from the engine's loaded tables (metadata and answer keys are not
  queryable data).

The rendered document is committed, and a test regenerates it and
compares byte-for-byte (the repo's generated-docs convention), so schema
drift fails CI rather than silently misleading the model. The agent loads
it as a versioned prompt resource (rmprep's prompt convention: a new file,
never a silent change).

### D3 — the MCP server: three tools, one contained engine

`rmqna-data` replaces the generated sample tools with (server.py declares
transport-only signatures; the engine lives behind a small protocol, the
rmprep layering):

- `rmqna-data.execute_sql(sql: str)` — validate, execute, return
  `{columns, rows, row_count, truncated, elapsed_ms}` via the standard
  payload shape. A refused or failing statement returns a structured
  error payload (`{error_kind, message}` with DuckDB's verbatim message)
  as a normal tool result, so the agent's repair loop can read it (R3);
  the tool raises only for library-level failures.
- `rmqna-data.search_column_values(table: str, column: str, search: str)`
  — entity/value resolution (R5): identifiers validated against the
  loaded schema (allowlist, not quoting), the search value bound as a
  parameter to an internal `SELECT DISTINCT … ILIKE` statement, capped.
- `rmqna-data.get_schema()` — returns the committed schema document, so
  the pair is self-describing for debugging and evals.

**The engine** (example-local, `rmqna_data/engine.py`): loads each
allowed CSV into an in-memory DuckDB exactly as `data_duckdb.py` does
(same naming regex), then `SET enable_external_access = false`; per-call
cursor; `asyncio.to_thread` + `wait_for` timeout with `cursor.interrupt()`
(30 s default); row cap 200 with `LIMIT cap+1` truncation detection.
Validation before execution, with sqlglot (already a core dependency),
dialect `duckdb` (R7; the redshift-for-deployability convention is
knowingly traded away — this example is local-only):

1. Exactly one statement; it must be a plain `exp.Select` (reject
   Insert/Update/Delete/Merge/Create/Drop/Command/Copy/Pragma/Set/
   Attach/Detach/Install/Load/Call/Into/Describe — deny by default:
   anything not a Select is refused).
2. Every table referenced (FROM/JOIN/subquery/CTE sources) must be in the
   loaded-table allowlist; unknown names are refused naming the table.
3. Table functions and filesystem/environment functions are refused
   (`read_csv*`, `read_parquet`, `read_json*`, `getenv`, `glob`, …:
   an explicit denylist plus refusal of any function that sqlglot parses
   as a table source).
4. `LIMIT` is injected/lowered to cap+1 at the AST, never by string edit.

Validation failures and DuckDB errors both come back as the structured
error payload. The server emits one content-free log line per call
(duration, row/column counts, truncated flag, error kind — never SQL
text), matching the library's deployed-log discipline; the SQL itself is
visible in Phoenix (D5) and the local model trace.

Tool-level governance is unchanged library machinery: policy rules allow
`tool.call` on `rmqna-data/*` for analyst/manager, samples prove it, pins
cover all three tools. The `data.query` policy action and per-query
obligations do not apply to model-authored SQL; that narrowing is
documented in the ADR (D7) with the compensating controls above.

### D4 — the agent (R3, R5, R9)

The generated LangGraph agent, customized rmprep-style:

- **System prompt** (versioned resource): role + answer policy, the
  schema document (D2), a DuckDB dialect cheat-sheet (date functions,
  `date_trunc`, integer division, ILIKE), metric definitions stated once
  each (exposure, utilization, as-of semantics — drawn from the
  dictionary's domain prose), the clarify-and-refuse policy, and 10–12
  curated question→SQL few-shot pairs (static; R6). Static prefix keeps
  prompt caching effective (rmprep convention).
- **Flow**: clarify gate (one multiple-choice question when the metric,
  time window, or entity is ambiguous — prompt-level policy, no extra
  graph machinery) → resolve names via `search_column_values`, filtering
  on returned canonical values, never guessed LIKE patterns → generate
  SQL → `execute_sql` → on error payload, repair with at most 2 retries,
  each a self-contained message carrying the failed SQL and the verbatim
  error → empty result: one re-examination, then answer with the caveat
  naming the filters applied.
- **Answers** state the tables used, filters, and as-of basis, and offer
  the SQL ("ask to see the SQL"); out-of-scope questions (not answerable
  from the 33 tables) are declined by the prompt policy.
- Standard template plumbing otherwise: `run_agent` service on port 8200,
  one-shot CLI, outcomes journal local-only, fake-provider tests with a
  scripted model driving the exact tool-call sequence.

### D5 — Phoenix captures the SQL (R10)

No new tracing code. The model emits SQL as the `execute_sql` tool-call
argument, so with `AC2_PHOENIX_ENABLED=true` and
`AC2_PHOENIX_CAPTURE_CONTENT=true` the agent-side tool span (named
`rmqna-data.execute_sql`, tagged with the server id) carries the SQL in
`input.value`, and the result payload in `output.value`; the model spans
carry it in the tool-call arguments. The rmqna README documents this as
*the* SQL debugging workflow (filter traces by the tool span), notes that
content capture is all-or-nothing per process (synthetic data makes that
acceptable locally), and that `AC2_MODEL_TRACE_PATH` records the same
calls to JSONL independently. A generated-style tracing test asserts the
span layout, like rmprep's.

### D6 — evals (R8)

`agents/rmqna/evals/cases.jsonl`: ~30 synthetic hand-written cases
(committable), each `{id, input: {question}, expected: {result: …,
reference_sql: …, answer_contains: …}, tags}` where `expected.result` is
a normalized result representation (sorted rows of selected columns, or
a scalar). The eval task runs the **real model** against the full agent
graph with the rmqna-data tools connected **in-process** via the
library's `InProcessMcpConnector` (the rmprep cross-service test
pattern) — hermetic on the tool side, live on the model side, no server
to start — and returns `{answer, executed_rows}`, the executed rows
extracted from the graph's tool messages. Scorers: a workspace-local
`scored_by("result_match", …)` that runs the case's `reference_sql`
through the same in-process engine and compares normalized results
against the agent's executed rows — execution-result match, not SQL
string match — plus substring checks on the answer. The library eval
flow applies otherwise: `pytest -m eval` (skips on the fake provider),
report → baseline via `agentlib eval rmqna --accept` (the initial
committed `baseline.json` comes from one maintainer-run live accept),
regression tolerance per the library default. The few-shot pairs (D4)
and the eval cases are disjoint sets. Hermetic CI coverage of the same
plumbing comes from the ordinary scripted-model tests (goal 5), not from
the eval suite.

### D7 — the governance ADR (R4/R10)

`examples/rmqna/docs/sql-governance.md`: records that rmqna deliberately
departs from the library's named-query stance for ad-hoc analytics; the
library contract is unchanged; enumerates the compensating controls
(AST allowlist validation, read-only single-SELECT, table allowlist,
function denylist, row cap, timeout, external access disabled,
tool-level policy/guardrails/audit, pins, content-free server logs) and
the narrowings (no per-query classification/obligations, no per-role
masks on ad-hoc results — synthetic local data); and names the upgrade
path if this ever graduates into the library (a new governed port with a
policy story for statement-level decisions).

### D8 — verification

Workspace: `agentlib check` green (ruff/format/mypy/pytest/policy
samples); validator rejection suite (DDL/DML, multi-statement, comment
smuggling, unknown tables, table functions, PRAGMA/ATTACH/COPY, the
hostile `' OR '1'='1` value as data); engine discipline tests (timeout,
truncation, external access off); schema-doc regeneration test; pins
verified; eval suite on the scripted model. Repo: full pytest, mypy,
ruff, lint-imports, architecture tests all green. Live: start rmqna-data
+ rmqna with the real model and Phoenix, ask 2–3 RM questions, verify
answers, the repair loop on an induced error, and the SQL visible on the
Phoenix tool span (capture on).

## Left to the builder

- Exact M-Schema-ish formatting of the schema document and the
  low-cardinality threshold; exact few-shot pairs and metric definitions
  (reviewed against the dictionary prose).
- The engine's internal module layout behind the server protocol, and
  whether to reuse `adapters/sql` helpers by import or keep the example
  self-contained (either is allowed for example code; prefer whichever
  reads cleaner under strict mypy).
- Repair-prompt wording; clarify-gate prompt wording.
- Eval case authorship (30 cases spanning joins, aggregation, dates,
  empty-result, ambiguity, refusal) and the normalized-result format.
- Whether `get_schema` returns markdown whole or sectioned (token budget
  call).

## Open questions for the maintainer

1. **Local launcher**: should rmqna join the `start-local` supervised
   stack (touches `tools/local_dev.py` + its architecture tests), or run
   via `agentlib run` / its own README commands in v1? Design assumes
   the latter.
2. **Workspace location**: `examples/rmqna` as designed, or inside
   `examples/rmprep` as a second agent sharing the workspace? Design
   assumes a separate workspace because the data folder is copied.

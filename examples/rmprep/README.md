# rmprep: relationship manager meeting preparation

This example prepares a client meeting memo and answers follow-up questions from
the CSV files in `data`. The supplied snapshot has 33 tables and 46 clients,
covering relationships, credit, deposits, payments, service issues and opportunities.
It contains real company names; the figures are supplied examples, not independently
verified facts about those companies. An external news source has not been supplied.

One agent service contains three business nodes:

```text
Question -> router -> memoagent -> meeting memo
                  -> qnaagent  -> focused answer
                         |
                   model -> MCP -> model
```

Both specialists share the conversation saved for the caller's thread.
LangGraph manages routing and messages. The library injects model access, MCP,
identity, policy, audit and checkpoint providers. The MCP service retrieves
evidence and calculates review findings; the model turns that evidence into prose.

For a local trace timeline with Python and pip, follow the
[Phoenix walkthrough](../../docs/phoenix-trial.md). The Phoenix adapter is shared
library code, also included in newly generated agents. Phoenix runs as a separate
service and adds a trace link to each Agent Lab response, with model attempts,
MCP calls and checkpoint references grouped by thread.

## 1. Install with Python and pip

For browser testing, follow the [local startup and testing guide](../../docs/local-startup.md).
It covers `setup-local.bat`, Anthropic/Phoenix settings in the root `.env`, login,
a Jones memo, same-thread follow-ups and client switching. For each later session,
run `start-local.bat` from the library root in **Command Prompt**, then
`stop-local.bat` when finished. Saved state is preserved.

In Agent Lab, select **Choose agent → Start chat with rmprep**. **Memos** opens
saved versions beside chat. **Open trace in Agent Lab** opens the response's
Phoenix trace when enabled; **Logs** provides local diagnostics and checkpoint
inspection. To ask about an older memo, name its reference in your question;
selecting it in the panel changes only the viewer.

The remaining steps cover manual setup and command-line walkthroughs. Manual
agent commands read `agents/rmprep/.env`; they do not automatically load the
root `.env` used by the launcher.

Open **Command Prompt** in the library repository. Use Python 3.11 or newer and
your team's existing pip configuration, including its enterprise package index.

```bat
python -m pip install -e packages\ai-agent-lib-core -e packages\ai-agent-lib-aws -e packages\ai-agent-lib-cli
python -m ai_agent_lib_cli install --workspace examples\rmprep
cd examples\rmprep
```

Use the same approved Python for installation and service startup. Run
the `install` command. That command installs both example services and the
development tools using the active Python interpreter and pip.

Create local settings once:

```bat
if not exist agents\rmprep\.env copy agents\rmprep\.env.example agents\rmprep\.env
if not exist mcp-servers\rmprep-data\.env copy mcp-servers\rmprep-data\.env.example mcp-servers\rmprep-data\.env
```

Keep existing `.env` files if you have configured them already. Check that the
MCP service's `AC2_DATA_SOURCES` setting points to the supplied folder:

```dotenv
AC2_DATA_SOURCES={"rmprep":{"kind":"duckdb_csv","data_dir":"../../data","queries_dir":"queries"}}
```

Paths are relative to the MCP service's `.env`. The CSV files stay in
`examples\rmprep\data`. Tests read `.env.example`, so local credentials do not
affect them. Run the remaining commands from `examples\rmprep`.

## 2. Run the local walkthrough

```bat
python -m rmprep.examples.trace_react
```

This repeatable example uses scripted model decisions and answers. It runs the
actual graph, MCP tools, CSV queries, policy, audit and SQLite checkpoints in
one process. The printed memo is a short scripted excerpt; it demonstrates
evidence flow, not the quality of a model-generated memo.

1. `memoagent` requests the evidence pack for Jones Dairy Farm, `JONE-0022`,
   with meeting date `2026-08-03`.
2. The MCP runs 21 governed queries and returns sourced records, calculated
   findings and evidence gaps.
3. `qnaagent` receives the saved conversation and retrieves the covenant
   details for the same client and date.
4. Both turns remain on `jone-0022-meeting`.

Expected summary:

```text
Thread: jone-0022-meeting | Route: memoagent
Checkpoint: 4 messages
Thread: jone-0022-meeting | Route: qnaagent
Checkpoint: 8 messages
```

The command prints paths to `model-trace.jsonl` and `checkpoint-history.json`
in a new folder under `agents\rmprep\.agentlib\react-demo`. The trace contains
four model request/response pairs with input message counts `2, 4, 6, 8`,
including each specialist's system instructions.

On the second model request, the tool message contains the memo evidence.
On the third request, the saved first question, tool call, evidence and memo
appear before the follow-up question. The fourth request adds the covenant
tool result. The test suite verifies these contents and reloads the saved
conversation after restarting the container.

Inspect a trace using its printed path:

```bat
python examples\read_trace.py "TRACE_FILE"
```

## 3. Generate a memo with your configured model

Start the MCP service in the first Command Prompt:

```bat
python -m rmprep_data
```

It listens at `http://127.0.0.1:8100/mcp`. Keep the window open.

For model-written answers, configure a tool-capable model in
`agents\rmprep\.env`. The default `fake` provider echoes input.
For Anthropic, add `anthropic` to the existing `ai-agent-lib-core[...]` extras
in `agents\rmprep\pyproject.toml`, then run:

```bat
python -m ai_agent_lib_cli install
notepad agents\rmprep\.env
```

Set `AC2_MODEL_PROVIDER=anthropic`, your available `AC2_MODEL_ID`, and
`ANTHROPIC_API_KEY`. Enable prompt caching in the same file:

```dotenv
AC2_MODEL_OPTIONS={"anthropic":{"prompt_caching":true,"cache_ttl":"5m"}}
```

Restart the agent after changing this setting. Keep the registry, policy and identity settings. The
[model setup guide](../../docs/anthropic-example.md) explains these settings;
use the `rmprep` commands below for this business example. A model run sends
the selected evidence and conversation to the configured provider.

In a second Command Prompt, starting at the repository root:

```bat
cd examples\rmprep
python -m rmprep "memo: Prepare a meeting memo for Jones Dairy Farm as of 2026-08-03." --thread jones-aug03
python -m rmprep "qna: Which covenants need discussion, and what do their source records show?" --thread jones-aug03
python -m rmprep "qna: What reporting and client commitments are overdue as of the meeting date?" --thread jones-aug03
```

The memo covers priority actions, relationship and financial position, credit,
treasury and service, opportunities, follow-up actions, and evidence gaps.
Use the same thread ID for follow-ups and a new thread for another meeting.
Refreshing a memo asks the model to retrieve the evidence again; the checkpoint
also retains the earlier conversation.

The agent copies full source references beside its claims. MCP results include
calculated changes and explicit evidence limits: for example, a recorded waiver
expiry date does not establish that a waiver was executed. Payment summaries
show the requested window and the last observed transaction date.

**Answer length and checks.** To set an answer length, include `Keep it within
450 words.` in your question. The defaults are 1,000 words for a memo and 350
for Q&A. Counts include heading text, figures, tables and full citations.
Markdown heading/list markers and divider-only lines do not count; code is
counted literally. The answer stays unchanged when it passes these checks.
Before saving an answer, the agent checks its length and checks that each
citation matches a source reference returned by MCP. It also rejects citation
placeholders and wording that assigns an unsupported fiscal year to an
audited-statement requirement. Common financial comparisons must include their
own two source references when citing a financial page. These are conservative
checks for known failure patterns. The model can revise a failing draft at most
twice, then the agent reports a validation error if the checks still fail.
These checks verify length and reference names; business reviewers still need
to judge whether each claim follows from its evidence.

For an HTTP endpoint, run `python -m rmprep.service` and send `POST /invoke`:

```json
{"input":{"question":"memo: Prepare a memo for SHIN-0039 as of 2026-08-03."},"thread_id":"shin-aug03"}
```

`POST /invoke/stream` reports router and specialist updates. Stop a service
with Ctrl+C.

To capture model inputs and outputs, add this to the agent's `.env`:

```dotenv
AC2_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl
```

Traces contain client evidence and conversation content. Keep them in the ignored
`.agentlib` folder and follow your team's data-handling rules when sharing them.
If an answer needs revision, the trace includes the draft and correction request.
The conversation checkpoint stores the accepted answer, so follow-ups do not
inherit rejected drafts.

Evidence-backed memos now include a stable reference and a version for their
client. Follow-ups select that document before the model runs. Use requests such
as `qna: Explain version 1 for JONE-0022.` or `qna: Compare versions 1 and 2 for
JONE-0022.` on the same thread. Read [memo context management](docs/context-management.md)
for client switching, historical evidence, long conversations and trace inspection.

You can also ask `prepare rm prep for Canada Goose`, then ask `How did you
calculate wallet share for Canada Goose?` in the same thread. A successful client
lookup connects the search name to the client's UEN. The follow-up receives the
saved memo and the relationship evidence that supports its wallet-share figure.

To compare clients, ask `Do we have a client containing canada?`, then
`Compare these two clients' financials.` in the same thread. The supplied search
returns `CANA-0010` and `SLEE-0041`. Both remain selected while the agent retrieves
their financials. The trace should contain the original search result and a
financial result for each client. See the [comparison walkthrough](docs/context-management.md#compare-clients-returned-by-a-search).

For long conversations, the agent now allows 96,000 estimated tokens of recent
history within a 128,000-token total input budget. This keeps larger financial
exchanges available for follow-ups. Configure `RMPREP_CONTEXT_*` in the agent's
`.env` and restart; see [context budgets](docs/context-management.md#keep-long-conversations-usable).
Older history remains in checkpoints even when it no longer fits in model input.

## 4. Explore the evidence

| MCP tool | What it does |
| --- | --- |
| `find_clients(search)` | Find clients by UEN, exact name or name fragment. Choose one for a memo, or select the returned group for a comparison. |
| `get_memo_evidence(uen, meeting_date)` | Collect a bounded evidence pack, sourced review findings and gaps. |
| `get_client_records(uen, section, start_date, end_date, offset)` | Retrieve client details, date ranges and further pages for Q&A. |
| `get_reference_data(section, search, end_date, offset)` | Retrieve product catalog, market snapshots, dictionary or vocabulary entries. |

The registry prefixes each name with `rmprep-data.`. The model-facing names
use underscores where required by model APIs.

Try these requests with a configured model:

| Question | Relevant evidence |
| --- | --- |
| Prepare a memo for Jones Dairy Farm as of August 3, 2026. | `JONE-0022`: covenant failures, reporting, working capital, controls and readiness. |
| How many days until SHIN-0039's facility matures? | `facilities`: 25 days for FAC000000020 at that meeting date. |
| What is WAWO-0046's excess availability as a percentage of commitment? | `borrowing_base`: 4.80%, calculated in code from matching USD amounts. |
| What needs attention before discussing more products with ALLO-0001? | Service cases, controls, commitments and the recorded NPS category. |
| Show CANA-0010's payments from January through June 2026 by currency. | `payment_totals`: grouped raw transactions, preserving CAD and other currencies. |
| Explain the latest working-capital trend for FARI-0016. | Dated working-capital history; avoid inventing an urgent problem. |

Read the [data review](docs/data-review.md) for table coverage, date semantics,
currency caveats and the example review thresholds. Read the
[MCP reference](mcp-servers/rmprep-data/README.md) for section names and pagination.

## 5. Understand and change the code

| File | Responsibility |
| --- | --- |
| `agents/rmprep/src/rmprep/graph.py` | Compose the three business nodes and inject library services. |
| `service.py` in that package | The HTTP entry point: `SERVICE`, an `AgentService`, runs on the shared `run_agent`. The context budget loads fail-fast in `main`, so a malformed value stops the start. |
| `router.py`, `prompts/`, `specialist.py` in that package | Select the workflow, load versioned instructions, and run the model/tool loop. |
| `response_review.py` in that package | Check answer length and exact source references before saving a reply. |
| `memos.py`, `selection.py` in that package | Preserve immutable memo versions and resolve document references within a scoped thread. |
| `clients.py` in that package | Remember client names from successful MCP lookups so follow-ups can use the same name. |
| `context.py`, `context_tools.py` in that package | Assemble bounded model input and check tool arguments against the selected client and date. |
| `mcp-servers/rmprep-data/src/rmprep_data/server.py` | Declare four MCP interfaces and inject governed query access. |
| `evidence.py` in that package | Compose pages and memo packs with provenance and gaps. |
| `signals.py` in that package | Calculate deterministic, sourced review findings. |
| `measurements.py` in that package | Calculate comparable changes, overdue days and observed payment coverage; describe what the records cannot establish. |
| `sections.py` and the service's `queries` folder | Allowlist evidence sections and define parameterized, read-only SQL. |
| `data` | Supplied CSV snapshot, read in place. |
| `registry` and `policies` | Declare tools, pin schemas and authorize access. |

The router recognizes `memo:`, `prepare rm prep`, and memo creation requests, including polite
requests after an introductory clause. Other requests use Q&A. Use `memo:` or
`qna:` when you want to make the workflow explicit.

The local policy allows analysts and managers to read this example's clients;
it does not yet restrict an RM to an assigned portfolio. Add those entitlements
before using a production data source. `rm_scenarios.csv` supplies expected
outcomes to tests only and is excluded from the tools.

The CLI created the starting workspace, MCP and agent. The business implementation
is maintained in these source files. `agentlib.toml` records generation inputs
and the query inventory; generating from it creates scaffolding, not a copy of
the custom composite tools and graph. Copy this example to retain those changes.

## 6. Run the checks

```bat
python -m ai_agent_lib_cli check
```

This runs linting, formatting, type checks, tests and policy samples. Tests cover
all evidence sections, the 10 supplied scenarios, source references, dates,
currency totals, pagination, denied access, routing, streaming and checkpoint
isolation/restart. Regression tests also cover evidence limits, financial and
working-capital arithmetic, payment coverage, exact citations and bounded answer
revision. They use scripted models and local data. Evaluate memo quality
with your configured model and business reviewers as a separate step.
Context tests also cover multiple versions, client switching, historical
references, comparisons, long histories, tool argument drift and older checkpoints.


## Local Phoenix tracing

The [Phoenix trial](../../docs/phoenix-trial.md) uses the same Python, a separate package folder, and SQLite.
Run `start-phoenix.bat setup` once from the library repository root, then follow
the guide to enable Phoenix in the root `.env`. `start-local.bat` starts the local
Phoenix server before rmprep and Agent Lab, using the package folder `.agentlib\phoenix\.venv`.
`stop-local.bat` in another Command Prompt stops the stack and preserves
conversations, traces, and checkpoints. **Ctrl+C** in the original launcher also works.
Each response links to its Phoenix trace; the existing turn inspector remains available.

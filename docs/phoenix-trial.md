# Local tracing with Phoenix

Phoenix gives Agent Lab a timeline of each agent response: model messages, MCP
calls, token usage, duration, and links back to checkpoint IDs. This trial runs
Phoenix on your Windows desktop using Python, pip, and a local SQLite file.
Use Python 3.11 or newer and the pip index and certificates supplied by your team.
Your package administrators must make the pinned package and its Windows wheels
available through the enterprise package repository.

For the complete first-run path, use [local startup and testing](local-startup.md).
This page explains Phoenix settings, trace inspection and captured verification results.

## 1. Install the Phoenix packages

Open Command Prompt in the `ai-agent-lib` repository and run:

```bat
start-phoenix.bat setup
```

The script installs `arize-phoenix==20.22.0` with pip into
`.agentlib\phoenix\.venv\Lib\site-packages`, using your existing Python. Agent Lab
and the agent keep their own `.venv` packages. All three run with the default
Python executable, without using environment-local Python or pip binaries.
You only need to repeat setup after changing the pinned Phoenix version or
changing the selected Python version or rebuilding its packages. The script preserves your pip configuration.

## 2. Enable Phoenix in the root .env

Add these settings to **`.env` beside `start-local.bat`**. Keep your existing model
provider and API key in that file.

```dotenv
EAP_TELEMETRY=off
EAP_LANGFUSE_ENABLED=false
EAP_PHOENIX_ENABLED=true
EAP_PHOENIX_CAPTURE_CONTENT=true
PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006
PHOENIX_PROJECT_NAME=rmprep-trial
```

For this local setup, `EAP_TELEMETRY=off` disables the separate operational OTLP
trace/metric exporter. Phoenix still receives agent/model/tool spans and token
metrics. Enable operational telemetry only when you have configured its own
collector; Phoenix is not the general OTLP metrics endpoint. If you use legacy
`EAP_AUDIT_OPTIONS`, remove `"tracing": true` or set it to `false` as well.

Content capture records model messages and tool arguments/results, so use
approved test data. Its default is `false`. The agent's existing JSONL traces,
audit records, and checkpoint database continue to work independently of Phoenix.
Enable one agent tracing provider at a time; enabling both Phoenix and
Langfuse produces a configuration error before the agent serves requests.

## 3. Start the stack

From Command Prompt in the repository root:

```bat
start-local.bat
```

The launcher reads the root `.env`, starts Phoenix, and waits for its health check
before starting the MCP server, rmprep, and Agent Lab. Phoenix uses
the same Python as the agent and UI, with its own package folder.
Startup checks that the packages match the selected Python; it does not install packages. If it is
missing, run `start-phoenix.bat setup`, then try again.

Open Agent Lab at <http://127.0.0.1:8765> and Phoenix at
<http://127.0.0.1:6006>. This local, single-user Phoenix trial needs no separate
login. Check the printed model and settings before testing. Read the Agent Lab
access code with `type .agentlib\ui-rmprep\access-code` in another Command Prompt.

Keep the launcher window open. Run `stop-local.bat` in a second Command Prompt
to stop the UI, agent, MCP server, and Phoenix, in that order. It also stops a
standalone `start-phoenix.bat` run from this checkout. **Ctrl+C** in the original
window also works. SQLite stores traces in
`.agentlib\phoenix\data\phoenix.db`; restarting keeps them. The `.agentlib` folder
is excluded from Git. Phoenix's startup log appears alongside the other service
logs in `.agentlib\local-runs\<run-id>\phoenix.log`.
Service log files use UTF-8, including first-run database messages on Windows.

`start-local.bat services` also includes Phoenix when enabled. UI-only and scripted
demo runs keep their existing scope. Command Prompt settings override the root
file; `--env-file` selects a different file instead of the root `.env`.
The launcher uses the same effective Phoenix settings as the agent, including an
optional separate tracing settings file.

Automatic startup applies to `http://127.0.0.1:6006` and
`http://localhost:6006`. Other endpoints are managed separately: the launcher
prints that choice and connects the agent without starting a local Phoenix server.
For a readiness check that stops everything afterward, run:

```bat
start-local.bat --smoke-test
```

The launcher configures Phoenix as a local trace viewer. Your agent calls the
model and MCP tools; Phoenix displays their inputs, outputs, metrics, and
checkpoint references. It binds HTTP to loopback and receives spans at
`http://127.0.0.1:6006/v1/traces`.

These server settings are applied automatically; keep your agent's provider and
API key in the root `.env`:

| Phoenix server feature | Local setting | Reason |
| --- | --- | --- |
| Playground model generation | `PHOENIX_ALLOWED_PROVIDERS=NONE` | Run model requests through Agent Lab; traces from every model provider remain visible. |
| Phoenix's own MCP endpoint | `PHOENIX_ENABLE_MCP_SERVER=false` | Agents use their registered MCP servers. The trace viewer does not need another MCP endpoint. |
| Code execution | `PHOENIX_ALLOWED_SANDBOX_PROVIDERS=NONE`, `PHOENIX_ENABLE_MCP_CODE_MODE=false` | Viewing traces requires no sandbox binaries. |
| Built-in assistant | `PHOENIX_DISABLE_AGENT_ASSISTANT=true` | Test your agent through Agent Lab. |

The launcher also disables usage telemetry and the optional gRPC collector.
The native Phoenix banner may still list gRPC; this trial uses HTTP only.
`tools/phoenix_server.py` contains version-checked compatibility shims for the
gRPC listener and the optional Bedrock Playground dependency check. The latter
only checks for `aioboto3` when Phoenix allows its AWS provider. Neither shim
changes installed packages, database queries, or warning filters. Review them
when upgrading Phoenix.

## 4. Verify a conversation

Choose **rmprep** and start a new chat. These questions exercise a real MCP call
without requesting client financial or contact records:

1. `Find clients containing the exact synthetic test name PHOENIX-TRIAL-NO-MATCH-20261015. Use find_clients once and report the number of matches. Do not broaden the search or retrieve other records.`
2. `Which exact synthetic name did you just search for, and how many matches did it return? Answer from this conversation without running another search.`

Accepted answers and confirmed agent failures include **Open trace in Agent Lab**
below usage and response time when execution created a trace. The link opens the
configured Phoenix trace. Export is asynchronous; allow a few seconds for ingestion.
Opening a trace does not rerun the agent.

In Phoenix, inspect:

| Item | What to look for |
| --- | --- |
| Root `rmprep.turn` span | One trace per invocation; the two traces share `session.id` and `agentlib.thread_id`. |
| `model` spans | Select a model span, then view **Input** and **Output** on its **Info** tab. Switch between **Messages** and **Raw**. The next model call includes the preceding tool result. |
| `rmprep-data.find_clients` span | MCP arguments, result, duration, and `agentlib.mcp_server`. The synthetic search should return zero matches. |
| Usage | Prompt/completion tokens and cache read/write counts. Prompt totals already include cache tokens. |
| Checkpoints | `agentlib.checkpoints` on the root span contains checkpoint IDs and parent IDs for that request. Use Agent Lab's **Logs** page to inspect saved state; chat links directly to Phoenix. Failed runs include available checkpoints and an error status; their rejected drafts remain in model traces, not accepted chat history. |
| Follow-up | Its model input includes the earlier exchange, and its answer reports the same search and count. |

Model spans measure provider-call duration; the root measures agent work including
tools. Agent Lab measures the full response time, including HTTP/UI overhead, so
the durations need not be identical. Checkpoint contents remain in the agent's
database; Phoenix receives references, not a second copy of the checkpoint store.

### Verified Jones follow-up after restart

On October 16, 2026, Agent Lab reopened an existing conversation containing Canada
Goose and Jones Dairy Farm memos. A follow-up named the saved Jones memo and asked
about covenant breaches, the cash conversion cycle change and wallet-share calculation.

| Model calls | New MCP calls | Revisions | Input / output tokens | UI time | Checkpoint references |
| --- | --- | --- | --- | --- | --- |
| 1 | 0 | 0 | 12,391 / 657 | 6.69 s | 4 |

The answer reused the selected memo's saved evidence. It reported DSCR 1.08,
fixed charge coverage 0.96, cash conversion cycle 87 → 106 days, and recorded
wallet share 40.21%, preserving the unknown waiver execution and wallet-share
methodology. Phoenix input/output and usage matched Agent Lab and the model JSONL.
Source checks covered eight CSV records, 91 fields and seven calculated comparisons.
Both memo versions remained saved; `stop-local.bat` stopped all four services
without changing stored conversation, checkpoint or trace rows during shutdown.

The earlier follow-up needed two calls because Markdown markers pushed the raw
word count over its limit. Content-word counting now excludes those markers;
regression tests preserve that original draft unchanged and still reject invalid
citations and answers over their content-word limit. Live wording, timing and call counts vary.
Follow the [local test questions](local-startup.md#generate-a-memo-and-ask-follow-ups)
to run this scenario with your own saved memo reference.

<details>
<summary>Earlier captured runs: October 15, 2026</summary>

### Verified Anthropic run

On October 15, 2026, these two questions ran through Agent Lab using the root
`.env` and `claude-sonnet-4-5`. The synthetic name returned zero matches. The
follow-up recalled that name and count without another MCP call.

| Turn | Model calls | MCP calls | Input / output tokens | Cache read / write tokens | UI response time | Checkpoint references |
| --- | --- | --- | --- | --- | --- | --- |
| Find the synthetic name | 2 | 1 | 8,135 / 115 | 3,970 / 4,157 | 5.02 s | 4 |
| Recall the result on the same thread | 1 | 0 | 4,235 / 33 | 4,157 / 75 | 1.89 s | 4 |

The first trace showed `rmprep-data.find_clients` returning an empty `matches`
list. The next model input included that tool result with the same tool-call ID.
The follow-up's input included the original question, tool result, saved answer,
and new question. The traces shared one session and thread, with separate request IDs.

Phoenix's API and SQLite records matched Agent Lab's saved metrics, the agent's
model JSONL records, the MCP server audit, and all eight checkpoint references.
Both traces remained available after stopping and restarting Phoenix with the
same SQLite file.
These counts are an example of one run; model output, caching, and duration vary.

### Verified JONE-0022 memo and follow-ups

On October 15, 2026 (America/New_York), the original broad memo request for
JONE-0022 as of 2026-08-03 completed through Agent Lab using Anthropic and Phoenix.
The memo had 867 words, including references, and required no revision. Two
follow-ups used the same thread:

1. Explain the $7M net working-capital decline and 19-day cash-conversion-cycle increase from saved evidence.
2. Ask whether the sweep was deployed in October 2025, retrieving opportunity and service-request records through MCP.

| Turn | Model calls | MCP calls | Input / output tokens | Cache hit | UI time | Checkpoint references |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2 | 1 | 29,550 / 2,921 | 27.3% | 68.58 s | 4 |
| 2 | 1 | 0 | 28,572 / 532 | 0.0% | 11.09 s | 4 |
| 3 | 2 | 2 | 59,727 / 598 | 96.7% | 13.17 s | 4 |

The calculation answer reused saved evidence. The October question retained the
August memo and retrieved records through its 2026-08-03 cutoff. Both the memo
and follow-up described Closed Won as a sales stage and left implementation
unverified. Each answer linked to Phoenix; model inputs/outputs, MCP audit events,
usage totals and all 12 checkpoint references matched the local records. Source
checks covered 59 CSV records, 579 fields, nine payment groups and 14 comparisons.

The memo and Q&A specialists have different static prompt prefixes. The first
Q&A call wrote its cache; the next follow-up reused it. Cache hits and timing vary
between runs. Regression tests also cover trace links and checkpoint references
for confirmed failed turns; the live conversation above had no failed turns.

</details>

## Settings

From your agent workspace, run `python -m ai_agent_lib_cli config explain rmprep`
to see effective Phoenix settings and their source files, with API keys masked.
Run `python -m ai_agent_lib_cli doctor rmprep` to validate them before startup.
These tracing checks do not contact Phoenix. They also reject enabling Phoenix
and Langfuse together. The batch launcher's summary includes the same resolved
settings, including a separate `EAP_PHOENIX_ENV_FILE` when configured.

| Variable | Purpose | Default |
| --- | --- | --- |
| `EAP_PHOENIX_ENABLED` | Enable tracing; `start-local.bat` also starts the local server | `false` |
| `EAP_PHOENIX_CAPTURE_CONTENT` | Include model and tool input/output | `false` |
| `EAP_PHOENIX_ENV_FILE` | Optional separate agent tracing settings file | Unset |
| `PHOENIX_COLLECTOR_ENDPOINT` | HTTP loopback or HTTPS origin, without `/v1/traces` | `http://127.0.0.1:6006` |
| `PHOENIX_PROJECT_NAME` | Project for this agent's spans | `agent-lab` |
| `PHOENIX_API_KEY` | Optional key for an authenticated Phoenix server | Empty for this local trial; masked in startup output |

Agent settings resolve in this order: process environment, separate tracing file,
then service `.env`. A separate file path in service `.env` is relative to that
file. Use an absolute path when setting it in the root `.env` or Command Prompt.
Setting `EAP_PHOENIX_ENABLED=false` in Command Prompt also bypasses a separate
tracing file. The Phoenix server uses its own fixed local settings and does not
load agent credentials.

## Reuse in another agent

Use `agent_tracing` from
`ai_agent_lib_core.integrations.langgraph.tracing` at the service entry point.
It accepts the same `application`, `config`, and `dotenv_path` arguments as the
existing `langfuse_tracing` context manager. Pass its yielded adapter to
`ServiceContainer(model_trace=tracing)` and call
`tracing.run(graph, services, context, given, respond=your_response_function)`
when enabled. The rmprep service demonstrates this composition.

The agent needs the library's existing `otel` extra, installed using pip. It does
not import the Phoenix server package. Existing `LangfuseTracing` and
`langfuse_tracing` imports and invocation APIs remain supported.

## Troubleshooting

### Remaining upstream warning

Phoenix 20.22.0 can print SQLAlchemy's `Column-expression-level unary distinct()`
warning during startup or browsing. Its database queries use column-level
`distinct()` where SQLAlchemy 2.1 expects
[`select(...).distinct()`](https://docs.sqlalchemy.org/en/21/core/sqlelement.html#sqlalchemy.sql.expression.distinct).
We leave this warning visible and keep Phoenix's database code unchanged. It
does not block the verified SQLite trace ingestion and browsing workflow. Review
an upstream query fix with the next Phoenix upgrade; installing `aioboto3` will
not fix it.

Disabling Phoenix's unused MCP endpoint avoids the startup
`PydanticJsonSchemaWarning` caused by its unset-value defaults during schema
generation. Requesting Phoenix's OpenAPI schema separately can still expose
that upstream warning. It is not filtered. The startup suggestion to install
`aioboto3` no longer applies because Playground generation is disabled; Bedrock
agents keep their own model dependencies and can still send traces to Phoenix.

### Startup and trace checks

| Symptom | Check |
| --- | --- |
| Package installation fails | Ask your package administrators for `arize-phoenix==20.22.0` and compatible wheels for your Python version. |
| Phoenix environment is missing | Run `start-phoenix.bat setup` once, then retry `start-local.bat`. |
| Port 6006 is in use | Stop a standalone Phoenix window before starting the combined stack. The launcher fails before starting services and leaves the existing process alone. |
| Repeated connection errors on port 4318 | Set `EAP_TELEMETRY=off` for this local trial, or configure your separate operational collector. Restart the stack. |
| No trace link | Check `EAP_PHOENIX_ENABLED` in the agent startup output and restart after changing settings. |
| Link opens before a trace appears | Allow a few seconds, then reload. Check the agent log for exporter connection errors. |
| Inputs and outputs are missing | Set `EAP_PHOENIX_CAPTURE_CONTENT=true` for approved test data, restart the agent, and send a new question. |
| The response succeeds while Phoenix is stopped | Expected: diagnostic export failures do not resend or fail an accepted agent response. Local inspection remains available. |

### Browse saved traces without starting the agent

Run `start-phoenix.bat` by itself to browse the SQLite traces. Press **Ctrl+C** in
that window before starting the combined stack. A supervised Phoenix process is
part of the stack: if it exits unexpectedly, the launcher stops its remaining
services and points you to the log. An externally managed endpoint has its own
lifecycle.

References: [Phoenix CLI](https://arize.com/docs/phoenix/self-hosting/deployment-options),
[SQLite storage](https://arize.com/docs/phoenix/self-hosting/deployment),
[OpenInference attributes](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md).

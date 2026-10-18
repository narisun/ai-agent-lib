# Trace agents with Phoenix

Phoenix gives Agent Lab a timeline of each agent response: model messages, MCP
calls, token usage, duration, and links back to checkpoint IDs. It is the only
agent-tracing backend the library ships, and it stays off until you enable it.

You choose where the Phoenix server lives:

- **Local, the default.** A Phoenix server on your own machine, started by this
  repository's launchers, with traces in one SQLite file. It needs no account,
  and nothing leaves the machine. Steps 1 to 4 below set it up.
- **Team-hosted.** Your agent exports to a Phoenix that your platform team
  runs, over HTTPS. See [Send traces to a team-hosted
  Phoenix](#send-traces-to-a-team-hosted-phoenix).

For the complete first-run path, use [local startup and testing](local-startup.md).
This page explains Phoenix settings, trace inspection, privacy, and captured
verification results.

Commands are shown for Command Prompt. On macOS/Linux, open a bash or zsh terminal
at the repository root and use the right-hand spelling. Paths use Windows
separators; use `/` on macOS/Linux. [Local startup](local-startup.md) explains the
convention and its Python requirement.

| Command Prompt | macOS/Linux |
| --- | --- |
| `python …` | `python3 …` |
| `start-phoenix.bat setup` | `./start-phoenix.sh setup` |
| `start-phoenix.bat` | `./start-phoenix.sh` |
| `start-local.bat` | `./start-local.sh` |
| `stop-local.bat` | `./stop-local.sh` |
| `type .agentlib\ui-rmprep\access-code` | `cat .agentlib/ui-rmprep/access-code` |

## Generated services: one setting

A service made with `agentlib new agent`, and any service that starts through
`run_agent`, has no tracing code to write. `run_agent` resolves the Phoenix
settings, starts the exporter, hands the same adapter to the service container
and to the HTTP app, and closes it after the container so spans flush last. Turn
tracing on in the service's `.env`:

```dotenv
EAP_PHOENIX_ENABLED=true
```

That is the whole integration. With nothing else set, the agent exports to
`http://127.0.0.1:6006` under the project name `agent-lab`, so a Phoenix server
must be listening there: start the local one (steps 1 to 4 below) or set
`PHOENIX_COLLECTOR_ENDPOINT` to a team-hosted one. **Content capture stays
off**: traces carry metadata, token counts, and checkpoint IDs, not conversation
text. Set `PHOENIX_PROJECT_NAME` to group one agent's traces, and
`EAP_PHOENIX_CAPTURE_CONTENT=true` only with approved test data (see [Privacy and
content capture](#privacy-and-content-capture)). Restart after changing a
setting. Tracing covers `POST /invoke`, including Agent Lab's live response
mode; the standalone CLI and the `/invoke/stream` graph-step endpoint keep their
existing behavior.

A generated agent's own `agents/<name>/docs/tracing.md` gives the same quick
start with its name filled in. A service that composes by hand follows
[Compose tracing by hand](#compose-tracing-by-hand) instead.

The rest of this page sets up the local server (steps 1 to 4), lists every
setting, and covers the team-hosted option, privacy, hand composition, and
troubleshooting.

## 1. Install the Phoenix packages

Use Python 3.11 or newer and the pip index and certificates supplied by your team.
Your package administrators must make the pinned package and compatible wheels for
your platform available through the enterprise package repository. Open a terminal
in the `ai-agent-lib` repository and run:

```bat
start-phoenix.bat setup
```

The script installs `arize-phoenix==20.22.0` with pip into the package folder
`.agentlib\phoenix\.venv`, using your existing Python. Agent Lab and the agent
keep their own `.venv` packages. All three run with the default Python
executable, without using environment-local Python or pip binaries. You only
need to repeat setup after changing the pinned Phoenix version, changing the
selected Python version, or rebuilding its packages. The script preserves your
pip configuration.

## 2. Enable Phoenix in the root .env

Add these settings to **`.env` beside the launchers**. Keep your existing model
provider and API key in that file.

```dotenv
EAP_TELEMETRY=off
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

`EAP_PHOENIX_CAPTURE_CONTENT=true` records model messages and tool
arguments/results, so use approved test data; its default is `false`. The agent's
existing JSONL traces, audit records, and checkpoint database continue to work
independently of Phoenix.

## 3. Start the stack

From a terminal in the repository root:

```bat
start-local.bat
```

The launcher reads the root `.env`, starts Phoenix, and waits for its health check
before starting the MCP server, rmprep, and Agent Lab. Phoenix uses
the same Python as the agent and UI, with its own package folder.
Startup checks that the packages match the selected Python; it does not install packages. If it is
missing, run `start-phoenix.bat setup`, then try again.

Open Agent Lab at <http://127.0.0.1:8765> and Phoenix at
<http://127.0.0.1:6006>. This local, single-user Phoenix setup needs no separate
login. Check the printed model and settings before testing. Read the Agent Lab
access code with `type .agentlib\ui-rmprep\access-code` in another terminal.

Keep the launcher window open. Run `stop-local.bat` in a second terminal
to stop the UI, agent, MCP server, and Phoenix, in that order. It also stops a
standalone `start-phoenix.bat` run from this checkout. **Ctrl+C** in the original
window also works. SQLite stores traces in
`.agentlib\phoenix\data\phoenix.db`; restarting keeps them. The `.agentlib` folder
is excluded from Git. Phoenix's startup log appears alongside the other service
logs in `.agentlib\local-runs\<run-id>\phoenix.log`.
Service log files use UTF-8, including first-run database messages on Windows.

`start-local.bat services` also includes Phoenix when enabled. UI-only and scripted
demo runs keep their existing scope. Variables set in the terminal override the root
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
The native Phoenix banner may still list gRPC; this local setup uses HTTP only.
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

<details>
<summary>Captured runs: October 15 and 16, 2026</summary>

These are dated records of real runs through Agent Lab, kept so you can compare
your own numbers. Model output, caching, and timing vary between runs.

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

</details>

## Settings

From your agent workspace, run `python -m ai_agent_lib_cli config explain rmprep`
to see effective Phoenix settings and their source files, with API keys masked.
Run `python -m ai_agent_lib_cli doctor rmprep` to validate them before startup.
These tracing checks do not contact Phoenix. Startup, `doctor`, `config explain`,
and the launcher's summary all resolve the settings through the same function,
`resolve_phoenix_settings`, so a check shows what startup will use, including a
separate `EAP_PHOENIX_ENV_FILE` when one is configured.

| Variable | Purpose | Default |
| --- | --- | --- |
| `EAP_PHOENIX_ENABLED` | Enable tracing; `start-local.bat` also starts the local server | `false` |
| `EAP_PHOENIX_CAPTURE_CONTENT` | Include model and tool input/output | `false` |
| `EAP_PHOENIX_ENV_FILE` | Optional separate agent tracing settings file | Unset |
| `PHOENIX_COLLECTOR_ENDPOINT` | HTTP loopback or HTTPS origin, without `/v1/traces` | `http://127.0.0.1:6006` |
| `PHOENIX_PROJECT_NAME` | Project for this agent's spans: 1 to 128 letters, digits, `_` or `-` | `agent-lab` |
| `PHOENIX_API_KEY` | Optional key for an authenticated Phoenix server, sent as a bearer token | Empty for the local server; masked in startup output |

Agent settings resolve in this order: process environment, separate tracing file,
then service `.env`. A separate file path in service `.env` is relative to that
file. Use an absolute path when setting it in the root `.env` or the process
environment. Setting `EAP_PHOENIX_ENABLED=false` in the process environment also
bypasses a separate tracing file. The Phoenix server uses its own fixed local
settings and does not load agent credentials.

These variables are read by Phoenix's own loader, not by the configuration
binding table. The validator accepts the three settings that start with `EAP_`;
the `PHOENIX_*` names are the vendor's own and are not checked for typos. [ADR
0004](adr/0004-tracing-configuration.md) records registering all six in the
binding table as future work.

## Send traces to a team-hosted Phoenix

Phoenix is local-first and remote-capable. The local server above is the default.
For a shared deployment, point the agent at a Phoenix server that your platform
team runs. This repository does not deploy or operate one: your platform team owns
its hosting, authentication, storage, backups, and retention.

On the agent side, set:

```dotenv
EAP_PHOENIX_ENABLED=true
PHOENIX_COLLECTOR_ENDPOINT=https://phoenix.your-company.example
PHOENIX_PROJECT_NAME=your-agent
```

- `PHOENIX_COLLECTOR_ENDPOINT` is the collector's origin, without `/v1/traces`.
  Startup rejects plain HTTP unless the host is loopback, and rejects a path,
  query, or embedded credentials.
- Add `PHOENIX_API_KEY` when the server requires one. The agent sends it as a
  bearer token. Startup output, `config explain`, and trace links never show it.
- Export is asynchronous and best effort. A collector outage never retries a
  business request or discards an accepted answer.
- Agent Lab's **Open trace in Agent Lab** link opens
  `<endpoint>/redirects/traces/<trace-id>`, so readers' browsers must reach that
  origin and sign in to Phoenix themselves. Agent Lab accepts HTTPS links for
  this, and plain HTTP only for loopback.

**Where the API key lives.** `PHOENIX_API_KEY` is read from the process
environment (or the separate tracing file, or the service `.env`), like every
Phoenix setting. It does not go through the secrets provider, so
`EAP_SECRETS_PROVIDER` does not apply to it. On AWS Fargate, define it as a
secret in the ECS task definition so the platform injects it into the container's
environment as `PHOENIX_API_KEY`; keep it out of the image, the repository, and
any committed `.env`.

Decide capture before you point real traffic at a shared server. A shared Phoenix
stores whatever the agent sends, for everyone who can open that project; see the
next section.

## Privacy and content capture

Content capture is the one setting here with a privacy cost, so it is off unless
you turn it on.

| `EAP_PHOENIX_CAPTURE_CONTENT` | What a trace holds |
| --- | --- |
| `false` (default) | Includes, for example, span names and timings, token counts, request and thread IDs, a scoped session hash, checkpoint references, model and provider names, tool and MCP server names, artifact IDs, and error types. |
| `true` | All of the above plus model messages, tool arguments and results, and the request and accepted answer. For rmprep that includes client evidence. |

Even with capture off, request and thread IDs and checkpoint references identify
conversations, so plan access and retention for the Phoenix server. The browser
receives only a private trace URL; the API key stays in the agent process.

The rule for any file or server holding model traces, and for files derived from
real conversations such as feedback logs and exported interactions:

- **Treat them as content-bearing.** Credential masking is best effort, not a
  substitute for keeping the data private: a question or answer that contains a
  secret is retained verbatim.
- **Keep them private.** Local Phoenix keeps traces in
  `.agentlib\phoenix\data\phoenix.db`, and the optional model-trace JSONL
  (`EAP_MODEL_TRACE_PATH`) usually sits in a service's `.agentlib` folder; Git
  ignores `.agentlib`. Never commit them, and restrict access to them the way
  you would restrict access to the Phoenix server.
- **Use approved test data locally.** Enable capture only for data you are
  cleared to inspect, and turn it off again when you finish.

[Measure an agent with evals](evals.md#6-privacy) applies the same rule to
feedback logs, exported interactions, and scored reports.

## Compose tracing by hand

A service built from the generated template, or started with `run_agent`, needs
none of this. Do it yourself only when a service composes its own entry point.

Use `agent_tracing` from `ai_agent_lib_core.integrations.langgraph.tracing`. Its
signature is `agent_tracing(application, config, *, dotenv_path=Path(".env"),
settings=None)`. It resolves the Phoenix settings from the process environment
and `dotenv_path` (pass `None` to read only the process environment, or pass
already-resolved `settings` to skip both), and yields a running adapter, or
`None` when tracing is off. Wrap the service lifetime so the container closes
before the exporter flushes, and pass the one adapter to both the container and
the response handler:

```python
from ai_agent_lib_core.integrations.langgraph.tracing import agent_tracing

async with agent_tracing(APPLICATION, config, dotenv_path=ENV_FILE) as tracing:
    services = ServiceContainer(config, model_trace=tracing)
    try:
        ...  # start and validate services, then build `graph`

        async def run(context, given):
            question = parse_question(given)
            if tracing is not None:
                return await tracing.run(
                    graph, services, context, question, respond=your_response_function
                )
            return await your_response_function(graph, services, context, question)

        ...  # serve, with `run` as the request handler
    finally:
        await services.aclose()
```

`your_response_function(graph, services, context, question)` keeps its current
behavior and returns the output dictionary. It must invoke the graph it is given,
because that graph carries the tracing callbacks. The adapter copies that
dictionary and adds `trace` metadata, so treat that key as reserved while tracing
is enabled. For isolated tests, construct
`PhoenixTracing(settings, application=..., exporter=...)` from
`ai_agent_lib_core.integrations.langgraph.phoenix` with an in-memory exporter
(OpenTelemetry's `InMemorySpanExporter`) instead of running a server. The rmprep service shows the full composition through `run_agent`.

The agent needs the library's `otel` extra, installed with
`python -m pip install "ai-agent-lib-core[otel]"`; generated agents already
declare it. Enabling tracing without it fails at startup with that command. The
agent does not import the Phoenix server package.

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
| Package installation fails | Ask your package administrators for `arize-phoenix==20.22.0` and compatible wheels for your Python version and platform. |
| Phoenix environment is missing | Run `start-phoenix.bat setup` once, then retry `start-local.bat`. |
| Port 6006 is in use | Stop a standalone Phoenix window before starting the combined stack. The launcher fails before starting services and leaves the existing process alone. |
| Repeated connection errors on port 4318 | Set `EAP_TELEMETRY=off` for this local setup, or configure your separate operational collector. Restart the stack. |
| Startup says tracing needs `ai-agent-lib-core[otel]` | Install the extra with pip, as shown in [Compose tracing by hand](#compose-tracing-by-hand), then restart. |
| Startup rejects `PHOENIX_COLLECTOR_ENDPOINT` | Use an HTTPS origin, or HTTP for loopback only, with no path, query, or credentials. |
| Startup fails with `unknown configuration variable` naming a Langfuse setting | Langfuse support was removed. Delete those lines from the `.env`; see [Since the October review](release-scope.md#since-the-october-review). |
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

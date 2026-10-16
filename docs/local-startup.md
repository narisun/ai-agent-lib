# Start the local testing environment

Use these batch files from **Windows Command Prompt** to run the rmprep agent,
its MCP data server, and **Agent Lab** together. Agent Lab is the browser UI for
testing agents and inspecting their responses. You need Python 3.11 or newer,
pip configured for your enterprise package index, and this repository on your
desktop. The scripts create virtual environments inside the checkout using
your normal user permissions.

## 1. Set up once

Open Command Prompt in the repository's top-level `ai-agent-lib` folder:

```bat
python --version
python -m pip --version
setup-local.bat
```

Setup uses your existing pip index, proxy, and certificate configuration. Follow
your team's Python setup instructions first if those commands fail. When your
organization provides a Python executable at a specific path, you can run:

```bat
"C:\path to approved Python\python.exe" tools\local_dev.py setup
```

Setup installs the library, rmprep agent, and MCP server in `.venv`, and installs
the UI in `packages\ai-agent-lib-ui\.venv`. The two environments use compatible
dependencies for their own services. The launcher selects each Python executable
directly, so you can run it from a fresh Command Prompt window without activating
an environment.

Setup also copies each service's `.env.example` to `.env` when needed and creates
the UI configuration in `.agentlib\ui-rmprep\config.json`. **Existing settings,
credentials, and conversations are kept.** Run setup again after changing package
dependencies or pulling changes that introduce new dependencies.

The default model is `fake`: it echoes your question and lets you check chat and
saved threads. To use Anthropic, put your settings in **`.env` beside
`start-local.bat`** (the repository root):

```dotenv
EAP_MODEL_PROVIDER=anthropic
EAP_MODEL_ID=claude-sonnet-4-5
ANTHROPIC_API_KEY=your-approved-key
```

Use a model enabled for your account. Install its Python dependency in the agent
environment, using your approved pip index:

```bat
.venv\Scripts\python.exe -m pip install "langchain-anthropic>=1.7.5,<2"
```

The launcher reads that file automatically for the rmprep agent. You can instead
select an existing file, without copying credentials:

```bat
start-local.bat --env-file "F:\path to your settings\.env"
```

Settings take precedence in this order: **Command Prompt environment variables →
the selected file (or root `.env`) → `examples\rmprep\agents\rmprep\.env` → library
defaults**. The selected file replaces the root file, rather than merging both.
Startup prints the loaded file path and effective model provider/ID; keys are not
printed. Check that it says `Agent model: anthropic` before testing.

The startup summary also lists configuration variables and the source of each
winning value. For example:

```text
Agent model: anthropic / claude-sonnet-4-5
Agent configuration (credentials, sensitive fields and unrecognized variables are masked):
  ANTHROPIC_API_KEY=********  [launcher .env]
  EAP_DEPLOYMENT_ENV="local"  [default]
  EAP_MODEL_ID="claude-sonnet-4-5"  [launcher .env]
  EAP_MODEL_OPTIONS={"anthropic": {"prompt_caching": true, "cache_ttl": "5m"}}  [service .env]
  EAP_MODEL_PROVIDER="anthropic"  [launcher .env]
  EAP_PROFILE="local"  [launcher .env]
  RMPREP_CONTEXT_INPUT_TOKENS="64000"  [service .env]
```

`shell` means the current Command Prompt environment; `launcher .env` means
the selected file or the root `.env`; `service .env` means rmprep's own file.
`default` and `profile local` identify library defaults. Blank credentials appear
as `(empty)`; core settings that treat blank as unset show their resolved default.
The launcher also prints the separate MCP settings
and UI configuration file paths.

JSON options keep their structure and non-sensitive values: caching options,
paths, roles, timeouts and token budgets remain readable. Sensitive fields are
masked recursively inside objects and lists, including passwords, API keys,
tokens, signing keys, identity subjects and contact details. Credential containers
and database connection strings are masked as a whole. URLs keep their endpoints
and ordinary query settings while masking embedded credentials, sensitive query
parameters and fragments. Custom HTTP header values are masked; ordinary content
headers such as `Accept` and `Content-Type` remain visible.

Top-level credentials, including Anthropic, Langfuse and AWS keys, stay masked.
Unrecognized file-defined variables are masked too. Output stays on one line per
variable, with long text values shortened after masking. Unrelated shell variables
are omitted. This is a startup summary of the
agent's environment and core settings; settings in referenced files are read by
their respective providers.

The root file is found beside the batch file even when you launch from another
folder. An explicit relative `--env-file` path uses your current Command Prompt
folder. A missing default root file keeps the service settings; a missing explicit
file stops startup with an error. These overrides apply to `rmprep` and `services`.
The UI and MCP keep their separate settings, and `demo` uses scripted replies.

Keep registry, identity and local storage settings in each service's own `.env`.
Relative library configuration paths, including `EAP_MODEL_TRACE_PATH`, continue
to resolve from the rmprep service's folder, even when set in the root file.
For other model providers, follow the
[rmprep model setup](../examples/rmprep/README.md#3-generate-a-memo-with-your-configured-model).
Starting services checks readiness; model requests happen when you send a message.

## 2. Start a scenario

Run one of these commands from the repository root:

| Command | What it starts | Use it when |
| --- | --- | --- |
| `start-local.bat` | rmprep MCP server, rmprep HTTP agent, and Agent Lab | You want to test rmprep in a browser. |
| `start-local.bat services` | rmprep MCP server and HTTP agent | You want to call the agent from your own client. |
| `start-local.bat ui` | Agent Lab using its saved configuration | The agents and MCP servers are already running. |
| `start-local.bat demo` | The scripted rmprep trace and checkpoint walkthrough; exits when complete | You want to see tool calls and conversation history using scripted model replies. |
| `start-local.bat --smoke-test` | The full stack, checks readiness, then stops it | You want to verify local startup before testing. |

The full stack starts in this order:

1. Phoenix at `http://127.0.0.1:6006`, when enabled for the local trial.
2. The MCP data server at `http://127.0.0.1:8100/mcp`.
3. The rmprep agent at `http://127.0.0.1:8000`.
4. Agent Lab at `http://127.0.0.1:8765`.

The launcher waits for each service to become ready before starting the next.
Keep the Command Prompt window open while testing. All services run on your
desktop and listen on loopback.

You can also call the batch file by its full path from another folder:

```bat
"F:\work\ai-agent-lib\start-local.bat"
```

For optional centralized tracing, follow the [shared Langfuse guide](langfuse.md).
The [rmprep walkthrough](langfuse-trial.md) records a verified example.
`start.bat` in the sibling `langfuse` folder manages its separate container backend. The agent and
UI continue to use their Python environments and `start-local.bat`. Restricted
desktops can connect to a shared Langfuse service instead.

## 3. Open Agent Lab and test an agent

When the launcher prints **All selected services are ready**, open
<http://127.0.0.1:8765> in your browser. Read the login code in a second Command
Prompt window, from the repository root:

```bat
type .agentlib\ui-rmprep\access-code
```

Sign in with that code, select **Choose agent → Start chat with rmprep**. With a real
model configured, try:

```text
memo: Prepare a meeting memo for JONE-0022 as of 2026-08-03.
```

Then ask in the same conversation:

```text
qna: Explain the main credit risks in that memo and cite the source records.
```

Select **Memos** in the header to read accepted memos beside chat and switch
versions. Select **Logs**, choose a chat, and open a turn to see LLM input/output,
MCP calls and checkpoints. The left sidebar keeps chat history; use
**Logs → Manage chat history** for rename and archive.

### Understand the response

The UI shows an expandable **Live draft (provisional)** while the model responds.
Default pattern guardrails check each growing text update before releasing it.
Blocking PII checks and structured-output validation wait for a complete response.
When the outcome is saved, the temporary draft panel disappears. Each reply shows
input/output token counts and elapsed time; cache-hit ratio appears when reported.
With Phoenix enabled, **Open trace in Agent Lab** opens that response's Phoenix
trace. Reopening a conversation restores its saved answer, metrics, and trace link.
The fake model returns its draft at once and reports simulated usage.
After changing library or UI code, run `stop-local.bat`, then `start-local.bat`.

### Inspect memos and model calls

New batch configurations enable rmprep's `artifacts` capability. For an existing
`.agentlib\ui-rmprep\config.json`, add `"artifacts"` to the rmprep binding's
`manifest.capabilities` array, then restart. Generate a new memo: older text-only
answers remain in chat. Follow the [panel guide](../packages/ai-agent-lib-ui/README.md#memos-beside-chat)
for version selection and limits.

To enable model tracing, add this to the agent's `.env` and restart the stack:

```dotenv
EAP_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl
```

That trace is written under `examples\rmprep\agents\rmprep\.agentlib`. It contains
conversation content, so use your approved test data and keep it private.

For Anthropic prompt caching, add this to the rmprep agent's `.env` and restart:

```dotenv
EAP_MODEL_OPTIONS={"anthropic":{"prompt_caching":true,"cache_ttl":"5m"}}
```

Responses show cache reads and writes alongside the hit percentage. The first
request may write the cache with 0% hits; matching requests within five minutes
can read it. See [rmprep prompt versions and caching](../examples/rmprep/docs/context-management.md#static-prompts-and-anthropic-caching).

## 4. Stop and restart

Open a second Command Prompt in the repository root and run:

```bat
stop-local.bat
```

The command asks all active launchers from this checkout to shut down. Each stops
its own services in reverse order: Agent Lab, rmprep, the MCP server, then Phoenix.
It also covers separate `start-local.bat ui`, `start-local.bat services`, and
`start-phoenix.bat` runs. It needs only Python; it does not load credentials,
install packages, or identify processes by their names or ports.

Wait for **Stopped gracefully**, then run `start-local.bat` again. The stop command
waits up to 150 seconds. The launcher allows 30 seconds per service for cleanup;
if a service requires a forced stop, the command reports that graceful shutdown
was not confirmed and exits with an error. Read the service logs before restarting.
A repeated stop is harmless when no managed launchers are running.

**Ctrl+C in the original launcher window** also stops its stack. If Command Prompt
asks whether to terminate the batch job, answer `Y`. Use Ctrl+C before closing
that window. Services started before this update must be stopped this way once;
they have no launcher control record. Services launched outside these scripts,
other checkouts, and remote Phoenix/Langfuse servers are managed separately.

Conversations stay in `.agentlib\ui-rmprep\conversations.sqlite`, checkpoints stay
at the configured agent checkpoint path, and Phoenix traces stay in
`.agentlib\phoenix\data\phoenix.db`. JSONL traces and audit logs remain in place.
The stop command never resets or deletes these stores. Launcher status files live
beside each run's logs; stale records do not authorize stopping another process.

You can run the command by its full path from any folder:

```bat
"F:\work\ai-agent-lib\stop-local.bat"
```

Each run has service logs in `.agentlib\local-runs\<run-id>\`. The launcher prints
the exact filenames, which you can open in an editor or read with `type`.
If a service fails to start or exits later, the launcher stops the rest of its
stack and reports which log to read. Services have 90 seconds each to become ready.

## Use another UI configuration

The launcher gives rmprep its own UI state folder, separate from the manual UI
setup's `.agentlib\ui` folder. To use an existing configuration for other agents:

```bat
start-local.bat ui --config .agentlib\ui\config.json
```

Create that configuration with the [UI's `init` command](../packages/ai-agent-lib-ui/README.md)
first. Repeat `--agent` and `--allow-url` there for each registry agent you want
to use. The UI-only scenario starts the frontend; start those agents and their
MCP dependencies separately. Relative config paths are resolved from the repository
root, even if you launch the batch file from another folder.

## Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| `Required file is missing` or an import error | Run `setup-local.bat` from the repository root. Check that pip can access your approved index. |
| pip cannot find a package or build a wheel | Ask your package administrators to provide the dependency or an approved wheel; see the [UI installation notes](../packages/ai-agent-lib-ui/README.md#1-prepare-a-separate-ui-environment). |
| A required port is in use | Run `stop-local.bat` for managed runs; use Ctrl+C for a manually started service, or use `start-local.bat ui` if your backends are already running. The launcher leaves existing processes alone. |
| Registry pins or local identity changed | Review the registry and agent `.env`, then explicitly refresh the UI config using the command below. Existing conversations remain. |
| The agent only repeats the question | Configure a real model using the rmprep instructions above. Use `start-local.bat demo` for a scripted tool-call example. |
| Startup selects `fake` when Anthropic was expected | Check `Agent environment file` and `Agent model` in the launcher output. Set both provider and model ID alongside the API key. Use `set EAP_MODEL_PROVIDER=` to clear an unwanted shell override, then restart. |
| Chrome still shows an old layout after a UI update | Stop and restart the launcher, then reload the chat tab. Updated custom styles get a new asset URL on restart. Wide response tables still scroll within their own container. |
| A service does not become ready | Read its printed log path. Check both service `.env` files, the registry URLs, and local data paths. The MCP URL should use port 8100 and the rmprep agent URL port 8000 for this scenario. |

After reviewing changed registry or identity settings, refresh the launcher's
rmprep UI configuration from the repository root:

```bat
packages\ai-agent-lib-ui\.venv\Scripts\python.exe -m ai_agent_lib_ui init --config .agentlib\ui-rmprep\config.json --env-file examples\rmprep\agents\rmprep\.env --agent rmprep --artifacts-agent rmprep --allow-url http://127.0.0.1:8000 --overwrite
start-local.bat
```

The launcher checks saved registry pins on every startup. It does not overwrite
an existing UI configuration automatically.

## Local Phoenix tracing

The [Phoenix trial](phoenix-trial.md) uses a separate Python environment and SQLite.
Run `start-phoenix.bat setup` once from the repository root. Add these settings to
the root `.env`, alongside your model settings:

```dotenv
EAP_TELEMETRY=off
EAP_LANGFUSE_ENABLED=false
EAP_PHOENIX_ENABLED=true
EAP_PHOENIX_CAPTURE_CONTENT=true
PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006
PHOENIX_PROJECT_NAME=rmprep-trial
```

Use content capture with approved test data. Run `start-local.bat` to start
Phoenix, MCP, rmprep, and Agent Lab together. Phoenix uses
`.agentlib\phoenix\.venv`; its SQLite database is
`.agentlib\phoenix\data\phoenix.db`. Each response has an **Open trace in Agent Lab** link to its Phoenix trace.
`stop-local.bat` stops the stack in reverse order and keeps saved traces.
The **Logs** page remains available for local diagnostic inspection.

`services` and `--smoke-test` include Phoenix when enabled; `ui` and `demo` do not.
A different collector endpoint is managed separately. Startup reports that choice
and does not launch a local Phoenix instance. Stop any standalone Phoenix window
before starting the combined stack; an occupied port stops startup without taking
over the existing process. See the trial guide for settings and troubleshooting.

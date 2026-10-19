# Run and test rmprep locally

Use this guide to run **Agent Lab**, the **rmprep** agent, its **MCP data server**,
and optional **Phoenix tracing** on a Windows, macOS, or Linux desktop. The
launchers use your normal user account and the same enterprise-approved Python
executable for every service. Packages stay isolated in `.venv` folders; startup
and installation use the default Python and its pip module.

You need your team's Python 3.11+ and pip setup, an approved package index, a
writable checkout, and a browser. For Anthropic answers, you also need an approved
API key and model. Use a provider approved to receive the example business data.

Run commands from the repository root: the folder containing `start-local.bat`
and `start-local.sh`. Commands are shown for **Command Prompt (cmd.exe)**. On
macOS/Linux, open a bash or zsh terminal there and use the spelling in the right
column. The `.sh` launchers take the same arguments as their `.bat` twins and run
`python3` from your `PATH`, so that must be your approved Python 3.11+. Paths in
the commands and prose use Windows separators; use `/` on macOS/Linux.

| Command Prompt | macOS/Linux |
| --- | --- |
| `python …` | `python3 …` |
| `setup-local.bat --model anthropic` | `./setup-local.sh --model anthropic` |
| `start-local.bat` | `./start-local.sh` |
| `type .agentlib\ui-rmprep\access-code` | `cat .agentlib/ui-rmprep/access-code` |
| `stop-local.bat` | `./stop-local.sh` |
| `start-phoenix.bat setup` | `./start-phoenix.sh setup` |

Complete steps 1–4 once. On later days, run `start-local.bat`, test in the
browser, then run `stop-local.bat`.

## 1. Set up once

```bat
python --version
python -m pip --version
setup-local.bat --model anthropic
```

Setup uses your existing pip index, proxy and certificate configuration. All pip
commands run as `python -m pip` with the selected Python. Setup prints its path
and the model provider whose dependencies it will install.

| Component | Virtual-environment folder | How it runs |
| --- | --- | --- |
| Library, rmprep and MCP server | `.venv` | The same `python` used for setup |
| Agent Lab | `packages\ai-agent-lib-ui\.venv` | The same Python, loading only the UI package set |
| Phoenix, when installed | `.agentlib\phoenix\.venv` | The same Python, loading only the Phoenix package set |

Each folder holds its packages in `Lib\site-packages` on Windows and in
`lib/python3.11/site-packages` on macOS/Linux; that folder name follows the
major and minor version of the Python you run.

These package folders resolve an upstream dependency conflict: Chainlit requires
MCP 1.x, while the library's MCP server requires MCP 2.x. Services communicate
over HTTP. The launchers load each folder's packages without running the
interpreter or `pip` inside any `.venv`. Setup uses the default Python's pip
and preserves your enterprise package-source configuration. Compatible existing
`.venv` packages can be reused; a different Python version or base installation
requires setup again. Existing environment executables are left untouched.

Setup creates missing service settings and UI configuration while keeping
existing files and saved conversations. Stop services before repeating setup.
Repeat setup after dependency changes or switching Python versions. Component
packages are replaced after a successful installation and dependency check;
conversations, traces and checkpoints are stored separately and are preserved.

| Command | Packages installed |
| --- | --- |
| `setup-local.bat` | Core library, rmprep, rmprep-data, Agent Lab, and the model dependency selected by the shell, root `.env`, or service `.env` (in that order). Defaults to `fake`. |
| `setup-local.bat --model bedrock` | The above with `ai-agent-lib-aws[bedrock]`, including `langchain-aws` and boto3. |
| `setup-local.bat --model anthropic` | The above with the core library's Anthropic extra. |
| `setup-local.bat --dev` | Also installs the scaffolding CLI and the rmprep/Agent Lab test, lint, and type-check tools. |
| `start-phoenix.bat setup` | The pinned Phoenix server and its required dependencies. |

`--model` selects packages to install; it does not change your runtime `.env`.
You can combine it with `--dev`. Each setup replaces the selected component's
package set; include `--dev` again to retain development tools. Pip resolves dependencies;
keep dependency resolution enabled.

The first command block selects Anthropic for this walkthrough. For Bedrock,
use `--model bedrock` instead. If your root `.env` already selects the model,
`setup-local.bat` detects it automatically. To add Phoenix tracing, run:

```bat
start-phoenix.bat setup
```

Use the same approved Python for setup and startup, in a fresh terminal
without activating `.venv`. In your team's prepared
terminal, `where python` (`which python3` on macOS/Linux) and `python -m pip --version`
show the selected installation. If your team provides an explicit path, use
`"C:\path to approved Python\python.exe" tools\local_dev.py setup --model bedrock`
(`/path/to/python3 tools/local_dev.py setup --model bedrock` on macOS/Linux)
and that same executable with `tools\local_dev.py` to start services. Package
access and native wheels must come from your approved index.

## 2. Set the model and tracing options

Create or edit **`.env` beside the launchers**. Keep your existing settings;
add or update these entries, replacing the API-key placeholder:

```dotenv
AC2_MODEL_PROVIDER=anthropic
AC2_MODEL_ID=claude-sonnet-4-5
ANTHROPIC_API_KEY=your-approved-key
AC2_MODEL_OPTIONS={"anthropic":{"prompt_caching":true,"cache_ttl":"5m"}}
```

Use a model enabled for your account. Keep the file private and out of Git.
For a basic connectivity check, you can keep the generated `fake` model; it
echoes questions. Business memos and natural-language follow-ups need a real model.

For **Amazon Bedrock**, use these model settings instead:

```dotenv
AC2_MODEL_PROVIDER=bedrock
AC2_MODEL_ID=your-enabled-model-or-inference-profile-id
AWS_REGION=your-aws-region
```

Use the AWS credentials or profile provided by your enterprise setup. If your
team supplies a named profile, set `AWS_PROFILE` to that profile. Run
`setup-local.bat --model bedrock`, then restart. This installs `langchain-aws`;
model access and authentication still come from your AWS account. See the
[AWS package guide](../packages/ai-agent-lib-aws/README.md) for provider options.

To see model input/output and MCP calls in local Phoenix, add:

```dotenv
AC2_TELEMETRY=off
AC2_PHOENIX_ENABLED=true
AC2_PHOENIX_CAPTURE_CONTENT=true
PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006
PHOENIX_PROJECT_NAME=rmprep-trial
```

Content capture includes messages, client evidence and tool arguments/results.
`AC2_TELEMETRY=off` disables the separate operational exporter; Phoenix still
receives model/tool spans and usage. See the [Phoenix guide](phoenix-trial.md)
for settings and trace details.

The root file supplies rmprep's model and tracing settings to the launcher.
Keep registry, identity and storage settings in each service's own `.env`.
Manual `python tools\package_runtime.py agent -m rmprep` commands use the service's settings; they do not load
the launcher's root file automatically.

## 3. Open Agent Lab and test an agent

```bat
start-local.bat
```

Before sending a question, check the startup summary:

- **Agent environment file** points to your root `.env`.
- **Agent model** shows `anthropic` and your model ID.
- Credentials appear as `********`. JSON options show ordinary values, with
  sensitive fields masked. Each row identifies where the value came from.
- If enabled, Phoenix becomes ready before MCP, rmprep and Agent Lab.

Wait for **All selected services are ready**, then open
[Agent Lab](http://127.0.0.1:8765). Keep the launcher window open. In a second
terminal at the repository root, read the local access code:

```bat
type .agentlib\ui-rmprep\access-code
```

Sign in with that code, then select **Choose agent → Start chat with rmprep**.
This code belongs to Agent Lab; it is separate from your model API key.
Local Phoenix opens at <http://127.0.0.1:6006> without a separate login.

### Generate a memo and ask follow-ups

Send these messages one at a time in the **same chat**:

1. `Prepare a meeting memo for Jones Dairy Farm (JONE-0022) as of 2026-08-03.`
2. `Explain the covenant breaches and the change in cash conversion cycle in that memo. Also, how was its wallet share calculated? Use the saved evidence.`
3. `Does the recorded waiver expiry prove the waiver was executed? Explain what the evidence establishes and what remains unknown.`

Check the answer against the supplied snapshot:

| Topic | Expected evidence |
| --- | --- |
| Covenants | DSCR 1.08 against a minimum 1.25; fixed charge coverage 0.96 against a minimum 1.15. |
| Cash conversion cycle | 87 days at 2026-03-31 to 106 days at 2026-06-30: an increase of 19 days. The values alone do not establish causes. |
| Wallet share | Recorded as 40.21%. The source does not supply the numerator, denominator, period or calculation method. |
| Waiver | A recorded expiry date does not prove execution. The answer should preserve that uncertainty. |

The first question retrieves MCP evidence. A follow-up can reuse complete saved
evidence without another MCP call. Inspect its model input to confirm the selected
memo and records are present. Model wording and call counts can vary.

### Test client switching and older memos

In the same chat, ask `Prepare a memo for Canada Goose as of 2026-08-03.`, then
`How was the wallet share in that memo calculated?`. The selected client should
be `CANA-0010`, with recorded wallet share **22.55%**. Its calculation method is
also unavailable in the source.

Open **Memos**, select the earlier Jones memo, and expand **Memo details** to copy
its reference. Ask `Using <memo-reference>, explain its covenant breaches.`,
replacing the placeholder with that reference. Selecting a version in the panel
changes the viewer; name its reference in the question to select it for the agent.
Both clients' memo versions remain saved. Reopen this chat from the sidebar to
continue it, or start a new chat for a separate conversation.

### Read the response and trace

Text appears in **Live draft (provisional)** as approved model output arrives.
The panel disappears when the final outcome is ready. Each response keeps token
usage and elapsed time visible, plus cache statistics when reported.

**Open trace in Agent Lab** opens that turn's Phoenix trace. Select a model span
for input/output, a tool span for MCP arguments/results, and the root span for
request, thread and checkpoint references. Allow a few seconds for ingestion.
**Logs** provides local diagnostics and saved checkpoint inspection. Opening a
trace or log does not run the agent again.

Chat presents readable answers without long source references beside every
figure. The original answer and memo documents retain their references for review.
MCP service logs include the query, parameters and record statistics.

Ask `Keep it within 450 words.` to change an answer's length limit. The agent
checks length and exact source references before it saves an answer. A failing
draft can be revised twice; rejected drafts remain in traces rather than
accepted conversation history. The [rmprep README](../examples/rmprep/README.md#3-generate-a-memo-with-your-configured-model)
lists the default limits, how words are counted and every check; see also
[context management](../examples/rmprep/docs/context-management.md).

## 4. Stop and restart

In a second terminal at the repository root:

```bat
stop-local.bat
```

Wait for **Stopped gracefully**. The launcher stops Agent Lab, rmprep, MCP and
local Phoenix in reverse order and preserves their data. Run `start-local.bat`
again to resume testing. Restart after editing code or environment settings.
**Ctrl+C** in the launcher window also stops its stack; use it before closing
that window. On Windows, if Command Prompt asks about terminating the batch job,
answer `Y`.

| Saved data | Default location under the repository root |
| --- | --- |
| Conversations, answers and memo versions | `.agentlib\ui-rmprep\conversations.sqlite` |
| rmprep checkpoints | `examples\rmprep\agents\rmprep\.agentlib\checkpoints.sqlite` |
| Phoenix traces | `.agentlib\phoenix\data\phoenix.db` |
| Service console logs | `.agentlib\local-runs\<run-id>\` — startup prints the exact path |
| Audit and optional model JSONL files | Each service's `.agentlib` folder |

`stop-local.bat` covers managed launchers from this checkout, including separate
UI, services and Phoenix runs. Stop manually launched services with Ctrl+C in
their own windows. Remote tracing servers have their own lifecycle. If graceful
shutdown is not confirmed, read the reported log before restarting. The stop
command waits up to 150 seconds; repeating it with no managed services is harmless.

## Run local checks

With the stack stopped, verify startup and shutdown without sending a model request:

```bat
start-local.bat --smoke-test
```

It starts configured services, waits for readiness, then stops them. To exercise
MCP tools and checkpoints with scripted model replies, run:

```bat
start-local.bat demo
```

Install the development tools once, then run the focused tests. From the
repository root:

```bat
setup-local.bat --dev
cd examples\rmprep
python ..\..\tools\package_runtime.py agent -m pytest -q
cd ..\..
cd packages\ai-agent-lib-ui
python ..\..\tools\package_runtime.py ui -m pytest -q
cd ..\..
```

Run UI tests from the UI package folder so pytest uses that package's configuration.
These tests use local fixtures and scripted models. Full contributor checks are
in the [library README](../README.md#working-on-the-library) and
[Agent Lab README](../packages/ai-agent-lib-ui/README.md#run-the-checks-and-build-a-wheel).

## Other startup scenarios

| Command | What it starts |
| --- | --- |
| `start-local.bat` | Local Phoenix when enabled, MCP, rmprep and Agent Lab. |
| `start-local.bat services` | Local Phoenix when enabled, MCP and rmprep; use your own HTTP client. |
| `start-local.bat ui` | Agent Lab using its saved configuration; backends must already be running. |
| `start-phoenix.bat` | Phoenix alone for browsing saved traces. Stop it before the combined stack. |

Local ports are Phoenix **6006**, MCP **8100**, rmprep **8000**, Agent Lab **8765**.
The MCP URL is `http://127.0.0.1:8100/mcp`. Each service gets up to 90 seconds to
become ready. An unexpected exit stops the rest of that launcher's stack and
reports the relevant log.

For another agent, follow [Agent Lab's manual setup](../packages/ai-agent-lib-ui/README.md).
Then use `start-local.bat ui --config .agentlib\ui\config.json` and start its
agent/MCP services separately. Relative `--config` paths use the repository root.
The default rmprep configuration is `.agentlib\ui-rmprep\config.json`.

## Which environment settings are used?

Startup prints effective settings and sources. For rmprep, a setting comes from
the highest-priority source below that defines it:

| Priority | Source | How it is found |
| --- | --- | --- |
| 1 | Process environment | Variables set in the terminal that runs the launcher. They override every file below. |
| 2 | Launcher file: the root `.env`, or the file passed with `--env-file` | The root file is found beside the launcher even when you start it from another folder. `--env-file` replaces it, and a relative path uses your current folder. A missing default root file keeps service settings; a missing explicit file stops startup. |
| 3 | Separate Phoenix env file, `AC2_PHOENIX_ENV_FILE` (optional) | Adds one level for the tracing settings. Prefer an absolute path; a relative one resolves from `examples\rmprep`, or from the service folder when set in a service `.env`. |
| 4 | rmprep service `.env` | `examples\rmprep\agents\rmprep\.env`, the rmprep service's own settings file. |
| 5 | Defaults | The library's built-in values. |

Wherever a value is set, library storage paths such as `AC2_MODEL_TRACE_PATH` resolve from the service folder.

To select a different file instead of the root `.env`:

```bat
start-local.bat --env-file "F:\path to your settings\.env"
```

`rmprep` and `services` apply the launcher file; UI and MCP keep separate
settings, and `demo` uses scripted replies.

The summary masks credentials, sensitive JSON fields, database connection strings
and unknown custom variables. Ordinary options such as caching, timeouts and
token budgets remain readable. It also names the MCP and UI settings files.
For optional file-based model inspection, add
`AC2_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl` to rmprep's settings and restart.
Capture starts with new turns; retain the file to inspect them later.

## Troubleshooting

| Symptom | First step |
| --- | --- |
| Missing package or mismatched Python | Run `setup-local.bat`; for Phoenix, run `start-phoenix.bat setup`. Check your approved pip index and wheels. |
| Login asks for a code | Run `type .agentlib\ui-rmprep\access-code`. Use `127.0.0.1:8765` consistently. |
| Startup selects `fake` | Check the printed environment path. Set provider and model ID along with the key. Clear an unwanted shell override with `set AC2_MODEL_PROVIDER=` (`unset AC2_MODEL_PROVIDER` on macOS/Linux), then restart. A shell that still exports the old `EAP_MODEL_PROVIDER` spelling needs it cleared the same way. |
| A port is in use | Run `stop-local.bat` for managed runs, or Ctrl+C in a manually started service's window. Existing processes are left alone. |
| No Phoenix trace or missing content | Check enabled/content-capture settings, restart, send a new question and allow time for ingestion. See [Phoenix troubleshooting](phoenix-trial.md#troubleshooting). |
| Cache hit is 0% | Check cache writes. A cold request, changed prompt/context, or expired entry can write a prefix; a later matching request can read it. |
| The answer revises itself | Inspect model calls in the trace. Drafts are provisional until length, citation and evidence checks pass. |
| Wrong memo is discussed | Keep the same chat and name the reference from **Memo details**. Viewing an older version alone does not select its model context. |
| An old layout appears in Chrome | Stop/restart, then reload the tab. Wide tables scroll within their own container. |
| Registry pins or local identity changed | Review the changes, then refresh UI configuration below. Saved conversations remain. |

After reviewing registry or identity changes, stop the stack and refresh its UI
configuration from the repository root:

```bat
python tools\package_runtime.py ui -m ai_agent_lib_ui init --config .agentlib\ui-rmprep\config.json --env-file examples\rmprep\agents\rmprep\.env --agent rmprep --artifacts-agent rmprep --allow-url http://127.0.0.1:8000 --overwrite
start-local.bat
```

Startup checks saved bindings and preserves existing UI configuration. Changing
the configured identity makes the old identity's conversations inaccessible to
the new identity; it does not delete them.

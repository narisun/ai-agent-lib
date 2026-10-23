# Agent Lab

<img src="src/ai_agent_lib_ui/assets/agent-lab.svg" alt="Agent Lab logo: a flask with three connected nodes" width="72" height="72">

Agent Lab is a local workspace for testing registered agents through chat and
inspecting how each response was produced. It uses Chainlit for chat and
connects to agents over HTTP.
Install it with the Python package `ai-agent-lib-ui`; commands continue to use
`ai_agent_lib_ui`. The repository launcher supplies its isolated package path.

To test the included rmprep agent, follow the
[local startup and testing guide](../../docs/local-startup.md). It covers the
batch scripts, root `.env`, Anthropic/Phoenix setup, access code, sample questions,
and stop/restart. The manual instructions below connect **your own agents**.

Use steps 1–4 below to connect your own agents. After your first chat, see
[responses and inspection](#responses-and-inspection), [memos](#memos-beside-chat),
[interrupted requests](#interrupted-requests), and
[online evals from saved chats](#online-evals-from-saved-chats). For code changes,
start with [how the code fits together](#how-the-code-fits-together).

This checkout supports a local developer with a configured static identity.
Enterprise SSO and shared hosting remain separate deployment work.

## 1. Prepare the UI packages

Use company-provided **Python 3.11 or newer**, pip configured for JFrog or your
approved package index, and a browser. Open **Command Prompt** (Windows) or a
**Bash terminal** (macOS/Linux) at the repository root.
For the complete rmprep stack, use `setup-local.bat` (`./setup-local.sh` on POSIX).
To install just Agent Lab:

```bat
python tools\package_runtime.py ui --install -e packages\ai-agent-lib-core -e "packages\ai-agent-lib-ui[chat]"
python tools\package_runtime.py ui --check
```

Or on POSIX:

```bash
python3 tools/package_runtime.py ui --install -e packages/ai-agent-lib-core -e "packages/ai-agent-lib-ui[chat]"
python3 tools/package_runtime.py ui --check
```

Commands are shown for Windows, then for POSIX where they differ. Paths in the
prose use Windows separators; on macOS/Linux use `/` and `python3`, for example
`.agentlib/ui/config.json`.

The `chat` extra installs Chainlit 2.12.0 and its required dependencies into
`packages\ai-agent-lib-ui\.venv\Lib\site-packages` (`.venv/lib/python3.x/site-packages`
on macOS/Linux). The launcher uses your existing Python with
that package folder. It runs the default Python and its pip module; `.venv` executables are never
selected. Use a fresh terminal without activating an environment.
Chainlit requires MCP 1.x; the agent/MCP server uses MCP 2.x. Separate package
sets let these HTTP services use the same Python executable reliably.

Pip retains your enterprise index and certificate configuration. Your platform
team must supply approved packages and native wheels. To install supplied core
and UI wheels, replace the editable requirements after `--install` with their
wheel paths, retaining the UI wheel's `[chat]` extra. This preview has not been
published as a release. Stop the UI before reinstalling its packages.

The commands below use `python tools\package_runtime.py ui -m ai_agent_lib_ui`
from the repository root. This supplies the UI packages without changing the
agent's imports. After changing Python versions, reinstall the UI package set.

## 2. Start your agent and register its address

Start the agent and its MCP services in their own Command Prompt windows (or Bash
terminals) using the same approved Python. The agent must accept `POST /invoke` with
`input.question` and return `output.answer`, `thread_id`, and `request_id`.

For [rmprep](../../examples/rmprep/README.md), run `setup-local.bat`
(`./setup-local.sh` on POSIX) and configure its service `.env` first. These direct
commands do not load the root `.env` used by `start-local.bat` (`./start-local.sh`).
In a fresh Command Prompt at the repository root, start the MCP service with
your approved Python:

```bat
python tools\package_runtime.py agent -m rmprep_data
```

Or on POSIX:

```bash
python3 tools/package_runtime.py agent -m rmprep_data
```

In another Command Prompt at the repository root, start the HTTP agent:

```bat
python tools\package_runtime.py agent -m rmprep.service --host 127.0.0.1 --port 8000
```

Or on POSIX:

```bash
python3 tools/package_runtime.py agent -m rmprep.service --host 127.0.0.1 --port 8000
```

The checked-in `rmprep` entry in `examples\rmprep\registry\agents.yaml` includes
this address. For another agent, add its address at the same indentation as `owner`:

```yaml
  url: http://127.0.0.1:8000
```

Keep its other properties. The registry identifies the service; the UI's explicit
URL allowlist determines which addresses it may call. The default fake model
checks connectivity; follow the rmprep walkthrough to configure a model for
business answers.

## 3. Initialize the UI

Return to a Command Prompt at the repository root. Run the first command as one line:

```bat
python tools\package_runtime.py ui -m ai_agent_lib_ui init --env-file examples\rmprep\agents\rmprep\.env --agent rmprep --artifacts-agent rmprep --allow-url http://127.0.0.1:8000
python tools\package_runtime.py ui -m ai_agent_lib_ui check
```

Or on POSIX:

```bash
python3 tools/package_runtime.py ui -m ai_agent_lib_ui init --env-file examples/rmprep/agents/rmprep/.env --agent rmprep --artifacts-agent rmprep --allow-url http://127.0.0.1:8000
python3 tools/package_runtime.py ui -m ai_agent_lib_ui check
```

`init` reads `AC2_REGISTRY_OPTIONS` and `AC2_IDENTITY_OPTIONS` from that agent's
`.env`. Relative registry paths resolve from the file's directory. Local setup
uses `AC2_PROFILE=local`, the `file` registry and `static` identity providers. Model API
keys and shell overrides are not imported. Confirm the file matches the running
service's settings.

The command creates `.agentlib\ui\config.json`, an access code and a session
secret. Serving creates `conversations.sqlite` beside them. Keep this folder
private and out of source control; it contains conversation text. The repository
already ignores `.agentlib`.

Repeat `--agent` and `--allow-url` for more agents. All selected agents must use
the same **static tenant, subject and roles** as the UI identity. Each UI process
represents that one local developer. Agents still enforce their own policies.
Both UI and agents use loopback addresses in this mode. Enterprise SSO and
shared multi-user hosting belong to a later phase.

`check` validates settings, registry references, versions and addresses. It does
not start services, call a model, or claim that an agent is ready to answer.

## 4. Sign in and chat

```bat
type .agentlib\ui\access-code
python tools\package_runtime.py ui -m ai_agent_lib_ui serve
```

Or on POSIX:

```bash
cat .agentlib/ui/access-code
python3 tools/package_runtime.py ui -m ai_agent_lib_ui serve
```

Copy the displayed code and open [the local UI](http://127.0.0.1:8765). Enter the
code to sign in. Use the exact `127.0.0.1` address. Keep the server window open.

1. Select **Choose agent**, then **Start chat with rmprep**. The chat identifies
   the selected agent. This works even when only one agent is configured.
2. With a configured business model, ask: **memo: Prepare a memo for JONE-0022 as
   of 2026-08-03.** Follow your team's data-sharing rules for the selected model.
3. In the same conversation, ask: **qna: Explain the covenant findings and their
   sources.** Only this new question is sent, with the same thread ID.
4. Select **Memos** to view the saved memo. Generate another memo in the same chat,
   then switch between versions with the **Memo version** selector.
5. With Phoenix enabled, select **Open trace in Agent Lab** below the response
   to inspect model input/output, MCP calls, metrics and checkpoint references.
   **Logs** provides local diagnostics and saved state. **Logs → Manage chat
   history** provides rename, search, archive and restore.
6. Stop with **Ctrl+C**, run `serve` again, and reopen your conversation. The
   completed messages and selected agent remain.

**Rate an answer.** Thumbs up and thumbs down appear beneath each accepted
answer. A click, with an optional comment, appends one `agentlib.feedback/v1` line
to `feedback.jsonl` in the UI state folder, beside `conversations.sqlite`. The line
records the request, thread, agent, who rated it and the verdict (`up` or `down`).
Feedback is evidence, so the log is append-only: Agent Lab refuses to delete a
rating, and a second click on the same answer adds a line rather than editing the
first. Only accepted answers can be rated. The file holds your comments; keep it
private, like the conversation database. [Online evals from saved
chats](#online-evals-from-saved-chats) joins these verdicts to conversations.

A conversation keeps its original agent. Start a new conversation to change
agents. **Choose agent** also lists MCP services referenced by your selected agents;
MCP execution stays inside the agent service.

Reopening a long conversation loads the latest 50 turns, or fewer when the page
reaches 2 MiB. One large turn is always included so it remains accessible. Use
**View older messages** to browse earlier pages. This changes only UI loading;
the agent retains its checkpoints and applies its own model context budget.

## Responses and inspection

Chat with registered HTTP agents and keep your conversation history. Agent Lab
provides agent selection, saved threads, rename, title search, archive, restore,
and resume. The UI sends each new question to the selected agent; the agent
manages its own conversation checkpoints.

A new chat shows a short introduction below the Agent Lab logo and above the
message box. It names the selected agent and links to **Choose agent**. The
introduction disappears once you send a question and is not part of the saved
conversation or model input.

Chat, Choose agent, Logs, the turn inspector, and Memos share the same colors,
controls, and light/dark preference. Use the theme control in the page header.
Content wraps to fit the window; long documents and diagnostic records scroll
only when they need more room.

Replies display a live draft as approved model text becomes available. When the agent finishes its
checks and saves the outcome, the temporary draft panel disappears and the final
answer or failure message appears.
The draft label shows the model call number so tool steps and revisions are visible.
Each response shows input tokens, output tokens, elapsed time, the cache-hit
ratio, and cache read/write counts when reported. These metrics remain visible
when you reopen a conversation. A warming request can write the cache with 0% hits.

Chat shows the answer, usage, elapsed time and an **Open trace in Agent Lab** link
when tracing is enabled. The link opens the configured Phoenix trace. A trace link
the UI cannot validate, such as a link from a retired tracing provider in an older
agent or saved conversation, is dropped; the answer and metrics are unaffected.
Comparison tables show values without
repeating long references in each cell. Full source references remain in the saved
answer and memo documents for review. Model/tool details are available through
the trace link and Logs page.

Use the per-response trace link to inspect model input/output, MCP tool arguments
and results, timing, usage and checkpoint references. Enable content capture for
approved test data to see messages and tool results. See the
[Phoenix setup](../../docs/phoenix-trial.md).
Agent Lab uses a transparent grey logo in light mode and a soft white logo in dark
mode so the artwork stays in the background.

The **Logs** page and authorized diagnostic endpoints remain available for local
file-based diagnostics and saved checkpoint state. They load only when opened;
inspection never reruns the agent or calls an MCP tool.

The local rmprep service supplies the diagnostic reader. Set
`AC2_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl` in the agent's `.env`, then
restart the agent to capture model exchanges for new turns. For the batch stack,
run `stop-local.bat`, then `start-local.bat` (`./stop-local.sh`, then `./start-local.sh`
on POSIX).
Tracing captures conversation content; use the same private storage and retention
rules as the agent's checkpoints. The inspector reads the agent's configured trace
file, so retain that file to inspect earlier turns. Capture is not retroactive.
Each section has a 64 KiB preview with an explicit truncation notice. Missing,
disabled and unsupported capture are labelled; they do not mean no calls occurred.
The local file reader scans up to 64 MiB per configured file and 200 matching records.

The UI verifies ownership and current registry bindings before reading diagnostics.
Set `"inspector_enabled": false` in its configuration to disable diagnostic access
while retaining chat. The agent separately authorizes `diagnostic.read`; the rmprep
example grants it to its analyst and manager roles. Other agents can inject a
`TurnInspector` into `agent_app(inspector=...)`. Without one, chat works and the
inspector reports unsupported. Deployments need their own authorized reader;
the UI never opens an agent checkpoint database.

## Memos beside chat

Select **Memos** in the chat header to open the optional right panel. Choose a
**Memo version** to read it alongside the conversation. The panel starts closed;
use the close button or Escape to return to full-width chat. On small screens it overlays
chat. Long tables and text scroll inside the panel.

Accepted memo versions are saved with their originating turn and survive a UI
restart. Q&A replies keep the selected memo intact. New versions appear in the
selector while the panel is open; **Refresh versions** checks immediately, and
**Older versions** loads earlier entries. Viewing a version does not change the
agent's active checkpoint context. To ask about an older memo, include its memo
reference or client and date in your question. Expand **Memo details** to find
the memo reference and its originating request.

rmprep publishes newly accepted memos as optional `output.artifacts`, alongside
the unchanged `output.answer`. Enable this capability at UI setup with
`--artifacts-agent rmprep`. For an existing configuration, add `"artifacts"` to
that binding's `manifest.capabilities` array, then restart the UI. Existing chats
remain usable. The batch setup enables it when creating a new configuration.

The local panel supports inline Markdown and plain text, limited by the manifest
and HTTP response size. Images and raw HTML are not rendered. An existing version
cannot be overwritten; conflicting declarations keep the first saved version.
Opaque content references need a future storage reader. Older text-only replies
remain in chat; generate a new memo to publish a panel version.

If an agent returns an invalid document, Agent Lab keeps the accepted answer and
shows a document warning. The warning survives refreshes and restarts; check the
agent logs to fix the artifact before requesting another memo.

## Live responses and metrics

Restart the agent and UI after updating this checkout. The launcher and new
CLI-generated agents enable live responses automatically. For an existing agent,
add `live_updates=True` to its `agent_app(...)` call in `service.py`.

The UI requests a live response from `POST /invoke`. Agents that support it return
draft events followed by one accepted result. Older agents return their usual
JSON answer using the same request; the UI does not resend a question to switch
transports. Providers that do not support token streaming deliver a complete draft.

The expandable draft can change during a tool loop or business-validation revision.
The default pattern guardrails check the growing text against the output-size
limit before releasing each chunk. PII findings are recorded when the full reply
arrives. Blocking PII checks, guardrail providers without incremental support,
and structured-output schemas buffer text until their complete-response checks pass. Drafts contain public response text; reasoning blocks
and tool arguments are excluded. Only the final accepted answer is saved in chat
history. Keep `live_updates=False` if application-specific checks must finish
before any draft is shown.

Token totals cover every governed model attempt in that response, including
revisions and tool selection. Input counts include cache reads/writes once;
cache hit is cache-read tokens divided by input tokens. Time measures the UI
server's complete agent request, including model and tool work. Missing usage is
shown as **unavailable**, and fake-model counts are marked **simulated**. Existing
saved conversations retain the metrics captured when they ran; missing old counts
are not reconstructed from transcript text.

## Interrupted requests

The UI saves each question and idempotency key **before** calling the agent. Only
one request may be active in a thread. Duplicate sends with the same key and
question return the original outcome. A browser disconnect or Stop action stops
waiting in that browser; the backend continues admitted work. Reopen the thread
to see its saved result.

A confirmed agent validation failure is saved as **failed**, with reported token
usage and elapsed time. Its provisional drafts do not become accepted answers.
You can send a new question in the same thread; the original request is not resent.

A timeout, malformed response, server/gateway failure, failed result commit, or
server interruption can leave the outcome **unknown**. The agent may already have
run tools. Reopen the thread to check the agent's saved outcome. The UI makes a
read-only status request; it never resends the question. A matching confirmed
answer or failure is saved with its metrics, and the thread can accept new
questions. The UI database records the reconciliation in the same transaction.
If the agent has no confirmed outcome, the thread stays held.

The local rmprep launcher and newly generated local agents keep a journal in
`<agent folder>\.agentlib\outcomes.sqlite`. Restart updated services to enable
it. Requests made before the journal was enabled cannot be recovered through it.
Treat this file like the checkpoint database: it contains accepted answers.
Other deployments can inject an `OutcomeJournal` into `agent_app`; a shared
journal is needed when requests can reach multiple service instances.

Normal shutdown waits for active calls within the request deadline. After a forced
stop, requests that reached dispatch become unknown on restart. Requests saved
before dispatch are marked failed and release the thread without being resent.
One process may own a state
folder at a time. This admission guard does not coordinate direct agent API callers
or a different UI state folder.

## Online evals from saved chats

Saved conversations and viewer feedback can become an eval set without calling any
agent. `export_interactions`, in `ai_agent_lib_ui.adapters.interactions`, writes
every completed exchange the identity owns, active and archived conversations
alike, as `agentlib.interaction/v1` JSONL. Given `feedback.jsonl`, each exchange
carries its latest verdict, matched by request ID. It reads only the UI's existing
state, so run it after real traffic has been saved.

It is a Python entry point, not yet a command. The export holds conversation
content, so write it under the UI state folder with the rest of the private state.
The worked script is in [online evals: capture](../../docs/evals.md#4-online-capture);
sampling and scoring the export follow in
[online evals: scoring](../../docs/evals.md#5-online-scoring).

## Configuration and common fixes

Settings live in `.agentlib\ui\config.json`. Paths can be absolute or relative
to that file. Stop the UI before editing settings or refreshing pins.

| Setting | Purpose and local default |
| --- | --- |
| `agents_path`, `tools_path` | Existing agent and MCP registry files. |
| `identity.subject`, `identity.tenant`, `identity.roles` | Static identity copied from the agent `.env`; must match all selected agents. |
| `bindings` | Approved agent manifests and URLs. Each run records the current registry revision. |
| `allowed_urls` | Exact loopback agent base URLs approved during initialization. |
| `port` | UI port, default `8765`; initialize with `--port` to change it. |
| `timeout_seconds` | Total HTTP deadline, default `180`, maximum `600`. A timeout means unknown. |
| `schema_version` | Configuration version, `agentlib.ui-local/v1`. |

Use `--config path\to\config.json` on each command for another local state folder.
The server always binds to `127.0.0.1`.

| Symptom | What to do |
| --- | --- |
| Agent has no URL | Add its running loopback address to the registry entry, then initialize. |
| `destination_denied` | Match the registry URL and `--allow-url`, including port and path. |
| `stale_binding` | Review the registry change. Stop the UI and repeat `init` with `--overwrite`, then run `check`. Unrelated registry revisions are accepted automatically. Start a new thread after an actual agent version, URL or authority change. |
| `not_found` | Check the allowed, active agent ID and your local identity/state folder. |
| `conflict` | Reopen the thread. It may have an active/unknown request or be archived. |
| Another server owns the folder | Stop that server; keep its lock in place while it runs. |
| Sign-in expires | Reload the page and sign in again. The login form returns you to the conversation or page you opened. Sessions last one hour; signing out revokes the current session. |
| UI rejects `LITERAL_API_KEY` | Keep model/content-capture credentials in the agent environment. The UI does not need them. |

`--overwrite` replaces configuration pins, keeping the database and secrets.
Changing identity makes old ownership scopes inaccessible to the new identity.

## How the code fits together

| Location | Responsibility |
| --- | --- |
| `contracts/` | Values and interfaces for catalog, conversations, artifacts and diagnostics. |
| `contracts/traces.py` | The private Phoenix `TraceLink`, and `tolerant_trace`, which drops a link that no longer validates instead of failing its answer. |
| `contracts/feedback.py` | The `FeedbackLog` port that appends a viewer's judgement of an answer. |
| `application/chat.py` | Admit requests and own their lifecycle independently of the browser. |
| `application/question_answer.py` | Validate the existing question/answer HTTP format. |
| `adapters/catalog.py` | Reuse core's public registry parser, authorize selection and check pins. |
| `adapters/http.py` | Bound request time/response size; disable redirects and automatic retries. |
| `adapters/events.py` | Parse the bounded server-sent event stream of live drafts and the terminal result. |
| `adapters/sqlite.py`, `migrations/` | Store threads, admitted turns, accepted answers and sessions. |
| `adapters/documents.py` | Read the immutable document versions saved with accepted answers. |
| `adapters/inspection.py` | Fetch bounded turn diagnostics from a registry-approved agent. |
| `adapters/interactions.py` | `export_interactions`: join saved conversations and feedback into online-eval records. |
| `config/` | Translate explicit local settings and validate loopback destinations. |
| `integrations/`, `assets/` | Assemble adapters and connect FastAPI/Chainlit to the application. |

The application depends on interfaces. The composition root (`integrations/runtime.py`)
injects a catalog, repository and dispatcher, and `compose()` returns a `Runtime`
that the session boundary, data layer and route factories receive as an argument.
Only `integrations/callbacks.py`, which Chainlit executes by file path, reads the
`Runtime` that `create_app` publishes for it. Tests substitute scripted HTTP
endpoints and temporary SQLite files, and run the parent routes, the session
boundary and the data layer in process. The UI never imports an agent graph or
reads its checkpoint database.
The catalog uses `ai_agent_lib_core.kit` for the shared parser; contracts and the
application stay independent of Chainlit and agent framework imports.

CI enforces that independence with import-linter contracts in `pyproject.toml`:
`contracts` imports nothing else from the package, `application` imports no
adapters, integrations, Chainlit, FastAPI or Starlette, and only `integrations`
imports Chainlit. After changing imports, run `lint-imports` from this directory
(the `dev` extra installs it).

Agent profiles share the same chat appearance and shell settings. The pinned
Chainlit release needs a small session-configuration adapter to read those
settings without trying to run an asynchronous profile lookup inside the active
WebSocket loop. Profile listing still uses the authenticated registry catalog;
per-profile shell overrides are not supported. Socket regression tests cover
this adapter when upgrading Chainlit.

## Run the checks and build a wheel

From the repository root, install the UI development tools, then run:

```bat
python tools\package_runtime.py ui --install -e packages\ai-agent-lib-core -e "packages\ai-agent-lib-ui[chat,dev]"
cd packages\ai-agent-lib-ui
python ..\..\tools\package_runtime.py ui -m pytest
python ..\..\tools\package_runtime.py ui -m mypy
python ..\..\tools\package_runtime.py ui -m ruff check .
python ..\..\tools\package_runtime.py ui -m ruff format --check .
python ..\..\tools\package_runtime.py ui --check
python -m pip wheel --no-deps --wheel-dir dist .
cd ..\..
```

Or on POSIX:

```bash
python3 tools/package_runtime.py ui --install -e packages/ai-agent-lib-core -e "packages/ai-agent-lib-ui[chat,dev]"
cd packages/ai-agent-lib-ui
python3 ../../tools/package_runtime.py ui -m pytest
python3 ../../tools/package_runtime.py ui -m mypy
python3 ../../tools/package_runtime.py ui -m ruff check .
python3 ../../tools/package_runtime.py ui -m ruff format --check .
python3 ../../tools/package_runtime.py ui --check
python3 -m pip wheel --no-deps --wheel-dir dist .
cd ../..
```

Tests use synthetic local agents. They cover real Socket.IO chat, same-thread
follow-ups, restart/reconnect, ownership, duplicate/concurrent sends, registry
changes, failed writes and unknown outcomes. The wheel check repeats real-server
tests using installed packages in CI. On an approved CI runner,
`python tools\check_consumer.py --profile ui-chat` creates a disposable test
environment for this check. `--profile ui` checks the lightweight contracts
installation. These CI checks are optional on restricted desktops.

When Node is already available, pytest also runs a small browser-script regression
for rapid memo-version switching. This optional check uses Node's built-in tools;
it skips on Python-only desktops and needs no npm packages.

For changes to the chat layout, also check the rendered page in Chrome. Open
**New chat**, then reopen a saved empty thread. Repeat with the sidebar and Memos
panel open and closed at mobile and desktop widths. Run the read-only
[browser layout check](tests/browser_layout.js) in the page's developer console;
it throws if a welcome-screen ancestor has a horizontal scrollbar or a chat
control extends past the viewport. This checks the nested chat scrollers as well
as the page itself. Confirm that a wide response table or code block still scrolls
inside its own container. Restart the UI and reload the tab after changing assets.

The UI has its own Windows/Linux CI job. Browser and enterprise release gates from
the [Phase 0 review](../../docs/frontend-phase0-review.md) (the framework-fit
evidence that preceded this package) remain tracked separately; this local
preview does not imply approval for shared production hosting.

## Local Phoenix tracing

The [Phoenix trial](../../docs/phoenix-trial.md) uses the same Python, a separate package folder, and SQLite.
Run `start-phoenix.bat setup` (`./start-phoenix.sh setup` on POSIX) once from the
library repository root, then follow the guide to enable Phoenix in the root `.env`.
`start-local.bat` (`./start-local.sh`) starts the local Phoenix server before rmprep
and Agent Lab, using `.agentlib\phoenix\.venv\Lib\site-packages`
(`.agentlib/phoenix/.venv/lib/python3.x/site-packages` on POSIX).
`stop-local.bat` (`./stop-local.sh`) in another terminal stops the stack and keeps saved
conversations, traces, and checkpoints. **Ctrl+C** in the launcher window also works.
Each response links to its Phoenix trace using **Open trace in Agent Lab**.
The **Logs** page provides local diagnostic inspection.

## Chat with an agent on AgentCore Runtime

An agent deployed with `agentlib deploy --target agentcore` is reached through
the AgentCore service endpoint instead of a loopback URL. Pin it at `init` with
its runtime ARN; the registry entry needs no `url` for such an agent. Run the
first command as one line:

```bat
python tools\package_runtime.py ui -m ai_agent_lib_ui init --env-file examples\rmprep\agents\rmprep\.env --agent rmprep --artifacts-agent rmprep --agentcore-runtime rmprep=arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/rmprep-abc123
python tools\package_runtime.py ui -m ai_agent_lib_ui check
```

Repeat `--agentcore-runtime AGENT=ARN` for more hosted agents; each must also be
selected with `--agent`. The invocation URL is derived from the ARN and recorded
in `allowed_urls`, so pinning the ARN is the approval of that one HTTPS address.
Loopback agents and hosted agents can share a configuration.

Two authentication modes apply to every runtime binding of a configuration:

- `--agentcore-auth iam` (default) signs each request with SigV4 using the
  desktop's AWS credentials, resolved by boto3 at each request (environment,
  profile or SSO session). Install the `aws` extra:
  `python tools\package_runtime.py ui --install -e "packages\ai-agent-lib-ui[chat,aws]"`.
- `--agentcore-auth bearer --agentcore-token-file PATH` sends
  `Authorization: Bearer` with the first line of that file, for a runtime with a
  JWT authorizer. The UI reads the file for every request and never copies the
  token into settings or logs; refresh the file with your identity provider's
  tooling and the next question uses the new token.

What is different from a loopback agent:

- The conversation's thread id travels as the runtime session id
  (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`), and the agent sees that
  session id as its thread id. Agent Lab's thread ids are UUIDs, which satisfy
  the platform's 33-character rule as they are. A shorter id, as some tests
  use, is extended deterministically with 32 hex digits of its SHA-256.
- The runtime proxies only `/invocations`, so an **unknown** outcome cannot be
  reconciled through the agent's outcome journal; the thread stays held. A
  `RetryableConflictException` is retried three times (0.5 s, 1 s, 2 s) before
  the outcome is left unknown; throttling and service faults are unknown at once.
- A `RuntimeClientError` (HTTP 424) means the agent's container returned an
  error. The run is saved as **failed** and is never replayed; when the agent's
  own refusal envelope survives the proxy, its code and metrics are kept.
  `UnauthorizedException` and `AccessDeniedException` are terminal failures too.
- Each completed or failed run links to **Open trace in CloudWatch**: the GenAI
  Observability view of the runtime's region. The console publishes no
  per-session address, so open the agent's sessions there and find the session
  by its id, which is the conversation's thread id.

The UI still binds to `127.0.0.1` with the same static local identity; the
runtime's own authorizer decides who the hosted agent serves.

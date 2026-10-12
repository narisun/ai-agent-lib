# ai-agent-lib-ui

Chat with registered HTTP agents and keep your conversation history. Phase 2
provides agent selection, saved threads, rename, title search, archive, restore,
and resume. The UI sends each new question to the selected agent; the agent
manages its own conversation checkpoints.

This is a local development preview in this checkout. Document panels, token/cache
metrics and the diagnostic inspector follow in later phases. The existing agent
HTTP API and Phase 1 contracts remain compatible.

## 1. Prepare a separate UI environment

Use company-provided **Python 3.11 or newer**, pip configured for JFrog or your
approved package index, and a browser. Open **Command Prompt** at the repository root:

```bat
python -m venv packages\ai-agent-lib-ui\.venv
packages\ai-agent-lib-ui\.venv\Scripts\activate.bat
python -m pip install -e packages\ai-agent-lib-core -e "packages\ai-agent-lib-ui[chat,dev]"
python -m pip check
```

The `chat` extra installs Chainlit 2.12.0 and the local server dependencies;
`dev` adds the check tools. Keep this environment separate from agent and MCP
environments: Chainlit requires MCP 1.x, while the library's MCP extra requires
MCP 2.x. They communicate over HTTP. `python tools\install_dev.py` continues to
install only the agent development environment.

Pip uses your existing enterprise index and certificate configuration. If your
desktop requires wheels, ask your platform team to provide approved wheels.
The Phase 0 installation needed Python-built wheels for `literalai==0.1.201`,
`syncer==2.0.3`, and `cuid==0.4`. See the
[wheel preparation example](../../experiments/chainlit-phase0/README.md#2-install-the-pinned-packages).
Use your approved package source throughout.

For wheels supplied by your team, create and activate a separate UI environment,
then install the supplied core and UI wheels with the `chat` extra:

```bat
python -m pip install "C:\approved-wheels\ai_agent_lib_core-0.1.0-py3-none-any.whl" "C:\approved-wheels\ai_agent_lib_ui-0.1.0-py3-none-any.whl[chat]"
```

Replace `C:\approved-wheels` with your team's location. This preview has not been
published as a release.

## 2. Start your agent and register its address

Start the agent and its MCP services in their own Command Prompt windows and
environments. The agent must accept `POST /invoke` with `input.question` and
return `output.answer`, `thread_id`, and `request_id`.

For [rmprep](../../examples/rmprep/README.md), complete its Python/pip setup first.
From `examples\rmprep`, use its environment to start the MCP service:

```bat
.venv\Scripts\activate.bat
python -m rmprep_data
```

In another Command Prompt at `examples\rmprep`, start the HTTP agent:

```bat
.venv\Scripts\activate.bat
python -m rmprep.service --host 127.0.0.1 --port 8000
```

Add this property to the existing `rmprep` entry in
`examples\rmprep\registry\agents.yaml`, at the same indentation as `owner`:

```yaml
    url: http://127.0.0.1:8000
```

Keep its other properties. The registry identifies the service; the UI's explicit
URL allowlist determines which addresses it may call. The default fake model
checks connectivity; follow the rmprep walkthrough to configure a model for
business answers.

## 3. Initialize the UI

Return to the Command Prompt with the **UI environment** active, at the repository
root. Run the first command as one line:

```bat
python -m ai_agent_lib_ui init --env-file examples\rmprep\agents\rmprep\.env --agent rmprep --allow-url http://127.0.0.1:8000
python -m ai_agent_lib_ui check
```

`init` reads `EAP_REGISTRY_OPTIONS` and `EAP_IDENTITY_OPTIONS` from that agent's
`.env`. Relative registry paths resolve from the file's directory. This phase
uses `EAP_PROFILE=local`, the `file` registry and `static` identity providers. Model API
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
python -m ai_agent_lib_ui serve
```

Copy the displayed code and open [the local UI](http://127.0.0.1:8765). Enter the
code to sign in. Use the exact `127.0.0.1` address. Keep the server window open.

1. Select **rmprep** in the agent menu and start a conversation.
2. With a configured business model, ask: **memo: Prepare a memo for JONE-0022 as
   of 2026-08-03.** Follow your team's data-sharing rules for the selected model.
3. In the same conversation, ask: **qna: Explain the covenant findings and their
   sources.** Only this new question is sent, with the same thread ID.
4. Refresh and open the saved conversation from the left sidebar.
5. Open **Conversations** at the top to rename, search titles, archive or restore.
   Chainlit's separate Delete action is disabled; use Archive here.
6. Stop with **Ctrl+C**, run `serve` again, and reopen your conversation. The
   completed messages and selected agent remain.

A conversation keeps its original agent. Start a new conversation to change
agents. **Catalog** also lists MCP services referenced by your selected agents;
MCP execution stays inside the agent service.

## Interrupted requests

The UI saves each question and idempotency key **before** calling the agent. Only
one request may be active in a thread. Duplicate sends with the same key and
question return the original outcome. A browser disconnect or Stop action stops
waiting in that browser; the backend continues admitted work. Reopen the thread
to see its saved result.

A timeout, invalid response, server/gateway failure, failed result commit, or
server interruption can leave the outcome **unknown**. The agent may already have
run tools. The UI holds that thread and never resends automatically. Inspect the
agent logs and start a new conversation when appropriate. Automatic reconciliation
needs the agent outcome journal planned for Phase 4.

Normal shutdown waits for active calls within the request deadline. After a forced
stop, unfinished records become unknown on restart. One process may own a state
folder at a time. This admission guard does not coordinate direct agent API callers
or a different UI state folder.

## Configuration and common fixes

Settings live in `.agentlib\ui\config.json`. Paths can be absolute or relative
to that file. Stop the UI before editing settings or refreshing pins.

| Setting | Purpose and local default |
| --- | --- |
| `agents_path`, `tools_path` | Existing agent and MCP registry files. |
| `identity.subject`, `identity.tenant`, `identity.roles` | Static identity copied from the agent `.env`; must match all selected agents. |
| `bindings` | Generated manifests pinned to registry revision, agent version and URL. |
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
| `stale_binding` | Review the registry change. Stop the UI and repeat `init` with `--overwrite`, then run `check`. Old threads retain their pins; start a new thread after a binding change. |
| `not_found` | Check the allowed, active agent ID and your local identity/state folder. |
| `conflict` | Reopen the thread. It may have an active/unknown request or be archived. |
| Another server owns the folder | Stop that server; keep its lock in place while it runs. |
| Sign-in expires | Sign in again. Sessions last one hour; signing out revokes the current session. |
| UI rejects `LITERAL_API_KEY` | Keep model/content-capture credentials in the agent environment. The UI does not need them. |

`--overwrite` replaces configuration pins, keeping the database and secrets.
Changing identity makes old ownership scopes inaccessible to the new identity.

## How the code fits together

| Location | Responsibility |
| --- | --- |
| `contracts/` | Values and interfaces for catalog, conversations, artifacts and diagnostics. |
| `application/chat.py` | Admit requests and own their lifecycle independently of the browser. |
| `application/question_answer.py` | Validate the existing question/answer HTTP format. |
| `adapters/catalog.py` | Reuse core's public registry parser, authorize selection and check pins. |
| `adapters/http.py` | Bound request time/response size; disable redirects and automatic retries. |
| `adapters/sqlite.py`, `migrations/` | Store threads, admitted turns, accepted answers and sessions. |
| `config/` | Translate explicit local settings and validate loopback destinations. |
| `integrations/`, `assets/` | Assemble adapters and connect FastAPI/Chainlit to the application. |

The application depends on interfaces. The composition root injects a catalog,
repository and dispatcher. Tests substitute scripted HTTP endpoints and temporary
SQLite files. The UI never imports an agent graph or reads its checkpoint database.
The catalog uses `ai_agent_lib_core.kit` for the shared parser; contracts and the
application stay independent of Chainlit and agent framework imports.

## Run the checks and build a wheel

With the UI environment active, from the repository root:

```bat
cd packages\ai-agent-lib-ui
python -m pytest
python -m mypy
python -m ruff check .
python -m ruff format --check .
python -m pip check
python -m pip wheel --no-deps --wheel-dir dist .
cd ..\..
python tools\check_consumer.py --profile ui-chat
```

Tests use synthetic local agents. They cover real Socket.IO chat, same-thread
follow-ups, restart/reconnect, ownership, duplicate/concurrent sends, registry
changes, failed writes and unknown outcomes. The wheel check repeats real-server
tests using installed packages. `--profile ui` separately checks the lightweight
contracts installation without the `chat` extra.

The UI has its own Windows/Linux CI job. Browser and enterprise release gates from
the [Phase 0 review](../../docs/frontend-phase0-review.md) remain tracked separately;
this local preview does not imply approval for shared production hosting.

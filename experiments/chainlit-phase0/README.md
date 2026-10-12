# Chainlit Phase 0: try the agent interface

This experiment checks whether Chainlit can provide the planned chat interface.
It includes agent selection, saved conversations, a memo panel, response metrics,
and a separate inspector. Read the [decision and evidence](../../docs/frontend-phase0-review.md)
for the results and remaining release gates.

The agent uses **synthetic DEMO-001 data**. It makes a real local HTTP request and
stores conversations in SQLite. Model exchanges, MCP results, checkpoints, and
token counts are scripted fixtures. Response time is measured. This example does
not read the tutorial's `.env` or send business data to a model provider.

## 1. Prepare Python

Use company-provided **64-bit Python 3.11**, pip configured for your enterprise
package repository, and your existing browser. Open **Command Prompt** in this
repository, then run:

```bat
cd experiments\chainlit-phase0
python -m venv .venv
set "PYTHON=%CD%\.venv\Scripts\python.exe"
```

Keep this environment separate from agent and MCP environments. Chainlit 2.12.0
requires MCP 1.x; this library's MCP extra requires MCP 2.x. The UI communicates
with agents over HTTP, so those packages do not need to share an environment.

## 2. Install the pinned packages

If your enterprise repository provides all the pinned wheels:

```bat
"%PYTHON%" -m pip install --only-binary=:all: -r requirements-checks.lock
"%PYTHON%" -m pip check
```

The check environment includes pytest and a Socket.IO test client. For running
the sample alone, use `requirements.lock` instead.

On the index used for this trial, `literalai==0.1.201`, `syncer==2.0.3`, and
`cuid==0.4` needed wheel preparation. Pip built them using Python. Your platform
team can publish those wheels to JFrog. If your enterprise repository permits
building these packages locally, these Command Prompt commands prepare a local
wheel folder and then install entirely from that folder:

```bat
"%PYTHON%" -m pip wheel --wheel-dir .agentlib\wheelhouse -r requirements-checks.lock
"%PYTHON%" -m pip install --no-index --find-links .agentlib\wheelhouse --only-binary=:all: -r requirements-checks.lock
"%PYTHON%" -m pip check
```

If pip reports a missing version or wheel, give the platform team the lock files
and [dependency inventory](evidence/dependencies.json). Use your company's package
source and certificate configuration. The recorded locks target Windows/Python
3.11; resolve and test a separate lock for a different platform.

## 3. Start and sign in

```bat
"%PYTHON%" -m spike
```

Keep this window open. In a second Command Prompt, open this experiment's folder
and display its generated local access code:

```bat
type .agentlib\access-code.txt
```

Open [the local sign-in page](http://127.0.0.1:8765/login). Choose **Alice (demo)**,
enter the code, and select **Open chat**. Use the exact `127.0.0.1` address; the
sample binds its session checks to that host and port.

These identities exercise scope checks; they are not enterprise SSO accounts.
The local code, session keys, and SQLite database stay in the ignored `.agentlib`
folder. Close the sample with **Ctrl+C** when finished.

## 4. Try the two-question walkthrough

1. Select **rmprep-demo** in the agent menu. Both menu entries come from
   [registry.json](registry.json), using the library's registry document shape.
2. Send: **Prepare the DEMO-001 meeting memo.**
3. Check the memo panel and response metrics: **12,000 tokens**, **60% prompt
   cache hit**, and roughly **2 seconds**. Token/cache values are fixtures;
   elapsed time measures the local HTTP operation. Cache ratio is
   `6,000 cached input tokens / 10,000 total input tokens`.
4. In the same conversation, send: **Explain how the gross margin was calculated.**
   The answer should state **$3 million / $12 million = 25%** and identify it as
   follow-up 1 on the same thread.
5. Select **Inspect run** below that answer. In **Model input / output**, find the
   earlier question and answer in `input`, then the `tool` message in the second
   model call. **Checkpoints** shows the saved conversation before and after the
   turn. **MCP evidence** and **Logs** show the matching tool/request identifiers.
6. Use **Return to chat** to return to the thread. Reload the page, or stop and
   restart the sample. Select the conversation in the left navigation. Both
   questions, answers, metrics, and memo links should remain available.

Each memo link has a short run suffix so selecting it opens that document.
After restoring history, Chainlit can initially display saved memos together;
select a memo link to view just that version. On a narrow screen the memo
opens in a drawer. Close it to return to chat; on a tablet, collapse the history
navigation for more reading space. JSON and memo tables have their own scroll areas.

The [catalog page](http://127.0.0.1:8765/catalog) shows the registered fixture
agents. Agent selection applies to a new conversation. The automated checks also
cover Bob in the same tenant and Alice in another tenant.

## 5. Run the checks

Stop the manually running sample with **Ctrl+C** first. The checks start their own
server on port 8765 and use a disposable database under `.agentlib\check-state`.

```bat
"%PYTHON%" -m pytest -q --basetemp .agentlib\check-state
"%PYTHON%" -m pip check
```

The checks exercise real HTTP and WebSocket requests. They cover saved history,
same-thread follow-ups, memo/metrics restoration, duplicate requests, browser
disconnects, cancellation, process crashes, sign-out, and access isolation.

To repeat the visual checks, use your browser's responsive tools at widths 390,
768, and 1440 pixels. Test chat, memo open/close, every inspector tab, keyboard
scrolling, and history restoration. Also test at 200% browser zoom on the target
enterprise browser. The [report](../../docs/frontend-phase0-review.md) distinguishes
the checks completed here from this remaining manual zoom check.

## How the sample is arranged

| File | Responsibility |
| --- | --- |
| `spike/server.py` | Compose FastAPI, sign-in, content routes, and the mounted chat. |
| `spike/security.py` | Require the parent session on HTTP/WebSocket paths and bind chat identity to it. |
| `spike/callbacks.py` | Translate Chainlit events into application operations. |
| `spike/application.py` | Own admitted HTTP tasks and persist results independently of the browser. |
| `spike/store.py` | Keep one scoped SQLite store for threads, runs, messages, and elements. |
| `spike/data_layer.py` | Adapt Chainlit's documented data-layer interface to that store. |
| `spike/fixture.py` | Serve deterministic answers and simulated diagnostics over HTTP. |
| `public/elements/Metrics.jsx` | Render metrics through Chainlit's custom-element hook. |
| `views/inspector.html`, `public/inspector.js` | Show authorized run evidence using local browser assets. |

The store bounds history to 200 threads and 400 objects per thread for this
experiment. Rename/search come from Chainlit; deletion and feedback are disabled
in the adapter. A process crash marks an unfinished request **unknown** and blocks
new turns on that thread. The experiment provides no manual reconciliation UI.

Production work needs typed contracts, migrations, paginated storage, real
diagnostic adapters, Entra/authorization integration, retention rules, and AWS
deployment. Follow the [implementation plan](../../docs/frontend-implementation-plan.md).

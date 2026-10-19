# View rmqna traces in Phoenix

The generated HTTP service already wires optional tracing, so there is no code to
add. Your graph stays ordinary LangGraph. With tracing on, each accepted response
can link to its model calls, tool calls and checkpoint references in Phoenix, a
trace viewer that runs separately from the agent.

## 1. Pick a Phoenix server

- **On your machine, for one developer:** in the ai-agent-lib repository, run the
  `start-phoenix` launcher (`start-phoenix.bat` on Windows, `./start-phoenix.sh` on
  macOS and Linux): `setup` once, then `start`. It serves Phoenix at
  `http://127.0.0.1:6006` and keeps traces in a local SQLite file. The repository's
  `docs/phoenix-trial.md` walks through it.
- **Shared by a team:** ask the platform team for the collector's HTTPS URL and,
  if it requires one, an API key.

## 2. Turn tracing on

Add these settings to `agents/rmqna/.env`, or to a private file that
`AC2_PHOENIX_ENV_FILE` names, such as `F:\work\phoenix\agent.env`:

```dotenv
AC2_PHOENIX_ENABLED=true
PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006
PHOENIX_PROJECT_NAME=rmqna
AC2_PHOENIX_CAPTURE_CONTENT=false
```

Add `PHOENIX_API_KEY=your-api-key` only for a collector that needs one, and
only in a private file. Keep real keys out of source control.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AC2_PHOENIX_ENABLED` | Export traces. Nothing is exported until this is `true`. | `false` |
| `PHOENIX_COLLECTOR_ENDPOINT` | The collector's origin: HTTPS, or HTTP for loopback. | `http://127.0.0.1:6006` |
| `PHOENIX_PROJECT_NAME` | The Phoenix project that groups this agent's traces. | `agent-lab` |
| `PHOENIX_API_KEY` | Credential for a collector that requires one. Never shown, never sent to the browser. | none |
| `AC2_PHOENIX_CAPTURE_CONTENT` | Include model messages and tool arguments/results in traces. | `false` |
| `AC2_PHOENIX_ENV_FILE` | A separate private file with any of the settings above. | none |

Set `AC2_PHOENIX_CAPTURE_CONTENT=true` only with approved test data. With
`false`, traces contain metadata, token counts and checkpoint IDs, not conversation
content. The browser receives a private trace link, never an API key.

## 3. Start the agent

Open Command Prompt at the workspace root and select Python as described in the
[workspace README](../../../README.md#select-python). The generated dependencies
include the core `otel` extra; the installer uses Python and pip.

```bat
"%PYTHON%" -m ai_agent_lib_cli install
set "AC2_PHOENIX_ENV_FILE=F:\work\phoenix\agent.env"
"%PYTHON%" -m ai_agent_lib_cli config explain rmqna
"%PYTHON%" -m ai_agent_lib_cli doctor rmqna
"%PYTHON%" -m rmqna.service --port 8200
```

Skip the `set` line when the settings are in `agents/rmqna/.env`. Use your
file's actual path otherwise. Process variables override that file, which
overrides `agents/rmqna/.env`. Restart the agent after changing settings.
`config explain` identifies each setting's source and masks the API key.
`doctor` validates the same settings as startup; this check does not contact
Phoenix or export content.

The Phoenix server runs separately. No Phoenix server runs inside the agent or
Agent Lab process. Operational telemetry keeps its existing destination.

## 4. Inspect a conversation

Select **rmqna** in Agent Lab. Ask `My name is Ada. Please greet me.` and
then `What is my name?` in the same chat. Use a real model to exercise tool choices;
the default fake model echoes input, while the tests script its tool calls.

Select **Open trace in Agent Lab** beneath either answer; it opens the trace in
Phoenix. Each question has its own trace and request ID. The two traces share a session scoped to the caller, tenant,
agent and thread. After a tool runs, a later model input shows its result. A
follow-up's model input shows the saved conversation. Checkpoint IDs appear in
root metadata; the checkpoint database stays with the agent. Token and cache
counts remain visible in Agent Lab and survive a chat reload.

Tracing covers `POST /invoke`, including Agent Lab's live response mode. The
standalone CLI and `/invoke/stream` graph-step endpoint keep their existing
behavior. Export happens in the background; a link may appear before ingestion
finishes. An exporter outage does not retry a business request or discard its
answer. To stop exporting, set `AC2_PHOENIX_ENABLED=false` and restart.
Checkpoint reference collection has a one-second deadline and a 64-reference
limit. Missing or truncated references are marked in trace metadata; a slow
diagnostic lookup does not indefinitely delay the accepted answer.

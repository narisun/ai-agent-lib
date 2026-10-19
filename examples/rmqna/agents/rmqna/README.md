# rmqna

Greets people by name.

This LangGraph agent has a model node and a tool node. The library supplies
model and tool wrappers that apply permissions, limits, and audit recording,
along with conversation storage and request context.

Read this page in order to test and run the agent. For a real-model walkthrough,
see [Anthropic](docs/anthropic-example.md); for model and tool traces,
see [Phoenix tracing](docs/tracing.md). Those are optional after the local tests pass.

## Before you start

Run all commands from the **workspace root**, where `agentlib.toml` lives.
Follow [Select Python](../../README.md#select-python) in the workspace README
to set `PYTHON` to your approved Python executable. Do this in every
new terminal. These examples use Windows Command Prompt; in bash/zsh replace `"%PYTHON%"`
with `"$PYTHON"` after setting `PYTHON` as that README describes.

Install the workspace once, and again after changing dependencies:

```bat
"%PYTHON%" -m ai_agent_lib_cli install
```

## Test it

```bat
"%PYTHON%" -m pytest agents/rmqna
```

**Expected result:** the tests pass using scripted models and local fixtures.
Choose a test for the behavior you want to change:

| To change | Start in this service folder |
| --- | --- |
| What a tool does | `tests/test_rmqna.py`, the function tests |
| How the agent behaves | `tests/test_rmqna.py`, the scripted model tests |
| What the rules allow | `tests/test_rmqna_as_configured.py` |
| HTTP requests and streaming | `tests/test_rmqna_service.py` |
| Answer quality with a real model | `evals/cases.jsonl` and `tests/test_rmqna_eval.py` |

## Run it, step by step

### 1. Prepare the model settings

Edit `agents/rmqna/.env` from the workspace root.
This service selects Anthropic. Set `AC2_MODEL_ID` and
`ANTHROPIC_API_KEY` before running it. Keep the key in `.env` or your approved
credential mechanism; keep credentials out of `.env.example` and source control.
Running the agent calls the real model and can incur charges. The tests use
scripted replies.

### 2. Start the required MCP servers

Start each server below in its **own terminal**, from the workspace root.
Select Python in each terminal first. Leave these terminals running and wait
for each server's `ready` log event before proceeding.

```bat
"%PYTHON%" -m ai_agent_lib_cli run rmqna-data
```

If a server fails to start, open its README under `mcp-servers/` and run
`"%PYTHON%" -m ai_agent_lib_cli doctor` followed by that server's name.

### 3. Start the agent's HTTP server

Open another terminal at the workspace root and select Python again:

```bat
"%PYTHON%" -m rmqna.service --port 8200
```

Leave it running. Wait for the `ready` log event. The server listens at
`http://127.0.0.1:8200`. `/healthz` reports liveness; `/readyz` becomes
successful after startup validation and graph/tool preparation finish.

The generated service enables live replies for the library's chat UI. Model text
appears in a provisional draft while the agent works. The draft panel disappears when
the final answer is saved; each reply shows input/output token usage and elapsed
time. Missing counts display as unavailable; fake-model counts are simulated.
For per-response model/tool timelines and checkpoint references, follow
[Connect Phoenix](docs/tracing.md). The generated service includes the shared
library adapter; tracing is disabled until you configure it.
The UI requests `POST /invoke` with `Accept: text/event-stream`. Regular JSON
requests keep their accepted-answer envelope. Default pattern guardrails check
each growing update; blocking PII and schema checks wait for the complete response.
To also wait for all application-specific
checks before showing any draft, set `live_updates=False` in
`src/rmqna/service.py`.

### 4. Send a request from another terminal

Save this as `ask_agent.py` in the workspace root. It uses Python's standard
library. Set `stream = True` to receive an update as each graph step completes.
Leave it `False` to receive the final response.

```python
import json
from urllib.request import Request, urlopen

stream = False
route = "/invoke/stream" if stream else "/invoke"
payload = {
    "input": {"question": "Say hello to Ada."},
    "thread_id": "demo",
}
request = Request(
    f"http://127.0.0.1:8200{route}",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urlopen(request, timeout=60) as response:
    if stream:
        for line in response:
            print(line.decode("utf-8"), end="")
    else:
        print(json.load(response))
```

In the request terminal, select Python again and run:

```bat
"%PYTHON%" ask_agent.py
```

**Expected result:** the response contains `output.answer`. With the fake model
its value is `fake: Say hello to Ada.`; a real model chooses its own wording.
Press Ctrl+C in each server terminal when you are finished.

### Ask a question from the command line

After preparing the model and starting any required MCP servers, you can
also ask one question directly. This command exits after printing the answer:

```bat
"%PYTHON%" -m rmqna "Say hello to Ada."
```

The local development identity supplies the caller when no token is sent.
Its roles are configured in `AC2_IDENTITY_OPTIONS`. Local audit logs and
conversation checkpoints are under `agents/rmqna/.agentlib/`.

## What to read

These paths are relative to this service folder:

| File | What it contains |
| --- | --- |
| `src/rmqna/tools.py` | Plain Python tool functions |
| `src/rmqna/graph.py` | Graph construction and governed components |
| `src/rmqna/__main__.py` | The command-line entry point |
| `src/rmqna/service.py` | The HTTP entry point and streaming |
| `.env.example` | Committed example settings used by tests |

## Add a tool

1. Write a failing function test in `tests/test_rmqna.py`.
2. Implement the function in `src/rmqna/tools.py` and add it to the list
   in `src/rmqna/graph.py`.
3. Add its name to `rmqna-calls-its-own-tools` in the workspace's
   `policies/agentlib/rules/data.yaml` and add a policy sample. Unlisted tools
   are denied; test this with `tests/test_rmqna_as_configured.py`.
4. Run the workspace checks:

   ```bat
   "%PYTHON%" -m ai_agent_lib_cli check
   ```

## Inspect model input and output

Start with this agent's scripted example, from the workspace root:

```bat
"%PYTHON%" -m rmqna.examples.trace_react
"%PYTHON%" examples\read_trace.py agents/rmqna/.agentlib/react-demo.jsonl --last 6
```

It uses this graph and the local `greet` tool, with scripted model replies and
an in-memory checkpoint. Expect 2, 4, and 6 messages on successive model calls.
It supplies its own test providers and runs with the services stopped.

To try Anthropic, follow [this agent's setup instructions](docs/anthropic-example.md),
then run:

```bat
"%PYTHON%" -m rmqna.examples.trace_anthropic
```

This sends two questions on one thread, prints the checkpoints and model trace,
and saves the files under `agents/rmqna/.agentlib/anthropic-demo/`.
It uses your configured model and API tokens. Add `--thread "your-thread-id"`
to continue a saved conversation. The examples offer the local greeting tool;
the normal CLI and HTTP entry points also discover any linked MCP tools.

The [annotated verified run](../../docs/verified-anthropic-run.md) explains how
the tool result and saved first turn became model input in a real Anthropic run.
Its sample files are included in the workspace for local inspection.

To capture calls from the normal CLI or HTTP entry point, use the following steps.
Add this setting to `agents/rmqna/.env`, then restart the agent:

```dotenv
AC2_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl
```

Run a question with a named conversation after starting any required MCP servers:

```bat
"%PYTHON%" -m rmqna "My name is Ada. Please greet me." --thread debug-ada
"%PYTHON%" -m rmqna "What is my name?" --thread debug-ada
```

Open `agents/rmqna/.agentlib/model-trace.jsonl`. Each line is a JSON event:

- `model.request` contains the complete ordered messages, normalized tool
  definitions, binding options, and per-call options.
- `model.response` contains the provider's AI message before output validation,
  including text, tool calls, and any usage metadata.
- `model.error` records a provider attempt's exception type. Match events by
  `call_id` and `attempt`; a retry increments the attempt number.

These are LangChain messages at the provider boundary. The SDK may transform
them into a different HTTP body. Use test data: recognizable credentials are
masked, but conversation content is retained. Keep the file private and out of
Git. Remove or comment out the setting, clear any process override, and restart
to stop capture. The file appends while enabled.

### How does checkpoint history reach the model?

The graph restores the same scoped thread, adds the new human question, and
passes the messages to its model node. That node prepends the system prompt.
After a tool call, the next model request includes the AI tool call and the tool
result. The model can then answer or request another tool.

Reuse `--thread debug-ada`, or the same HTTP `thread_id`, with the same caller
and application to continue. Use a new thread ID for a separate conversation.
The default fake model echoes questions; tests use scripted replies to exercise
the tool loop. Checkpointing retains history; your graph decides what to send
and when to summarize long conversations.

### Why is the trace empty?

Run `"%PYTHON%" -m ai_agent_lib_cli config explain rmqna` to confirm the path.
Calls must use `services.model()` and pass the pipeline's input checks before
they reach the provider. Check audit records for denials. A
`model_trace.disabled` warning means the destination or serialization failed;
fix it and restart. Operational DEBUG logging and model-content capture have
separate controls.

## Diagnose failures and evaluate answers

```bat
"%PYTHON%" -m ai_agent_lib_cli doctor rmqna
```

The diagnostic explains what failed and suggests a fix. A real-model
evaluation is a separate, explicit command:

```bat
"%PYTHON%" -m ai_agent_lib_cli eval rmqna
```

## Prepare a deployment

```bat
"%PYTHON%" -m ai_agent_lib_cli deploy rmqna
```

The first run creates `agents/rmqna/deploy.env`. Fill in its deployment
settings and add the provider dependencies reported by the CLI to the service's
`pyproject.toml`. Install them, review the plan, then generate the deployment files:

```bat
"%PYTHON%" -m ai_agent_lib_cli install
"%PYTHON%" -m ai_agent_lib_cli deploy rmqna --plan
"%PYTHON%" -m ai_agent_lib_cli deploy rmqna
```

**Expected result:** `deploy/rmqna/` contains Terraform and image-build
files. Follow its README to build images and apply infrastructure on a build
machine or CI runner with a container engine, Terraform, and the required
registry and AWS permissions.


### Recover an interrupted UI request

Reopen the conversation to check its saved outcome. The UI only reads status; it
never repeats the question. The generated local service keeps confirmed outcomes
in `.agentlib\outcomes.sqlite`. Protect this file like the checkpoint database.
If no confirmed outcome exists, the UI keeps the thread held.

The HTTP service accepts one active turn per caller and thread. A second request
receives `409 thread_busy` before execution. Wait for the first turn to finish
before submitting again. The default guard covers one application instance;
multiple workers or replicas sharing checkpoints need a shared `ThreadAdmission`
implementation passed to `agent_app(admission=...)`. Use a single worker until
that coordination is configured.

When model output guardrails or a structured-output schema are configured, draft
text is buffered until those checks pass. Without those checks, tokens can appear
immediately. Application-specific validation may still revise a provisional draft.

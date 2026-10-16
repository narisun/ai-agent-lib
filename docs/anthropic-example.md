# Ask Anthropic two questions and inspect the conversation

This example calls your configured Anthropic model through the `helper`
graph. It asks two questions on the same conversation thread, prints the
checkpoint after each answer, and shows the model trace. It uses the agent's
local `greet` tool, identity, and policy rules.

You need Python 3.11 or newer, pip, access to Anthropic's API, an API key, and
a model ID available to your account. Running this example sends the sample
questions and the thread's history to Anthropic and uses billable API tokens.

A live run with `claude-sonnet-4-5` has been verified: both questions shared a
thread, the model called `greet` once, and the three model inputs contained
2, 4, and 6 messages. Read the [annotated captured run](verified-anthropic-run.md)
for actual responses, selected-field JSON samples, and the checkpoint checks.
That page can be followed using the included files before making an API call.

## 1. Select Python and install dependencies

Open **Command Prompt** at your generated workspace root. Follow
[Select Python](getting-started.md#using-a-new-terminal)
to set `PYTHON` to your approved Python executable.

This agent was created with the `fake` provider. Open
`agents/helper/pyproject.toml` and add `anthropic` to the extras on its
`ai-agent-lib-core` dependency. Keep any existing version constraint. For example:

```toml
dependencies = [
    "ai-agent-lib-core[jwt,mcp,otel,serve,anthropic]",
]
```

Keep any other dependencies already in that list.

Install the workspace after saving any dependency changes:

```bat
"%PYTHON%" -m ai_agent_lib_cli install
```

`install` uses this Python's pip and the library source in `agentlib.toml`.
Repeat the Python selection in each new Command Prompt terminal.

## 2. Configure your model

Open your agent's local settings in Notepad:

```bat
notepad agents\helper\.env
```

Update these settings, replacing the example values with your model ID and key:

```dotenv
EAP_MODEL_PROVIDER=anthropic
EAP_MODEL_ID=your-model-id
ANTHROPIC_API_KEY=your-api-key
```

Keep the registry, policy, and identity settings already in the file. Keep the
key in `.env`, which is ignored by Git. If your team supplies it through the
process environment, use that existing mechanism instead. Environment values
take precedence over `.env` values.

The example enables tracing in its own configuration. You do not need to set
`EAP_MODEL_TRACE_PATH` for this script. It uses the configured checkpoint store;
the tutorial's local defaults use SQLite under `agents/helper/.agentlib/`.

## 3. Run both questions

```bat
"%PYTHON%" -m helper.examples.trace_anthropic
```

Run this directly from Command Prompt. The script builds the graph and offers
its local `greet` tool itself, so you can leave the agent and MCP service
terminals closed.

It sends these questions, in sequence:

1. "My name is Ada. Could you use the greet tool to greet me?"
2. "What is my name, and what did the greeting tool say? Use our conversation so far."

Both requests use the same authenticated caller, application, and `thread_id`.
Each request gets a fresh `request_id`. The graph restores the first turn's
checkpoint before it adds the second question.

## 4. Read the output

The script prints:

1. The selected model, conversation thread, and output file paths.
2. Each question and Anthropic's answer.
3. The complete checkpoint messages after each answer, as formatted JSON.
4. The model trace, including every model request and response from this run.

If the model calls `greet` once during the first turn and answers the second
question directly, you will see four checkpoint messages after turn 1 and six
after turn 2. The model requests will contain 2, 4, and 6 messages respectively.
The model chooses its wording and tools, so these counts can vary. The
[verified run](verified-anthropic-run.md#what-we-verified) followed this sequence;
its persisted SQLite messages also matched the final six-message snapshot.

In the trace, find the `model.request` for the second turn's `request_id`.
Its `messages` should include the first question, the first answer, any tool
exchange, and the new question. This is the context Anthropic receives.
The system prompt is added by the graph's model node on every call; it is
not part of this graph's checkpoint messages.

For each tool exchange, match `response.tool_calls[*].id` in the model response
with the `tool_call_id` in the next request's tool message. You can then see
exactly which result the graph supplied to the model.

The script saves two files in a new directory for every run:

| File | Contents |
| --- | --- |
| `agents/helper/.agentlib/anthropic-demo/<run-id>/model-trace.jsonl` | Model inputs and outputs at the library's provider adapter boundary. |
| `agents/helper/.agentlib/anthropic-demo/<run-id>/checkpoint-history.json` | The checkpoint messages after each of the two turns, with request and thread IDs. |

Copy the printed paths to open these files in your editor. You can also use the
included reader; replace `PASTE_TRACE_PATH_HERE` with the printed trace path:

```bat
"%PYTHON%" examples\read_trace.py "PASTE_TRACE_PATH_HERE"
```

The checkpoint history file is a readable snapshot. LangGraph continues the
conversation from its configured checkpoint database, not from this JSON file.
The trace contains LangChain messages and responses, not the SDK's HTTP traffic
or private model reasoning. Both files contain conversation content; keep them
private. Their `.agentlib/` directory is ignored by Git.

## 5. Continue the thread or start fresh

By default, another run starts a new conversation with a new thread ID. To
continue one, copy its printed thread ID into this command:

```bat
"%PYTHON%" -m helper.examples.trace_anthropic --thread "PASTE_THREAD_ID_HERE"
```

This sends the same two example questions again, appending them to the saved
conversation. Keep the same caller, application, and checkpoint store. The
SQLite checkpoint survives the script exiting, while each run gets its own
trace file. Use the default fresh thread when you want to compare clean runs.

## Common questions

**Why is a model ID or credential rejected?** Check the `.env` values and any
Command Prompt environment overrides. Use a model ID your account can access
and a valid API key. If you configured a `default` entry in `EAP_MODEL_ALIASES`,
that entry selects the model instead of `EAP_MODEL_ID`.

**Why did the model skip the tool?** The question asks it to use `greet`, but
tool choice remains the model's decision. Use the
scripted ReAct example (`"%PYTHON%" -m helper.examples.trace_react`)
when you need a repeatable tool-call sequence.

**What if a call fails?** Read the terminal error. A partial model trace may
already exist at the printed path, and completed graph steps may be saved in
the checkpoint store. Correct the settings or connection problem and rerun.
The checkpoint history JSON is written when both questions complete.

**How do I test changes without using API tokens?** The default tests inject
a scripted model and use the real graph, local policy rules, and SQLite
checkpoints. They also reopen the container to check saved history:

```bat
"%PYTHON%" -m pytest agents/helper/tests/test_helper_tracing.py
"%PYTHON%" -m ai_agent_lib_cli check
```

See [Model tracing and ReAct checkpoints](developer-guide.html#faq) for trace fields,
provider boundaries, and the graph's responsibilities.

# Ask Anthropic two questions and inspect the conversation

This example calls your configured Anthropic model through the `rmqna`
graph. It asks two questions on the same conversation thread, prints the
checkpoint after each answer, and shows the model trace. It builds the graph
without MCP tools, so the model answers from the schema document in its prompt;
identity and policy rules apply as usual.

You need Python 3.11 or newer, pip, access to Anthropic's API, an API key, and
a model ID available to your account. Running this example sends the sample
questions and the thread's history to Anthropic and uses billable API tokens.

The [annotated captured run](../../../docs/verified-anthropic-run.md) was made with the
workspace template's sample greeting agent, before this agent replaced it. It is kept to show how a
tool result and saved history become model input: actual responses, selected-field JSON samples,
and the checkpoint checks. Its tool is the template's `greet`, not one of rmqna's. That page can
be followed using the included files before making an API call.

## 1. Select Python and install dependencies

Open **Command Prompt** at your generated workspace root. Follow
[Select Python](../../../README.md#select-python)
to set `PYTHON` to your approved Python executable.

This agent's dependency already includes the library's `anthropic` extra.

Install the workspace after saving any dependency changes:

```bat
"%PYTHON%" -m ai_agent_lib_cli install
```

`install` uses this Python's pip and the library source in `agentlib.toml`.
Repeat the Python selection in each new Command Prompt terminal.

## 2. Configure your model

Open your agent's local settings in Notepad:

```bat
notepad agents\rmqna\.env
```

Update these settings, replacing the example values with your model ID and key:

```dotenv
AC2_MODEL_PROVIDER=anthropic
AC2_MODEL_ID=your-model-id
ANTHROPIC_API_KEY=your-api-key
```

Keep the registry, policy, and identity settings already in the file. Keep the
key in `.env`, which is ignored by Git. If your team supplies it through the
process environment, use that existing mechanism instead. Environment values
take precedence over `.env` values.

The example enables tracing in its own configuration. You do not need to set
`AC2_MODEL_TRACE_PATH` for this script. It uses the configured checkpoint store;
the tutorial's local defaults use SQLite under `agents/rmqna/.agentlib/`.

## 3. Run both questions

```bat
"%PYTHON%" -m rmqna.examples.trace_anthropic
```

Run this directly from Command Prompt. The script builds the graph itself,
without MCP tools, so you can leave the agent and MCP service terminals closed.

It sends these questions, in sequence:

1. "Which tables describe a client's credit facilities?"
2. "Which of those holds covenant tests? Use our conversation so far."

Both requests use the same authenticated caller, application, and `thread_id`.
Each request gets a fresh `request_id`. The graph restores the first turn's
checkpoint before it adds the second question.

## 4. Read the output

The script prints:

1. The selected model, conversation thread, and output file paths.
2. Each question and Anthropic's answer.
3. The complete checkpoint messages after each answer, as formatted JSON.
4. The model trace, including every model request and response from this run.

If the model answers both questions directly, you will see two checkpoint messages after
turn 1 and four after turn 2. The model requests will contain 2 and 4 messages respectively.
The model chooses its wording, so these counts can vary. For a run with a tool call, see
the [verified run](../../../docs/verified-anthropic-run.md#what-we-verified) of the template
agent, or run the scripted tool-loop example, `rmqna.examples.trace_react`.

In the trace, find the `model.request` for the second turn's `request_id`.
Its `messages` should include the first question, the first answer, any tool
exchange, and the new question. This is the context Anthropic receives.
The system prompt (instructions, schema document and examples, one static block) is added by
the graph's model node on every call; it is not part of this graph's checkpoint messages.

For each tool exchange, match `response.tool_calls[*].id` in the model response
with the `tool_call_id` in the next request's tool message. You can then see
exactly which result the graph supplied to the model.

The script saves two files in a new directory for every run:

| File | Contents |
| --- | --- |
| `agents/rmqna/.agentlib/anthropic-demo/<run-id>/model-trace.jsonl` | Model inputs and outputs at the library's provider adapter boundary. |
| `agents/rmqna/.agentlib/anthropic-demo/<run-id>/checkpoint-history.json` | The checkpoint messages after each of the two turns, with request and thread IDs. |

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
"%PYTHON%" -m rmqna.examples.trace_anthropic --thread "PASTE_THREAD_ID_HERE"
```

This sends the same two example questions again, appending them to the saved
conversation. Keep the same caller, application, and checkpoint store. The
SQLite checkpoint survives the script exiting, while each run gets its own
trace file. Use the default fresh thread when you want to compare clean runs.

## Common questions

**Why is a model ID or credential rejected?** Check the `.env` values and any
Command Prompt environment overrides. Use a model ID your account can access
and a valid API key. If you configured a `default` entry in `AC2_MODEL_ALIASES`,
that entry selects the model instead of `AC2_MODEL_ID`.

**Why did the model not run any SQL?** This example gives it no tools. Use the
scripted tool-loop example (`"%PYTHON%" -m rmqna.examples.trace_react`) for a repeatable
tool-call sequence, or ask the running agent, which discovers the `rmqna-data` tools.

**What if a call fails?** Read the terminal error. A partial model trace may
already exist at the printed path, and completed graph steps may be saved in
the checkpoint store. Correct the settings or connection problem and rerun.
The checkpoint history JSON is written when both questions complete.

**How do I test changes without using API tokens?** The default tests inject
a scripted model and use the real graph, local policy rules, and SQLite
checkpoints. They also reopen the container to check saved history:

```bat
"%PYTHON%" -m pytest agents/rmqna/tests/test_rmqna_tracing.py
"%PYTHON%" -m ai_agent_lib_cli check
```

See [Model tracing and ReAct checkpoints](../README.md#inspect-model-input-and-output) for trace fields,
provider boundaries, and the graph's responsibilities.

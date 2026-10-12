# Follow-ups, memo versions and long conversations

Use one thread for a conversation. Each accepted, evidence-backed memo gets a
reference such as `memo-a12b34c56d78` and a version number for its client. The
reference appears at the end of the answer. A new memo creates a new version;
earlier versions keep their original text and evidence.

The agent selects the document before it calls the model. It then supplies that
document, relevant evidence and recent discussion as model input. LangGraph
stores the conversation and memo records in the existing scoped checkpoint.
The HTTP request and response formats stay the same.

## Try two memos and a follow-up

Follow the [workspace setup](../README.md), configure a tool-capable model, and
start the MCP service in a second Command Prompt. From `examples\rmprep`, run:

```bat
python -m rmprep "memo: Prepare a memo for JONE-0022 as of 2026-07-03." --thread jones-review
python -m rmprep "memo: Prepare a memo for JONE-0022 as of 2026-08-03." --thread jones-review
python -m rmprep "qna: Explain the previous memo's covenant findings." --thread jones-review
python -m rmprep "qna: Explain this memo's working-capital section." --thread jones-review
python -m rmprep "qna: Compare versions 1 and 2 for JONE-0022." --thread jones-review
```

The third request selects the July memo. The fourth continues with July because
that is now the active document. The fifth selects both versions explicitly.
Versions follow generation order for each client, not meeting-date order.
If you reuse an existing thread, use the version numbers printed in its answers.

The CSV data is a supplied snapshot. Date filters exclude later dated records;
they cannot reconstruct what undated/current-status records said in July.
Those limitations remain part of each memo's evidence.

## Select the intended document

| Request | Selection |
| --- | --- |
| `Explain this memo.` | The active memo. A Q&A answer does not become a memo. |
| `Explain the latest memo for JONE-0022.` | The most recently generated memo for that client. |
| `Explain the previous memo.` | The preceding version for the active client, relative to the active memo. |
| `Explain version 1 for JONE-0022.` | That client's first version in this thread. |
| `Explain the July memo for JONE-0022.` | A matching meeting month; clarification if several versions match. |
| `Explain the memo as of 2026-07-03.` | A matching meeting date for the active client; clarification if several versions match. |
| `Explain memo-a12b34c56d78.` | That exact reference in this authorized thread. Replace this example with a real reference from an answer. |
| `Compare versions 1 and 2 for JONE-0022.` | Both snapshots, identified separately in the model input. |
| `Explain the SHIN-0039 memo.` | The active or most recent memo for SHIN-0039. JONE-0022 evidence is excluded. |

An unavailable or ambiguous reference produces a clarification before any model
call. Relative generation dates such as “yesterday's memo” ask for an explicit
date or reference. For a different client, provide its UEN or full name; use
`memo:` and `qna:` to make the workflow explicit. The selector uses deterministic
rules, so an exact memo reference is the clearest way to select a document.

References work inside their own thread and authorization scope. A memo ID from
another thread does not grant access to that thread. Starting a new thread starts
a separate conversation.

## Explain saved evidence or retrieve current evidence

“Explain this memo's covenant findings” uses the selected memo's original
evidence. Additional tool requests must keep that client and meeting date.
Client or date drift is rejected before the tool runs; the model can correct
its arguments up to twice.

“What has changed since this memo? Retrieve current evidence” keeps the original
memo as a baseline and requires a new tool result. Its retrieval date is today's
UTC date. The answer must distinguish the original snapshot from the new result.
The supplied CSV files remain the data source; a new retrieval is not a live
banking feed. To compare another explicit meeting date, generate that memo and
compare the two versions.

Earlier answers and discussion summaries are background. Source records provide
the evidence. Exact citations are checked against the evidence actually included
in that model call, rather than every record ever retrieved in the thread.

## Keep long conversations usable

The checkpoint keeps the full conversation. Before each model call, rmprep:

1. Pins the selected memo versions and their original tool exchanges.
2. Includes relevant evidence sections for focused questions, with original
   values, source references, dates and limitations.
3. Adds recent complete exchanges for the same selected context.
4. Includes a short extractive summary of older discussion topics when needed.
   It is labeled as user text, not evidence.
5. Checks the input budget, including subsequent tool results and draft revisions.

Tool calls and tool results stay together. Original payloads are never edited by
evidence projection. Very large questions must be split; an oversized selected
evidence pack requires a narrower section or date range. The agent reports a
validation error before sending an oversized input to the model.

The default policy allows an estimated 48,000 input tokens, including a 4,000
token allowance for tool definitions. A question may use 8,000 tokens and recent
discussion may use 6,000. These are conservative UTF-8 text estimates, not the
provider's tokenizer. Choose a policy below your model's supported context window,
leaving space for its output and checking actual usage in model traces.

For a different policy, inject it where your application builds the graph:

```python
from rmprep.context import ContextBudget, ContextBuilder
from rmprep.graph import build_graph

graph = build_graph(
    services,
    mcp_tools,
    context_builder=ContextBuilder(
        ContextBudget(
            input_tokens=24_000,
            question_tokens=4_000,
            recent_tokens=3_000,
            tool_schema_tokens=4_000,
        )
    ),
)
```

## Inspect the decision

Set `EAP_MODEL_TRACE_PATH=.agentlib/model-trace.jsonl` in the agent's `.env`.
Each model request contains a `Context selection` manifest in its system
message. It identifies the selected memo IDs, client, meeting date, retrieval
date, mode, included message IDs, omitted turns, discussion summary and estimated
input size. The model messages show the actual evidence projection.

The latest checkpoint exposes the same manifest as `context_diagnostics`, plus
`memos`, `active_memo_id`, `active_client` and `selection`. This is agent state
available to checkpoint inspection; the UI's dedicated inspector remains a
separate frontend phase. Rejected drafts appear in traces but do not create
memo versions.

Run the local scripted walkthrough to inspect a memo and Q&A without a model API:

```bat
python -m rmprep.examples.trace_react
```

Its `checkpoint-history.json` includes memo records and context diagnostics, as
well as the saved conversation. The script prints the output directory.

On first use, older checkpoints are indexed for evidence-backed memo turns.
Their messages stay unchanged. An unavailable original generation timestamp is
marked unknown. Plain historical answers without a memo evidence pack remain
conversation messages.

## Run the regression checks

From `examples\rmprep`, in its Python environment:

```bat
python -m pytest agents\rmprep\tests -q
python -m ai_agent_lib_cli check
```

The tests use scripted model decisions. They verify selection, MCP arguments,
model input, immutable versions, scoped persistence, restart behavior and input
budgets. Business reviewers should still evaluate the quality of model-written
explanations against their selected evidence.

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
| `Explain the July 2025 memo.` | A matching month and year for the active client. |
| `In this memo, was the sweep deployed in October 2025?` | The active memo; October describes the event being questioned, not the document date. |
| `Explain the memo as of 2026-07-03.` | A matching meeting date for the active client; clarification if several versions match. |
| `Explain memo-a12b34c56d78.` | That exact reference in this authorized thread. Replace this example with a real reference from an answer. |
| `Compare versions 1 and 2 for JONE-0022.` | Both snapshots, identified separately in the model input. |
| `Explain the SHIN-0039 memo.` | The active or most recent memo for SHIN-0039. JONE-0022 evidence is excluded. |

An unavailable or ambiguous reference produces a clarification before any model
call. Relative generation dates such as “yesterday's memo” ask for an explicit
date or reference. For a different client, provide its UEN or full name; use
`memo:` and `qna:` to make the workflow explicit. The selector uses deterministic
rules, so an exact memo reference is the clearest way to select a document.

A broad memo has a 1,000-word maximum, including headings and references; Q&A
defaults to 350 words. An explicit request such as `within 600 words` overrides
that default. The model targets 70% of the available body budget to leave room
for complete citations. Validation allows at most two revisions; rejected drafts
remain visible in model traces and never become accepted memo versions.

A successful `find_clients` lookup also remembers the search name in the thread.
For example, `Canada Goose` can resolve to `Canada Goose Holdings Inc.` and UEN
`CANA-0010`. After `prepare rm prep for Canada Goose`, ask `How did you calculate
wallet share for Canada Goose?` in the same thread. The shorter name selects the
saved client and memo. A complete search with several matches remembers each
canonical name, but its broad search term does not become an alias for one client.
A truncated lookup establishes no alias. A name shared by several clients requires
clarification when the request needs one client.

For this question, the model receives the memo and its original `relationship`
section, including source references and limitations. The supplied CSV records
`wallet_share_pct` as **22.55%** for `CANA-0010`. That is a recorded percentage;
the CSV does not supply its numerator, denominator, measurement period or
calculation method. The agent should explain that gap and cite
`relationship[uen=CANA-0010]`.

References work inside their own thread and authorization scope. A memo ID from
another thread does not grant access to that thread. Starting a new thread starts
a separate conversation.

## Compare clients returned by a search

In one UI thread, ask these questions in order:

1. `Do we have a client containing canada?`
2. `Compare these two clients' financials.`
3. `Which of them had the larger revenue decline?`

The supplied snapshot returns Canada Goose Holdings Inc. (`CANA-0010`) and Sleep
Country Canada Inc. (`SLEE-0041`). The follow-up selects both UENs as a group. The
agent can retrieve their financials in parallel or one at a time; receiving the
first result keeps the second client selected. Each company needs its own evidence,
currency, period and source references in the answer.

The Command Prompt equivalent, using the same setup and running MCP service, is:

```bat
python -m rmprep "Do we have a client containing canada?" --thread canada-comparison
python -m rmprep "Compare these two clients' financials." --thread canada-comparison
```

Inspect `selection.client_uens` in the checkpoint and the Context selection block
in `model.request.messages`. Both should list `CANA-0010` and `SLEE-0041`. The model
input also keeps the original search exchange, including the tool call and result.
Once retrieved, the financial tool results for both clients should be present.

If a search is truncated, or "these two" refers to three matches, specify the UENs.
To switch clients, ask `Show financials for JONE-0022.` This selects Jones and stops
carrying the comparison group into subsequent questions. Memo references continue
to select their own saved snapshots and dates.

Checkpointing saves and restores state. The application still decides which saved
messages reach the model and which tool arguments are allowed. When a follow-up
loses context, compare the checkpoint with **every** model request in its ReAct
loop, including validation corrections. A correct first request can still lose
context after a tool result. Existing threads recover a search group from their
saved tool result on the next comparison request; no history reset is needed.

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

## Follow-up questions retain the accepted subject and date

The checkpoint stores a `focus` record with the selected clients, memo references,
evidence date and source-exchange message IDs. Routing reads that record; an
accepted answer updates it. Older checkpoints are normalized when next opened.

- After a memo, "What about debt?" keeps the same client and memo.
- After retrieving current financials, "How did you calculate that?" uses the
  fresh evidence and answer. It does not silently return to the older memo.
- After a question with an explicit as-of date, subsequent questions retain that
  date until you change it.
- "Refresh <memo reference> as of 2026-10-12" creates a new version for that date.
  "Explain <memo reference>" selects the original saved document.
- A failed or unresolved company lookup does not erase the accepted focus.

Exact citations must exist in the evidence sent to the model. Prose-based guesses
about which periods a comparison needs are recorded under `review_advisories` in
context diagnostics. These advisories do not cause model revision loops. They
are useful review hints, not proof of financial correctness.

## Keep long conversations usable

The checkpoint keeps the full conversation. Before each model call, rmprep:

1. Pins the selected memo versions, or a selected client group's search, with
   their original tool exchanges. Follow-ups can also pin the latest accepted
   evidence exchange from conversation focus.
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

The default policy allows an estimated **128,000 input tokens**, including a
4,000-token allowance for tool definitions. Recent discussion can use **96,000
tokens**, and a question can use **16,000 tokens**. These share the total input
limit; they are not extra allowances added to it. Selected memos, the current tool
loop and the system prompt also use that total.

The verified two-client financial comparison used about 13,500 estimated tokens
for one exchange. The larger recent-history limit keeps that exchange available
to the next question. Q&A reuses applicable financial records already in model
input; it retrieves more when records are missing, the requested scope changes,
or the user explicitly requests fresh data.

Set these in `agents\rmprep\.env` and restart the agent:

```dotenv
RMPREP_CONTEXT_INPUT_TOKENS=128000
RMPREP_CONTEXT_RECENT_TOKENS=96000
RMPREP_CONTEXT_QUESTION_TOKENS=16000
RMPREP_CONTEXT_TOOL_SCHEMA_TOKENS=4000
```

The [developer guide's configuration table](../../../docs/developer-guide.html#variables)
lists each setting's purpose and local/AWS usage. The same variables work as
environment settings on the agent's Fargate task. These are rmprep settings;
other agents keep their own context policies. Process variables override `.env`.
For a temporary Command Prompt override before starting the batch script:

```bat
set "RMPREP_CONTEXT_RECENT_TOKENS=96000"
start-local.bat
```

Run the batch script from the library repository root. To check the resolved
values in the Python environment where rmprep is installed:

```bat
python -c "from rmprep.settings import load_context_budget; print(load_context_budget())"
```

Use positive whole numbers. The question and recent-history limits must each fit
inside the input budget after the tool-definition allowance. Invalid values fail
startup. These are conservative UTF-8 text estimates, not the provider's tokenizer.
Keep input below the deployed model's context window with room for generated output.
Larger input can increase cost and latency; prompt caching does not increase the
context window. See [Anthropic's context-window guide](https://platform.claude.com/docs/en/build-with-claude/context-windows).

No finite budget fits every conversation. When history outgrows it, the checkpoint
still retains all messages. Model input keeps the selected memo/search exchanges
and the newest complete discussion that fits. Older discussion is represented by
short topic extracts, not a full summary of its facts. A question about omitted
evidence may need retrieval again. For exact historical memo facts, select the memo
reference so its original evidence is pinned. The larger budget does not promise
perfect recall of an unlimited conversation.

Programmatic callers can inject a policy without reading the environment. For
example, use lower limits for a model with a smaller context window:

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
date, mode, omitted turns and discussion summary. The model messages show the
actual evidence projection. Message IDs and token estimates are kept in checkpoint
diagnostics, so those changing values do not invalidate the prompt cache.

The latest checkpoint exposes that manifest and diagnostic counts as `context_diagnostics`, plus
`memos`, `active_memo_id`, `active_client` and `selection`. This is agent state
available to checkpoint inspection; the UI's dedicated inspector remains a
separate frontend phase. Rejected drafts appear in traces but do not create
memo versions.

Diagnostics include `input_token_budget`, `recent_token_budget`,
`retained_recent_tokens`, `question_token_budget` and `tool_schema_token_allowance`.
Use these with `included_message_ids` and `omitted_turns` to see what fit. They stay
out of the static prompt so budget diagnostics do not disturb prompt caching.

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

Existing threads whose `prepare rm prep` request previously went through Q&A
can recover that memo on their next question when the thread has no memo index
and the original successful evidence pack is saved. This upgrade preserves the
original messages and evidence; it does not regenerate the memo.

## Static prompts and Anthropic caching

Prompt files are installed with the agent package:

| Resource under `src/rmprep/prompts` | Purpose |
| --- | --- |
| `rmprep/common.v3.md` | Shared evidence, citation, client-group and context rules. |
| `memoagent/system.v2.md` | Memo structure and evidence retrieval instructions. |
| `qnaagent/system.v3.md` | Reuse supplied evidence for follow-ups; retrieve missing or refreshed client records. |

`prompts/__init__.py` pins those versions and loads them using Python package
resources. It works from any directory and from an installed wheel. For a prompt
change, add a new numbered file and update the pinned version. The router is a
Python decision function; it has no LLM prompt.

The first system text block contains the static common and specialist instructions.
The second contains the selected client/memo, dates, output budget and other request
context. Selected history follows. This separation keeps changing values out of
the static prefix without changing which evidence the model can see.

For Anthropic, set this in `agents\rmprep\.env` and restart the agent:

```dotenv
EAP_MODEL_OPTIONS={"anthropic":{"prompt_caching":true,"cache_ttl":"5m"}}
```

The library marks the static block with an explicit cache breakpoint and enables
Anthropic automatic caching of the growing conversation. A first request writes
eligible prefixes; subsequent matching requests can read them. Switching specialists
uses a different prompt, so each specialist may need a warming request. Changing
selected history can invalidate later prefixes while retaining the static one.

Check `model.request.invocation_options.cache_control`, the first system block's
`cache_control`, and `model.response.response.usage_metadata.input_token_details`.
The UI shows cache reads, cache writes and the read/total-input percentage across
all model calls for the answer. A 0% hit with positive cache writes is normal while
warming. Small prefixes and expired entries can also give 0% hits. Sonnet 4.5 needs
at least 1,024 tokens in a cacheable prefix.

Set `prompt_caching` to `false` to turn it off. `cache_ttl` also accepts `1h`, which
has a higher cache-write cost. These options apply to direct Anthropic, not Bedrock.
See [Anthropic's prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
for model-specific limits and pricing. Policy, guardrails, audit and checkpoint
selection still run on every request; this does not reuse earlier model answers.

## Why does a live answer sometimes change before it finishes?

The live draft shows model text before the agent has checked it. A tool call can
add evidence, and a failed length or citation check can trigger up to two revisions.
The UI labels each model call and collapses the draft when the request ends.

Citation checks distinguish changes across time from comparisons between clients.
For example, comparing two clients' FY2025 revenues requires their FY2025 sources.
Explaining one client's revenue growth from FY2024 to FY2025 requires both years.
The presence of older records in the conversation does not change those requirements.

If the answer still fails the checks, the UI saves a failed outcome with reported
token usage and elapsed time. You can send another question in the same thread.
Only accepted answers are added to its saved conversation. A broken connection or
unconfirmed outcome still holds the UI thread to prevent duplicate work.

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

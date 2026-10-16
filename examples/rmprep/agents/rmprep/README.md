# rmprep agent

One service contains three LangGraph business nodes:

- `router` chooses memo preparation or Q&A from the newest question.
- `memoagent` builds a sourced memo from the MCP evidence pack.
- `qnaagent` answers questions using the saved conversation and scoped MCP queries.

Start with the [workspace walkthrough](../../README.md). It covers Command
Prompt setup with Python and pip, model configuration, tracing and checks.

From `examples\rmprep`, with the environment active:

```bat
python -m rmprep.examples.trace_react
python -m pytest agents\rmprep\tests -q
```

The walkthrough scripts the model while using the real CSV queries, policy and
checkpoints. For model-written answers, configure a tool-capable model in this
service's `.env` and start `python -m rmprep_data` in another Command Prompt.

```bat
python -m rmprep "memo: Prepare a memo for JONE-0022 as of 2026-08-03." --thread jones-aug03
python -m rmprep "qna: Explain the covenant findings and their sources." --thread jones-aug03
```

Keep the same thread for follow-ups. The default fake model echoes input.
The supplied data has client and banking evidence; external news is unavailable.

Accepted evidence-backed memos include a reference and a version per client.
Use `qna: Explain version 1 for JONE-0022.` to select an older memo, or
`qna: Compare versions 1 and 2 for JONE-0022.` to compare snapshots. The agent
clarifies ambiguous references and bounds model input while preserving the full
saved conversation. See [memo context management](../../docs/context-management.md)
for a Command Prompt walkthrough, budget configuration and trace examples.

`graph.py` composes the service, `router.py` selects the specialist,
`prompts/` contains versioned evidence and memo instructions, and `specialist.py`
runs the model/tool loop. `response_review.py` checks final-answer length and
exact source references. The injected `ServiceContainer` supplies infrastructure.
It also rejects citation placeholders and known wording that invents the fiscal
year of an audited-statement requirement when the evidence leaves it unknown.
Named financial comparisons need both source references for that concept; a
revenue citation cannot stand in for gross profit.
Financial calculations and review findings belong to the MCP evidence layer;
the agent explains them and proposes questions for the RM to investigate.

Include `Keep it within 450 words.` to set a word limit. Defaults are 1,000 words
for a memo and 350 for Q&A, including headings, tables and citations. A reply
that fails length or citation checks gets up to two model revisions. Continued
failure raises a validation error. Drafts appear in model traces; only accepted
replies enter the conversation checkpoint. These checks do not establish that
every claim is supported: evaluate the prose against the cited records too.


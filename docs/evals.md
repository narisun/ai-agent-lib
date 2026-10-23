# Measure an agent with evals

An eval runs an agent and scores what comes back, so a prompt or model change
is judged by more than one person's read of one transcript. The library
supports two kinds, both off the request path — `agentcraft2_core.evaluation`
is never imported there:

- **Offline**, against a dataset you write by hand: `evals/cases.jsonl` →
  `run_eval` → scorers → `report.require({...})`.
- **Online**, against what production already answered: capture (Agent Lab
  feedback and `export_interactions`) → `sampled` → `score_interactions` →
  the same `require`/`require_no_regression` gates.

This guide uses the `helper` agent from the [getting-started
tutorial](getting-started.md) as its running example. Everything below also
applies to a hand-written service; swap in your own package name and graph.
Run every CLI command below from the workspace root — `tutorial/` in the
tutorial — the same place `agentcraft2 new agent` and `agentcraft2 check` run from.

## 1. What an eval is here

Both kinds end at the same two types: a `Scorer` returns a `Score` from 0 to
1 for one case; an `EvalReport` holds every case's scores and exposes
`.means`, `.by_tag(...)`, `.require(...)` and `.to_json()`. An offline run
builds its cases from a JSONL file with a known expected answer. An online
run builds its cases from recorded production exchanges, which usually have
no expected answer to match — a `judged` scorer, or another model- or
rule-based `scored_by`, stands in for it.

| | Offline | Online |
| --- | --- | --- |
| Cases come from | `evals/cases.jsonl`, written by hand | Sampled production interactions |
| Expected answer | Known, in the case | Usually none; a judge grades the answer instead |
| Runs the agent again | Yes — `run_eval` calls your `ask`/`answer` function | No — `score_interactions` scores the answer already recorded |
| Typical cadence | On every change, in CI | Periodically, over a rolling window |
| Gate | `report.require({...})` | `report.require({...})` and/or `require_no_regression(...)` |

A minimal offline run, scoring an exact match on a structured field:

```python
from pathlib import Path

from agentcraft2_core.evaluation import exact, load_cases, run_eval

cases = load_cases(Path("evals/cases.jsonl"))
report = await run_eval(triage, cases, [exact("category")])
report.require({"category": 0.9})
```

`run_eval` never raises for a failing case — a case whose task raises scores
0 on every metric and is reported by the error's type, and the run goes on.
`report.require(...)` is the assertion that turns "the mean was low" into a
test failure.

## 2. Offline: the generated loop

`agentcraft2 new agent helper` writes two files beside the agent: a starter
dataset and a test that runs it. The dataset, `agents/helper/evals/cases.jsonl`:

```jsonl
{"id": "greet-ada", "input": {"question": "Say hello to Ada."}, "expected": {"mentions": "Ada"}}
{"id": "greet-grace", "input": {"question": "Please greet Grace for me."}, "expected": {"mentions": "Grace"}}
{"id": "greet-nobody", "input": {"question": "Greet someone whose name is empty."}, "expected": {"mentions": "nobody"}, "tags": ["edge"]}
```

And the test, `agents/helper/tests/test_helper_eval.py`:

```python
from pathlib import Path

import pytest
from agentcraft2_core import ServiceContainer
from agentcraft2_core.evaluation import (
    EvalCase,
    dataset_digest,
    load_cases,
    load_report,
    require_no_regression,
    run_eval,
    scored_by,
)
from agentcraft2_core.testing import load_eval_config

from helper import APPLICATION, answer, build_graph

SERVICE = Path(__file__).resolve().parents[1]
CASES = SERVICE / "evals" / "cases.jsonl"
REPORT = SERVICE / ".agentcraft2" / "eval-report.json"
BASELINE = SERVICE / "evals" / "baseline.json"
ACCEPT_MARKER = SERVICE / ".agentcraft2" / "eval-accept.flag"


def mentions(case: EvalCase, output: str) -> bool:
    return str(case.expected["mentions"]).lower() in output.lower()


@pytest.mark.eval
@pytest.mark.enable_socket
async def test_helper_meets_its_bar(tmp_path: Path) -> None:
    config = load_eval_config(SERVICE / ".env", state_dir=tmp_path)
    if config.model.provider == "fake":
        pytest.skip("set a real model provider and model ID in the service's .env")
    async with ServiceContainer(config) as services:
        await services.validate()
        graph = build_graph(services)

        async def ask(case: EvalCase) -> str:
            context = await services.authenticate(
                None, application=APPLICATION, thread_id=f"eval-{case.id}"
            )
            return await answer(graph, services, context, str(case.input["question"]))

        report = await run_eval(
            ask,
            load_cases(CASES),
            [scored_by("mentions", mentions)],
            case_timeout=120.0,
            metadata={
                "application": APPLICATION,
                "model": f"{config.model.provider}:{config.model.model_id or ''}",
                "dataset": dataset_digest(CASES),
            },
        )
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report.to_json(), encoding="utf-8")
    print("\n".join(report.summary()))
    if BASELINE.exists() and not ACCEPT_MARKER.exists():
        require_no_regression(report, load_report(BASELINE), tolerance=0.05)
    report.require({"mentions": 0.8})
```

This test is marked `@pytest.mark.eval` and excluded from the default
`pytest`/`agentcraft2 check` run, so it never blocks on a model nor incurs
charges by accident. With `AC2_MODEL_PROVIDER=fake` it skips itself.
`dataset_digest(CASES)` is a sha256 of the file's bytes, recorded in the
report's metadata. It is a record for people: it tells you whether two reports
ran the same cases. The baseline gate does not check it (see below).

Run it explicitly once a real model is configured (see [step 8 of the
tutorial](getting-started.md#8-optional-use-and-evaluate-a-real-model)):

```bat
python -m agentcraft2_cli eval helper
```

This writes `agents/helper/.agentcraft2/eval-report.json` — `report.to_json()`,
with the schema `agentcraft2.eval-report/v1`, its metadata, every case's scores,
and the mean per metric. That file is transient state, not a record to keep
by hand.

Accept the current report as the bar to protect once you are satisfied with
it:

```bat
python -m agentcraft2_cli eval helper --accept
```

`--accept` runs the evals itself — it does not reuse the last report. If that
run passes, it copies the fresh `.agentcraft2/eval-report.json` to
`evals/baseline.json` and prints the path. Commit `evals/baseline.json`
alongside `evals/cases.jsonl` — both hold synthetic cases and aggregate scores,
not conversation content. From then on, every `agentcraft2 eval helper` run also
calls `require_no_regression`, which compares each metric's mean against the
baseline and fails when it dropped by more than its `tolerance` (0.05 above).
The failure names the regressed metrics, both means, and the case IDs whose own
score dropped — read those cases first.

The comparison matches metrics by name and cases by ID. A metric present in
only one report is not compared, and the gate never looks at `dataset_digest`,
so after you add or edit cases on purpose, accept a new baseline yourself.

Re-run `--accept` once a regression is intentional (the model changed, the
rubric changed) to move the bar. On an intentional regression, `--accept`
skips only the baseline gate. While that run is in flight it leaves a marker
(`.agentcraft2/eval-accept.flag`) beside the report, and the generated test skips
only the `require_no_regression` gate against the existing baseline while the
marker is there. The absolute `report.require({...})` bar still applies, so the
run still has to pass before its report replaces the baseline. The marker is
removed once the run ends, whether or not it passed.

## 3. Judges

`exact` and `scored_by` compare an output against something you can write in
code. For what cannot be matched that way — tone, faithfulness to the
evidence, whether an answer actually addresses the question — ask a model
instead with `judged`. Build the judge from the `judge` model alias, not a
hardcoded model ID, so the eval names a role:

```python
from agentcraft2_core.evaluation import Verdict, judged

verdicts = services.model("judge").with_structured_output(Verdict)
politeness = judged(verdicts.ainvoke, "Is the answer polite and on topic?")
```

`Verdict` is a small Pydantic model (`score` from 1 to 5, a short `reason`);
`judged` rescales it to 0–1 and keeps the reason as the score's note. Add the
alias to the service's `.env` so `services.model("judge")` resolves, typically
to a cheaper or faster model than `default`:

```dotenv
AC2_MODEL_ALIASES={"judge": {"model_id": "your-judge-model-id"}}
```

See the [searchable configuration table](developer-guide.html#variables) for
`AC2_MODEL_ALIASES` and every other variable.

A judge is itself a model call and can be wrong or inconsistent. Check it
against a few answers a person has already graded before trusting its score
in a gate:

```python
from pathlib import Path

from agentcraft2_core.evaluation import LabeledCase, calibrate, load_cases

cases = {case.id: case for case in load_cases(Path("evals/cases.jsonl"))}
labeled = [
    LabeledCase(cases["greet-ada"], "Hello there, Ada!", human=1.0),
    LabeledCase(cases["greet-grace"], "Greetings.", human=0.4),
]
calibration = await calibrate(politeness, labeled, within=0.25)
print("\n".join(calibration.summary()))
```

`calibrate` runs the scorer over every labeled case and reports the mean gap
to the human grade and the fraction within `within` of it (the
`disagreements`, by case ID, are the rest). A judge that disagrees with
people measures the judge, not the agent — fix the rubric or pick a better
judge model before using it to gate anything.

## 4. Online: capture

Capture happens on the request path, but it is cheap and append-only: it
never scores anything itself.

**Feedback.** Agent Lab shows thumbs up/down controls beneath every accepted
answer (see [sign in and chat](../packages/agentcraft2-ui/README.md#4-sign-in-and-chat)).
Clicking one writes one `agentcraft2.feedback/v1` line to `feedback.jsonl`, in
the same `.agentcraft2\ui\` folder as `conversations.sqlite`
(see [initialize the UI](../packages/agentcraft2-ui/README.md#3-initialize-the-ui)).
A record is never deleted, only appended — feedback is evidence.

**Export.** Turn saved conversations into interaction records with
`export_interactions`, joining feedback by request ID when a feedback log is
given:

```python
from pathlib import Path

from agentcraft2_ui.adapters.catalog import LocalAccess
from agentcraft2_ui.adapters.interactions import export_interactions
from agentcraft2_ui.adapters.sqlite import SqliteThreads
from agentcraft2_ui.config.settings import Settings
from agentcraft2_ui.config.shell import private_secret

STATE = Path(".agentcraft2/ui")
settings = Settings.read(STATE / "config.json")
store = SqliteThreads(
    STATE / "conversations.sqlite",
    LocalAccess(settings),
    private_secret(STATE / "session-secret").encode(),
)

count = await export_interactions(
    store,
    settings.identity.principal(),
    STATE / "online" / "production.jsonl",
    feedback_path=STATE / "feedback.jsonl",
)
print(f"wrote {count} interactions")
```

This is a documented Python entry point, not yet a CLI command — save it as a
small script and run it with the UI's own Python. It writes every completed
exchange the principal owns, active and archived conversations alike, as
`agentcraft2.interaction/v1` JSONL (`load_interactions` reads it back). Run it
after the server has handled some real traffic; `export_interactions` reads
the UI's existing state, it does not call any agent. Writing the export under
`STATE` (`.agentcraft2/ui/online/`) keeps it beside the UI's other private
state, inside the folder the repository already ignores — see
[privacy](#6-privacy).

## 5. Online: scoring

Scoring reuses offline's machinery. `sampled` picks a reproducible slice of
the exported interactions — the same rate and salt choose the same rows
anywhere, so a rerun or a second scorer sees the identical sample —
and `score_interactions` scores it with ordinary scorers, returning an
ordinary `EvalReport`:

```python
from pathlib import Path

from agentcraft2_core.evaluation import (
    load_interactions,
    load_report,
    require_no_regression,
    sampled,
    score_interactions,
)

STATE = Path(".agentcraft2/ui")
interactions = load_interactions(STATE / "online" / "production.jsonl")
weekly = sampled(interactions, rate=0.1, salt="2026-10-18")
report = await score_interactions(weekly, [politeness])

report.require({"judged": 0.8})
baseline = STATE / "online" / "baseline.json"
if baseline.exists():
    require_no_regression(report, load_report(baseline), tolerance=0.05)
print(report.by_tag("down"))
```

Each interaction becomes a case whose `id` is the request ID and whose tag is
its feedback verdict, `"up"`, `"down"`, or none — `report.by_tag("down")`
isolates the mean score on exactly the answers a person rejected, which is
usually the first thing worth reading. `score_interactions` does not call the
agent again; the task it runs is the recorded `answer`, so only the scorers
(typically `judged`) make model calls. Because online cases carry no
`expected` field, scorers that compare against one — `exact` — score every
case 0; use `judged` or a rule-based `scored_by` instead.

Gate online scoring exactly like an offline run: `report.require({...})` for
an absolute bar, `require_no_regression` against a saved online baseline for
drift. Write that baseline the same way `--accept` does offline —
`baseline.write_text(report.to_json())` — after a run you are satisfied with.

## 6. Privacy

Every file in this guide other than `evals/cases.jsonl` and
`evals/baseline.json` holds real conversation content: `feedback.jsonl`, an
exported interaction JSONL, and any report produced by scoring real
interactions (a judge's `reason` can quote the question or the answer). Treat
them like a model trace that captures content:

- Keep them under the service's private `.agentcraft2/` state (or another path
  the repository already ignores) — never commit them. The checked-in
  `evals/cases.jsonl` and `evals/baseline.json` are safe to commit because
  their cases are synthetic test data, written by hand.
- Use test data locally; restrict access to these files the way you would
  restrict access to the model trace.
- Credential masking elsewhere in the library is best effort, not a
  substitute for keeping these files private — a question or answer that
  contains a secret is retained verbatim.

The same rule governs the traces themselves; what a Phoenix trace holds with
content capture on and off, and where local traces live, is in [Privacy and
content capture](phoenix-trial.md#privacy-and-content-capture).

`feedback.jsonl` and an exported interaction file can grow without bound as
real traffic accumulates; rotate or prune them the way you would any other
local log of user content.

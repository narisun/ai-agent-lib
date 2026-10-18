# Enterprise Review Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the requirements of
`docs/design/improvement-specification-2026-10-18.md` — offline evaluation
depth, online evaluation, HTTP service composition, quality gates, and
developer-experience fixes — without breaking any public API.

**Architecture:** All new evaluation code lives in
`ai_agent_lib_core/evaluation/` (off the request path, enforced by an existing
import-linter contract). Feedback capture and interaction export live in the
UI package, which writes versioned JSONL records that the core evaluation
module reads and scores. Service composition is an additive helper in
`ai_agent_lib_core/integrations/http/`. Quality gates are configuration-only
changes to the UI pyproject and CI.

**Tech Stack:** Python 3.11+, pydantic 2, pytest + pytest-asyncio (auto mode),
ruff, strict mypy, import-linter, Click (CLI), Starlette (`[serve]` extra),
Chainlit 2.12 (UI package, separate venv).

## Global Constraints

- Python floor: `>=3.11`. Line length 100. Google-style docstrings — ruff `D`
  rules are enforced on `src` (not tests).
- Strict mypy is on for core/AWS/CLI src **and** their tests. Annotate all test
  functions (`-> None`) and fixtures.
- Value types are `@dataclass(frozen=True, slots=True)`. Async-first. No
  environment reads outside `ai_agent_lib_core.config`. No `EAP_` literals
  outside `config` (an architecture test scans for them).
- New record formats carry a `"schema"` field: `agentlib.eval-report/v1`,
  `agentlib.feedback/v1`, `agentlib.interaction/v1`.
- Errors come from the existing taxonomy in `ai_agent_lib_core.contracts`:
  `ValidationFailed(message, expected=..., actual=..., fix=...)`,
  `ConfigurationError(...)`. Match the message style of
  `evaluation/report.py::require`.
- The main repo test run is `python -m pytest` from the repository root
  (sockets blocked except loopback). The UI package runs in its own
  environment: from `packages/ai-agent-lib-ui`, use
  `python ../../tools/package_runtime.py ui -m pytest` (and `... -m mypy`).
- Before each commit, the touched package must pass: `python -m ruff check .`,
  `python -m ruff format --check .`, `python -m mypy`, and the focused tests.
  Run `lint-imports` when imports changed.
- Do not rename or remove any existing public name. New names are added to the
  owning module's `__all__` and, for evaluation, to
  `ai_agent_lib_core/evaluation/__init__.py`.
- Commit style: short imperative subject, e.g. `eval: versioned report with
  metadata`. One commit per task.

---

## Phase 1 — Offline evaluation depth (spec R1)

### Task 1: Versioned eval report with metadata (R1.1)

**Files:**
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/report.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/run.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_evaluation_report_v1.py`

**Interfaces:**
- Produces: `REPORT_SCHEMA = "agentlib.eval-report/v1"` (module constant in
  `report.py`); `EvalReport.metadata: Mapping[str, str]` (new field, default
  empty); `run_eval(..., metadata: Mapping[str, str] | None = None)`.
- Consumed by: Task 4 (`load_report`), Task 6 (template), Task 10
  (`score_interactions`).

- [ ] **Step 1: Write the failing test**

```python
"""The eval report identifies itself and records where it came from."""

from __future__ import annotations

import json

from ai_agent_lib_core.evaluation import EvalCase, run_eval
from ai_agent_lib_core.evaluation.scorers import exact


async def _answer(case: EvalCase) -> dict[str, int]:
    return {"y": 1}


async def test_report_json_carries_schema_and_metadata() -> None:
    case = EvalCase(id="a", input={"q": "x"}, expected={"y": 1})
    report = await run_eval(_answer, [case], [exact("y")], metadata={"model": "fake"})
    data = json.loads(report.to_json())
    assert data["schema"] == "agentlib.eval-report/v1"
    assert data["metadata"] == {"model": "fake"}
    assert data["means"] == {"y": 1.0}


async def test_metadata_defaults_to_empty() -> None:
    case = EvalCase(id="a", input={}, expected={"y": 1})
    report = await run_eval(_answer, [case], [exact("y")])
    assert report.metadata == {}
    assert json.loads(report.to_json())["metadata"] == {}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_report_v1.py -q`
Expected: FAIL — `run_eval() got an unexpected keyword argument 'metadata'`.

- [ ] **Step 3: Implement**

In `report.py`: add the constant and the field, and extend `to_json`.

```python
REPORT_SCHEMA = "agentlib.eval-report/v1"
"""The identity of the JSON a report writes; load_report checks it."""
```

On `EvalReport`, after `metrics`:

```python
    metadata: Mapping[str, str] = field(default_factory=dict)
```

(Document it in the class docstring: *"metadata: Where the run came from —
model, dataset digest, application — for comparing runs."*) Keep `by_tag`
working by passing the field through:
`EvalReport(tuple(chosen), self.metrics, self.metadata)`.

In `to_json`, add two entries at the top of the serialized object:

```python
                "schema": REPORT_SCHEMA,
                "metadata": dict(self.metadata),
```

Add `REPORT_SCHEMA` to `report.py`'s `__all__`.

In `run.py`: add the keyword parameter and pass it through.

```python
async def run_eval(
    task: Callable[[EvalCase], Awaitable[Any]],
    cases: Sequence[EvalCase],
    scorers: Sequence[Scorer],
    *,
    concurrency: int = 4,
    metadata: Mapping[str, str] | None = None,
) -> EvalReport:
```

Final line: `return EvalReport(tuple(results), names, dict(metadata or {}))`.
Add `Mapping` to the imports from `collections.abc`. Document the parameter:
*"metadata: Where the run came from, kept on the report for comparison."*

In `evaluation/__init__.py`, export `REPORT_SCHEMA`.

- [ ] **Step 4: Run tests and the existing suite slice**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_report_v1.py packages/ai-agent-lib-core/tests/unit/test_evaluation.py -q`
Expected: PASS (existing evaluation tests must not break).

- [ ] **Step 5: Lint, types, commit**

```bash
python -m ruff check packages/ai-agent-lib-core && python -m ruff format --check packages/ai-agent-lib-core && python -m mypy
git add -A packages/ai-agent-lib-core
git commit -m "eval: versioned report schema with run metadata"
```

### Task 2: Dataset digest and per-case timeout (R1.2)

**Files:**
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/cases.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/run.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_evaluation_report_v1.py` (append)

**Interfaces:**
- Produces: `dataset_digest(path: Path) -> str` (returns `"sha256:" + 64 hex`);
  `run_eval(..., case_timeout: float | None = None)`.
- Consumed by: Task 6 (template), Task 10 (`score_interactions`).

- [ ] **Step 1: Write the failing tests (append to the Task 1 test file)**

```python
import asyncio
import hashlib
from pathlib import Path

from ai_agent_lib_core.evaluation import dataset_digest


def test_dataset_digest_is_the_sha256_of_the_bytes(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_bytes(b'{"input": {}}\n')
    expected = "sha256:" + hashlib.sha256(b'{"input": {}}\n').hexdigest()
    assert dataset_digest(cases) == expected


async def test_case_timeout_fails_only_the_slow_case() -> None:
    async def slow_or_fast(case: EvalCase) -> dict[str, int]:
        if case.id == "slow":
            await asyncio.Event().wait()
        return {"y": 1}

    cases = [
        EvalCase(id="slow", input={}, expected={"y": 1}),
        EvalCase(id="fast", input={}, expected={"y": 1}),
    ]
    report = await run_eval(slow_or_fast, cases, [exact("y")], case_timeout=0.05)
    by_id = {r.case.id: r for r in report.results}
    assert by_id["slow"].error_type == "TimeoutError"
    assert by_id["slow"].scores[0].value == 0.0
    assert by_id["fast"].scores[0].value == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_report_v1.py -q`
Expected: FAIL — `dataset_digest` not importable; `case_timeout` unexpected.

- [ ] **Step 3: Implement**

In `cases.py` (add `import hashlib` at the top, extend `__all__`):

```python
def dataset_digest(path: Path) -> str:
    """The dataset's identity for a report's metadata: sha256 over the file's bytes.

    Two reports are comparable only when they ran the same cases; the digest
    makes that checkable without keeping the file.
    """
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
```

In `run.py`, add the parameter after `metadata`:

```python
    case_timeout: float | None = None,
```

and bound the task inside `one`'s `try` block:

```python
                if case_timeout is None:
                    output = await task(case)
                else:
                    output = await asyncio.wait_for(task(case), case_timeout)
```

(`TimeoutError` is already caught by the existing `except Exception` and
reported by type name, which on 3.11+ is `"TimeoutError"`.) Document:
*"case_timeout: Seconds one case may take. A case over it scores 0 and is
reported as TimeoutError; None never interrupts."*

Export `dataset_digest` from `evaluation/__init__.py`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_report_v1.py packages/ai-agent-lib-core/tests/unit/test_evaluation.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, types, commit**

```bash
python -m ruff check packages/ai-agent-lib-core && python -m mypy
git add -A packages/ai-agent-lib-core
git commit -m "eval: dataset digest and per-case timeout"
```

### Task 3: Baselines and regression gating (R1.3)

**Files:**
- Create: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/baseline.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/__init__.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_evaluation_baseline.py`

**Interfaces:**
- Consumes: `EvalReport` (`.means`, `.metrics`, `.results`), `REPORT_SCHEMA`
  from Task 1.
- Produces: `SavedReport(means, case_scores, metadata)`;
  `load_report(path: Path) -> SavedReport`;
  `Regression(metric: str, baseline: float, current: float, cases: tuple[str, ...])`;
  `regressions(current: EvalReport, baseline: SavedReport, *, tolerance: float = 0.0) -> list[Regression]`;
  `require_no_regression(current: EvalReport, baseline: SavedReport, *, tolerance: float = 0.0) -> None`.
- Consumed by: Task 6 (template), Task 11 (docs).

- [ ] **Step 1: Write the failing tests**

```python
"""Comparing a run with its saved baseline."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_core.contracts import ValidationFailed
from ai_agent_lib_core.evaluation import (
    EvalCase,
    EvalReport,
    load_report,
    regressions,
    require_no_regression,
    run_eval,
)
from ai_agent_lib_core.evaluation.scorers import exact


async def _run(answers: dict[str, int]) -> EvalReport:
    async def task(case: EvalCase) -> dict[str, int]:
        return {"y": answers[case.id]}

    cases = [EvalCase(id=i, input={}, expected={"y": 1}) for i in answers]
    return await run_eval(task, cases, [exact("y")], metadata={"model": "fake"})


async def test_a_saved_report_round_trips(tmp_path: Path) -> None:
    report = await _run({"a": 1, "b": 0})
    path = tmp_path / "baseline.json"
    path.write_text(report.to_json(), encoding="utf-8")
    saved = load_report(path)
    assert saved.means == {"y": 0.5}
    assert saved.case_scores == {"a": {"y": 1.0}, "b": {"y": 0.0}}
    assert saved.metadata == {"model": "fake"}


async def test_an_equal_or_better_run_passes(tmp_path: Path) -> None:
    baseline = await _run({"a": 0, "b": 1})
    path = tmp_path / "baseline.json"
    path.write_text(baseline.to_json(), encoding="utf-8")
    current = await _run({"a": 1, "b": 1})
    assert regressions(current, load_report(path)) == []
    require_no_regression(current, load_report(path))


async def test_a_drop_beyond_tolerance_names_metric_and_cases(tmp_path: Path) -> None:
    baseline = await _run({"a": 1, "b": 1})
    path = tmp_path / "baseline.json"
    path.write_text(baseline.to_json(), encoding="utf-8")
    current = await _run({"a": 1, "b": 0})
    found = regressions(current, load_report(path), tolerance=0.1)
    assert [(r.metric, r.cases) for r in found] == [("y", ("b",))]
    with pytest.raises(ValidationFailed) as caught:
        require_no_regression(current, load_report(path), tolerance=0.1)
    assert "y" in str(caught.value)


def test_a_file_that_is_not_a_report_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_report(path)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_baseline.py -q`
Expected: FAIL — cannot import `load_report`.

- [ ] **Step 3: Implement `baseline.py`**

```python
"""Comparing one eval run with a saved baseline, and gating on regressions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ai_agent_lib_core.contracts import ValidationFailed
from ai_agent_lib_core.evaluation.report import REPORT_SCHEMA, EvalReport

__all__ = ["Regression", "SavedReport", "load_report", "regressions", "require_no_regression"]


@dataclass(frozen=True, slots=True)
class SavedReport:
    """An earlier run's results, read back from the JSON a report writes.

    Attributes:
        means: Each metric's mean over that run.
        case_scores: Each case's score per metric, by case ID.
        metadata: Where that run came from, as the run recorded it.
    """

    means: Mapping[str, float]
    case_scores: Mapping[str, Mapping[str, float]]
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Regression:
    """One metric that got worse than the baseline.

    Attributes:
        metric: The metric's name.
        baseline: Its mean in the baseline run.
        current: Its mean now.
        cases: The IDs, in both runs, whose score for the metric dropped.
    """

    metric: str
    baseline: float
    current: float
    cases: tuple[str, ...]


def load_report(path: Path) -> SavedReport:
    """Read a saved report. Accepts v1 and the earlier report without a schema field.

    Raises:
        ValidationFailed: If the file is unreadable, not a report, or another schema.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as problem:
        raise ValidationFailed(
            f"the baseline {path} cannot be read",
            expected="the JSON an eval writes with report.to_json()",
            actual=str(problem),
        ) from problem
    wrong = not isinstance(data, dict) or not isinstance(data.get("means"), dict)
    if wrong or data.get("schema", REPORT_SCHEMA) != REPORT_SCHEMA:
        raise ValidationFailed(
            f"the baseline {path} is not an eval report",
            expected=f'an object with "means" and schema {REPORT_SCHEMA}',
            actual="another shape or schema",
            fix="write the baseline with agentlib eval SERVICE --accept",
        )
    results = data.get("results", [])
    return SavedReport(
        means={str(k): float(v) for k, v in data["means"].items()},
        case_scores={
            str(r["id"]): {str(k): float(v) for k, v in r.get("scores", {}).items()}
            for r in results
            if isinstance(r, dict) and "id" in r
        },
        metadata={str(k): str(v) for k, v in data.get("metadata", {}).items()},
    )


def regressions(
    current: EvalReport, baseline: SavedReport, *, tolerance: float = 0.0
) -> list[Regression]:
    """The metrics whose mean dropped more than ``tolerance`` since ``baseline``.

    A metric present in only one of the runs is not compared. ``cases`` lists
    the IDs present in both runs whose own score dropped more than ``tolerance``.
    """
    current_cases = {r.case.id: {s.name: s.value for s in r.scores} for r in current.results}
    found: list[Regression] = []
    for metric in current.metrics:
        if metric not in baseline.means:
            continue
        before, now = baseline.means[metric], current.means[metric]
        if now >= before - tolerance:
            continue
        dropped = tuple(
            case_id
            for case_id, scores in sorted(current_cases.items())
            if metric in scores
            and metric in baseline.case_scores.get(case_id, {})
            and scores[metric] < baseline.case_scores[case_id][metric] - tolerance
        )
        found.append(Regression(metric, before, now, dropped))
    return found


def require_no_regression(
    current: EvalReport, baseline: SavedReport, *, tolerance: float = 0.0
) -> None:
    """Raise unless no metric regressed: the second assertion an eval test ends with.

    Raises:
        ValidationFailed: Naming each regressed metric, both means and the
            dropped cases.
    """
    found = regressions(current, baseline, tolerance=tolerance)
    if not found:
        return
    raise ValidationFailed(
        "the eval regressed against its baseline on " + ", ".join(r.metric for r in found),
        expected="; ".join(f"{r.metric} >= {r.baseline - tolerance:.2f}" for r in found),
        actual="; ".join(
            f"{r.metric} = {r.current:.2f} (was {r.baseline:.2f}; dropped cases: "
            f"{', '.join(r.cases[:10]) or 'none listed'})"
            for r in found
        ),
        fix="fix the regression, or accept the new report: agentlib eval SERVICE --accept",
    )
```

Export all five names from `evaluation/__init__.py` and add them to its
`__all__` (keep the list sorted).

- [ ] **Step 4: Run tests**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_baseline.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, types, commit**

```bash
python -m ruff check packages/ai-agent-lib-core && python -m mypy
git add -A packages/ai-agent-lib-core
git commit -m "eval: baselines, regression detection and gating"
```

### Task 4: Judge calibration (R1.4)

**Files:**
- Create: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/calibration.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/__init__.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_evaluation_calibration.py`

**Interfaces:**
- Consumes: `EvalCase`, `Scorer`, `Score`.
- Produces: `LabeledCase(case: EvalCase, output: Any, human: float)`;
  `CalibrationReport(cases: int, mean_absolute_error: float, agreement: float,
  disagreements: tuple[tuple[str, float, float], ...])` with
  `summary() -> list[str]`;
  `calibrate(scorer: Scorer, labeled: Sequence[LabeledCase], *, within: float = 0.25) -> CalibrationReport`.

- [ ] **Step 1: Write the failing tests**

```python
"""Checking a judge against answers a person has graded."""

from __future__ import annotations

import pytest

from ai_agent_lib_core.evaluation import EvalCase, LabeledCase, calibrate
from ai_agent_lib_core.evaluation.scorers import Score, Scorer


def _judge(value_for: dict[str, float]) -> Scorer:
    class ByCase:
        name = "judged"

        async def __call__(self, case: EvalCase, output: object) -> Score:
            return Score("judged", value_for[case.id])

    return ByCase()


def _labeled(human: dict[str, float]) -> list[LabeledCase]:
    return [
        LabeledCase(EvalCase(id=i, input={}), output="answer", human=grade)
        for i, grade in human.items()
    ]


async def test_a_perfect_judge_agrees_everywhere() -> None:
    labeled = _labeled({"a": 1.0, "b": 0.0})
    report = await calibrate(_judge({"a": 1.0, "b": 0.0}), labeled)
    assert report.cases == 2
    assert report.mean_absolute_error == 0.0
    assert report.agreement == 1.0
    assert report.disagreements == ()


async def test_an_inverted_judge_disagrees_everywhere() -> None:
    labeled = _labeled({"a": 1.0, "b": 0.0})
    report = await calibrate(_judge({"a": 0.0, "b": 1.0}), labeled)
    assert report.agreement == 0.0
    assert report.disagreements == (("a", 1.0, 0.0), ("b", 0.0, 1.0))
    assert any("disagree" in line for line in report.summary())


async def test_calibration_needs_at_least_one_case() -> None:
    with pytest.raises(ValueError, match="at least one"):
        await calibrate(_judge({}), [])


def test_a_human_grade_is_a_number_from_0_to_1() -> None:
    with pytest.raises(ValueError):
        LabeledCase(EvalCase(id="a", input={}), output="x", human=1.5)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_calibration.py -q`
Expected: FAIL — cannot import `LabeledCase`.

- [ ] **Step 3: Implement `calibration.py`**

```python
"""Checking a judge against answers a person has graded, before trusting it."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean
from typing import Any

from ai_agent_lib_core.evaluation.cases import EvalCase
from ai_agent_lib_core.evaluation.scorers import Scorer

__all__ = ["CalibrationReport", "LabeledCase", "calibrate"]


@dataclass(frozen=True, slots=True)
class LabeledCase:
    """One answer a person has graded.

    Attributes:
        case: The case the answer was for.
        output: The answer that was graded.
        human: The person's grade, from 0, wrong, to 1, right.
    """

    case: EvalCase
    output: Any
    human: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.human) or not 0.0 <= self.human <= 1.0:
            raise ValueError(
                f"the case {self.case.id!r} has the human grade {self.human!r}; "
                "a grade is a number from 0 to 1"
            )


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """How closely a judge tracks the person's grades.

    Attributes:
        cases: How many labeled cases were checked.
        mean_absolute_error: The mean gap between the judge and the person.
        agreement: The fraction of cases where the gap is within the bound.
        disagreements: The cases outside the bound: ID, human grade, judge score.
    """

    cases: int
    mean_absolute_error: float
    agreement: float
    disagreements: tuple[tuple[str, float, float], ...]

    def summary(self) -> list[str]:
        """A few lines for a terminal: the error, the agreement, the disagreements."""
        lines = [
            f"{self.cases} labeled cases",
            f"  mean absolute error  {self.mean_absolute_error:.2f}",
            f"  agreement            {self.agreement:.0%}",
        ]
        lines += [
            f"  disagrees on {case_id}: human {human:.2f}, judge {judge:.2f}"
            for case_id, human, judge in self.disagreements
        ]
        return lines


async def calibrate(
    scorer: Scorer, labeled: Sequence[LabeledCase], *, within: float = 0.25
) -> CalibrationReport:
    """Run ``scorer`` over graded answers and report how closely it agrees.

    Use it before trusting a judged metric: a judge that disagrees with people
    measures the judge, not the agent.

    Args:
        scorer: The judge being checked, usually built with :func:`judged`.
        labeled: The graded answers.
        within: How far the judge may sit from the person and still agree.

    Raises:
        ValueError: With no labeled cases; a calibration over nothing proves nothing.
    """
    if not labeled:
        raise ValueError("calibration needs at least one labeled case")
    gaps: list[float] = []
    disagreements: list[tuple[str, float, float]] = []
    for item in labeled:
        score = await scorer(item.case, item.output)
        gap = abs(score.value - item.human)
        gaps.append(gap)
        if gap > within:
            disagreements.append((item.case.id, item.human, score.value))
    return CalibrationReport(
        cases=len(labeled),
        mean_absolute_error=mean(gaps),
        agreement=1 - len(disagreements) / len(labeled),
        disagreements=tuple(disagreements),
    )
```

Export `LabeledCase`, `CalibrationReport`, `calibrate` from
`evaluation/__init__.py`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_calibration.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, types, commit**

```bash
python -m ruff check packages/ai-agent-lib-core && python -m mypy
git add -A packages/ai-agent-lib-core
git commit -m "eval: judge calibration against human grades"
```

### Task 5: `agentlib eval --accept` (R1.5, CLI half)

**Files:**
- Modify: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/commands/check.py`
- Test: `packages/ai-agent-lib-cli/tests/unit/test_cli_check.py` (append; follow
  the file's existing fixtures for building a workspace and stubbing the
  `Toolbox` — read the file before writing)

**Interfaces:**
- Produces: `agentlib eval SERVICE --accept` copies
  `<service>/.agentlib/eval-report.json` to `<service>/evals/baseline.json`
  after the evals pass.

- [ ] **Step 1: Read the existing test file's helpers**

Read `packages/ai-agent-lib-cli/tests/unit/test_cli_check.py` and note how it
builds a workspace and invokes commands (CliRunner and any Toolbox stub). The
tests below must use the same helpers; adapt invocation details to them.

- [ ] **Step 2: Write the failing tests**

Behavioral content (adapt setup to the file's fixtures):

```python
def test_eval_accept_promotes_the_report_to_the_baseline(...) -> None:
    # Arrange a workspace with service "helper", a passing stubbed pytest run,
    # and a report at agents/helper/.agentlib/eval-report.json containing
    # '{"schema": "agentlib.eval-report/v1", "means": {"m": 1.0}, "results": [], "metadata": {}}'.
    result = runner.invoke(cli, ["eval", "helper", "--accept", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    baseline = root / "agents" / "helper" / "evals" / "baseline.json"
    assert json.loads(baseline.read_text(encoding="utf-8"))["means"] == {"m": 1.0}


def test_eval_accept_without_a_service_is_refused(...) -> None:
    result = runner.invoke(cli, ["eval", "--accept", "--workspace", str(root)])
    assert result.exit_code != 0
    assert "--accept needs a service" in result.output


def test_eval_accept_without_a_report_names_the_fix(...) -> None:
    # Same arrangement, but no eval-report.json on disk.
    result = runner.invoke(cli, ["eval", "helper", "--accept", "--workspace", str(root)])
    assert result.exit_code != 0
    assert "eval-report.json" in result.output
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest packages/ai-agent-lib-cli/tests/unit/test_cli_check.py -q`
Expected: the new tests FAIL — `--accept` is not a known option.

- [ ] **Step 4: Implement in `commands/check.py`**

Add the option and logic to the existing `evals` command:

```python
@click.command("eval")
@click.argument("service", required=False)
@click.option(
    "--accept",
    is_flag=True,
    help="After the evals pass, save the report as the service's evals/baseline.json.",
)
@workspace_option
@pass_toolbox
def evals(tools: Toolbox, service: str | None, accept: bool, workspace: Path | None) -> None:
```

Before running pytest:

```python
    if accept and not service:
        raise CliError("--accept needs a service", fix="agentlib eval SERVICE --accept")
```

After the existing success path (after the `code != 0` check):

```python
    if accept:
        report = target / ".agentlib" / "eval-report.json"
        if not report.is_file():
            raise CliError(
                f"there is no report at {report.relative_to(root).as_posix()}",
                fix="run the evals once without --accept, or check the service's eval test",
            )
        baseline = target / "evals" / "baseline.json"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(report.read_bytes())
        click.echo(f"baseline accepted: {baseline.relative_to(root).as_posix()}")
```

Extend the command docstring with one line: *"--accept saves the passing
report as evals/baseline.json; later runs fail on regressions against it."*

- [ ] **Step 5: Run tests, lint, commit**

```bash
python -m pytest packages/ai-agent-lib-cli/tests/unit/test_cli_check.py -q
python -m ruff check packages/ai-agent-lib-cli && python -m mypy
git add -A packages/ai-agent-lib-cli
git commit -m "cli: agentlib eval --accept promotes the report to the baseline"
```

### Task 6: Template eval test uses metadata, timeout and baseline (R1.5, template half)

**Files:**
- Modify: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/agent/tests/test___package___eval.py.jinja`
- Test: the CLI's generated-workspace suites,
  `packages/ai-agent-lib-cli/tests/generated/`

**Interfaces:**
- Consumes: `dataset_digest`, `load_report`, `require_no_regression`,
  `run_eval(..., case_timeout=..., metadata=...)` from Tasks 1–3.

- [ ] **Step 1: Edit the template**

Change the imports line to:

```python
from ai_agent_lib_core.evaluation import (
    EvalCase,
    dataset_digest,
    load_cases,
    load_report,
    require_no_regression,
    run_eval,
    scored_by,
)
```

Add beside `REPORT`:

```python
BASELINE = SERVICE / "evals" / "baseline.json"
```

Change the `run_eval` call to:

```python
        report = await run_eval(
            ask,
            load_cases(CASES),
            [scored_by("mentions", mentions)],
            case_timeout=120.0,
            metadata={
                "application": APPLICATION,
                "model": config.model.provider,
                "dataset": dataset_digest(CASES),
            },
        )
```

After `print("\n".join(report.summary()))` and before `report.require(...)`:

```python
    if BASELINE.exists():
        # A passing run may still be worse than the last accepted one.
        require_no_regression(report, load_report(BASELINE), tolerance=0.05)
```

Extend the module docstring with: *"When evals/baseline.json exists — written
by `agentlib eval {{ name }} --accept` — the run also fails on regressions
against it."*

- [ ] **Step 2: Regenerate and run the generated-workspace tests**

Run: `python -m pytest packages/ai-agent-lib-cli/tests/generated -q`
Expected: PASS — generation succeeds and the generated test still compiles and
skips on the fake model. If a generated-file inventory test lists template
outputs, update its expectation for the changed file.

- [ ] **Step 3: Full CLI suite, lint, commit**

```bash
python -m pytest packages/ai-agent-lib-cli -q
python -m ruff check packages/ai-agent-lib-cli && python -m mypy
git add -A packages/ai-agent-lib-cli
git commit -m "cli: generated evals carry provenance, timeout and a baseline gate"
```

---

## Phase 2 — Online evaluation (spec R2)

### Task 7: Feedback records and JSONL log (R2.1)

**Files:**
- Create: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/feedback.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/__init__.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_evaluation_feedback.py`

**Interfaces:**
- Produces: `FEEDBACK_SCHEMA = "agentlib.feedback/v1"`;
  `FeedbackRecord(request_id, thread_id, application, subject, verdict, comment="", created_at="")`
  with `verdict: Literal["up", "down"]`;
  `JsonlFeedbackLog(path)` with `async def write(record: FeedbackRecord) -> None`;
  `load_feedback(path: Path) -> list[FeedbackRecord]`.
- Consumed by: Task 9 (UI capture), Task 10 (joining feedback onto
  interactions).

- [ ] **Step 1: Write the failing tests**

```python
"""Feedback records: append-only evidence of a person's judgement."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_core.contracts import ValidationFailed
from ai_agent_lib_core.evaluation import FeedbackRecord, JsonlFeedbackLog, load_feedback


def _record(verdict: str = "up") -> FeedbackRecord:
    return FeedbackRecord(
        request_id="r-1",
        thread_id="t-1",
        application="helper",
        subject="u-1",
        verdict=verdict,  # type: ignore[arg-type]
        comment="good",
        created_at="2026-10-18T00:00:00+00:00",
    )


async def test_feedback_round_trips_through_the_log(tmp_path: Path) -> None:
    log = JsonlFeedbackLog(tmp_path / "state" / "feedback.jsonl")
    await log.write(_record("up"))
    await log.write(_record("down"))
    records = load_feedback(tmp_path / "state" / "feedback.jsonl")
    assert [r.verdict for r in records] == ["up", "down"]
    assert records[0].request_id == "r-1"
    assert records[0].comment == "good"


def test_a_line_with_another_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "feedback.jsonl"
    path.write_text('{"schema": "other/v1", "verdict": "up"}\n', encoding="utf-8")
    with pytest.raises(ValidationFailed, match="line 1"):
        load_feedback(path)


def test_a_verdict_is_up_or_down(tmp_path: Path) -> None:
    path = tmp_path / "feedback.jsonl"
    path.write_text(
        '{"schema": "agentlib.feedback/v1", "request_id": "r", "thread_id": "t",'
        ' "application": "a", "subject": "s", "verdict": "sideways"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="verdict"):
        load_feedback(path)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_feedback.py -q`
Expected: FAIL — cannot import `FeedbackRecord`.

- [ ] **Step 3: Implement `feedback.py`**

```python
"""A person's judgement of one production answer, as append-only evidence."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from ai_agent_lib_core.contracts import ValidationFailed

__all__ = ["FEEDBACK_SCHEMA", "FeedbackRecord", "JsonlFeedbackLog", "load_feedback"]

FEEDBACK_SCHEMA = "agentlib.feedback/v1"
"""The identity each feedback line carries; load_feedback checks it."""


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """One judgement: who found which answer good or bad.

    Attributes:
        request_id: The judged request, as the agent reported it.
        thread_id: The conversation the request belongs to.
        application: The agent that answered.
        subject: Who judged, from the verified principal.
        verdict: ``up`` or ``down``.
        comment: What the person said, when they said anything.
        created_at: When, as ISO 8601 text from the caller's clock. The record
            never reads a clock itself.
    """

    request_id: str
    thread_id: str
    application: str
    subject: str
    verdict: Literal["up", "down"]
    comment: str = ""
    created_at: str = ""


class JsonlFeedbackLog:
    """Append feedback records to a file, one JSON object per line.

    Writes are serialized and run outside the event loop. Parent directories
    are created on the first write. The file holds conversation content; keep
    it beside the service's other private state.

    Args:
        path: Destination file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    async def write(self, record: FeedbackRecord) -> None:
        """Append one record."""
        line = json.dumps({"schema": FEEDBACK_SCHEMA, **asdict(record)}, ensure_ascii=False)
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")


def load_feedback(path: Path) -> list[FeedbackRecord]:
    """Read every feedback record in a JSONL file. Blank lines are skipped.

    Raises:
        ValidationFailed: If a line is not JSON, carries another schema, or has
            a verdict other than up or down. The message names the line.
    """
    records: list[FeedbackRecord] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name}, line {number}"
        try:
            data = json.loads(line)
        except json.JSONDecodeError as problem:
            raise ValidationFailed(
                f"{where} is not JSON",
                expected="one feedback object per line",
                actual=str(problem),
            ) from problem
        if not isinstance(data, dict) or data.get("schema") != FEEDBACK_SCHEMA:
            raise ValidationFailed(
                f"{where} is not a feedback record",
                expected=f"schema {FEEDBACK_SCHEMA}",
                actual=f"schema {data.get('schema')!r}" if isinstance(data, dict) else "not an object",
            )
        verdict = data.get("verdict")
        if verdict not in ("up", "down"):
            raise ValidationFailed(
                f"{where} has no verdict",
                expected='"up" or "down"',
                actual=repr(verdict),
            )
        records.append(
            FeedbackRecord(
                request_id=str(data.get("request_id", "")),
                thread_id=str(data.get("thread_id", "")),
                application=str(data.get("application", "")),
                subject=str(data.get("subject", "")),
                verdict=verdict,
                comment=str(data.get("comment", "")),
                created_at=str(data.get("created_at", "")),
            )
        )
    return records
```

Export `FEEDBACK_SCHEMA`, `FeedbackRecord`, `JsonlFeedbackLog`,
`load_feedback` from `evaluation/__init__.py`.

- [ ] **Step 4: Run tests, lint, commit**

```bash
python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_feedback.py -q
python -m ruff check packages/ai-agent-lib-core && python -m mypy
git add -A packages/ai-agent-lib-core
git commit -m "eval: versioned feedback records and JSONL log"
```

### Task 8: Interactions, sampling and online scoring (R2.2)

**Files:**
- Create: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/online.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/evaluation/__init__.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_evaluation_online.py`

**Interfaces:**
- Consumes: `run_eval`, `EvalCase`, `EvalReport`, `Scorer` from Phase 1.
- Produces: `INTERACTION_SCHEMA = "agentlib.interaction/v1"`;
  `Interaction(request_id, thread_id, question, answer, application="", verdict="")`;
  `write_interactions(path: Path, interactions: Sequence[Interaction]) -> None`;
  `load_interactions(path: Path) -> list[Interaction]`;
  `sampled(interactions: Sequence[Interaction], *, rate: float, salt: str = "") -> list[Interaction]`;
  `as_cases(interactions: Sequence[Interaction]) -> list[EvalCase]`;
  `score_interactions(interactions, scorers, *, concurrency=4, case_timeout=None, metadata=None) -> EvalReport`.
- Consumed by: Task 10 (UI export writes this format), Task 11 (docs).

- [ ] **Step 1: Write the failing tests**

```python
"""Scoring recorded production exchanges, off the request path."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core.evaluation import (
    EvalCase,
    Interaction,
    load_interactions,
    sampled,
    score_interactions,
    write_interactions,
)
from ai_agent_lib_core.evaluation.scorers import Score, Scorer


def _interaction(number: int, verdict: str = "") -> Interaction:
    return Interaction(
        request_id=f"r-{number}",
        thread_id="t-1",
        question=f"question {number}",
        answer=f"answer {number}",
        application="helper",
        verdict=verdict,
    )


def test_interactions_round_trip_through_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "interactions.jsonl"
    given = [_interaction(1), _interaction(2, verdict="down")]
    write_interactions(path, given)
    assert load_interactions(path) == given


def test_sampling_is_deterministic_and_rate_bound() -> None:
    interactions = [_interaction(n) for n in range(200)]
    first = sampled(interactions, rate=0.25, salt="week-42")
    second = sampled(interactions, rate=0.25, salt="week-42")
    assert first == second
    assert 20 <= len(first) <= 80  # a quarter, within loose bounds
    assert sampled(interactions, rate=1.0) == interactions
    assert sampled(interactions, rate=0.0) == []


async def test_scoring_judges_the_recorded_answers() -> None:
    class AnswerLength:
        name = "short"

        async def __call__(self, case: EvalCase, output: object) -> Score:
            return Score("short", 1.0 if len(str(output)) < 20 else 0.0)

    scorer: Scorer = AnswerLength()
    report = await score_interactions(
        [_interaction(1), _interaction(2, verdict="down")],
        [scorer],
        metadata={"source": "test"},
    )
    assert [r.case.id for r in report.results] == ["r-1", "r-2"]
    assert report.means == {"short": 1.0}
    assert report.by_tag("down") == {"short": 1.0}
    assert report.metadata == {"source": "test"}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_online.py -q`
Expected: FAIL — cannot import `Interaction`.

- [ ] **Step 3: Implement `online.py`**

```python
"""Scoring what production already answered: records, sampling and the runner.

Capture is on the request path and append-only; scoring is here, in a separate
process, with the same scorers offline evals use. The task given to
:func:`run_eval` only returns the recorded answer — the scorers, typically
:func:`~ai_agent_lib_core.evaluation.judged`, do any model calls.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_agent_lib_core.contracts import ValidationFailed
from ai_agent_lib_core.evaluation.cases import EvalCase
from ai_agent_lib_core.evaluation.report import EvalReport
from ai_agent_lib_core.evaluation.run import run_eval
from ai_agent_lib_core.evaluation.scorers import Scorer

__all__ = [
    "INTERACTION_SCHEMA",
    "Interaction",
    "as_cases",
    "load_interactions",
    "sampled",
    "score_interactions",
    "write_interactions",
]

INTERACTION_SCHEMA = "agentlib.interaction/v1"
"""The identity each interaction line carries; load_interactions checks it."""


@dataclass(frozen=True, slots=True)
class Interaction:
    """One production exchange: what was asked and what was answered.

    Attributes:
        request_id: The request, as the agent reported it.
        thread_id: The conversation the request belongs to.
        question: What was asked.
        answer: What the agent answered.
        application: The agent that answered.
        verdict: ``up`` or ``down`` when a person judged it; empty otherwise.
    """

    request_id: str
    thread_id: str
    question: str
    answer: str
    application: str = ""
    verdict: str = ""


def write_interactions(path: Path, interactions: Sequence[Interaction]) -> None:
    """Write interactions as JSONL, one object per line, replacing the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"schema": INTERACTION_SCHEMA, **asdict(item)}, ensure_ascii=False)
        for item in interactions
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load_interactions(path: Path) -> list[Interaction]:
    """Read every interaction in a JSONL file. Blank lines are skipped.

    Raises:
        ValidationFailed: If a line is not JSON or carries another schema.
            The message names the line.
    """
    interactions: list[Interaction] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name}, line {number}"
        try:
            data = json.loads(line)
        except json.JSONDecodeError as problem:
            raise ValidationFailed(
                f"{where} is not JSON",
                expected="one interaction object per line",
                actual=str(problem),
            ) from problem
        if not isinstance(data, dict) or data.get("schema") != INTERACTION_SCHEMA:
            raise ValidationFailed(
                f"{where} is not an interaction",
                expected=f"schema {INTERACTION_SCHEMA}",
                actual=f"schema {data.get('schema')!r}" if isinstance(data, dict) else "not an object",
            )
        interactions.append(
            Interaction(
                request_id=str(data.get("request_id", "")),
                thread_id=str(data.get("thread_id", "")),
                question=str(data.get("question", "")),
                answer=str(data.get("answer", "")),
                application=str(data.get("application", "")),
                verdict=str(data.get("verdict", "")),
            )
        )
    return interactions


def sampled(
    interactions: Sequence[Interaction], *, rate: float, salt: str = ""
) -> list[Interaction]:
    """A deterministic sample: the same inputs select the same interactions anywhere.

    An interaction is chosen by hashing ``salt`` and its request ID, so a rerun,
    another machine, or a second scorer sees the identical sample. Change the
    salt to draw a different sample at the same rate.

    Args:
        interactions: What production recorded.
        rate: The fraction to keep, from 0 to 1.
        salt: Varies the draw without touching the data.
    """
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"the sample rate is a number from 0 to 1, not {rate!r}")

    threshold = rate * 0x100000000  # rate 1.0 keeps every draw, 0.0 keeps none

    def chosen(item: Interaction) -> bool:
        digest = hashlib.sha256((salt + item.request_id).encode("utf-8")).hexdigest()
        return int(digest[:8], 16) < threshold

    return [item for item in interactions if chosen(item)]


def as_cases(interactions: Sequence[Interaction]) -> list[EvalCase]:
    """Turn interactions into cases: the request ID names the case, the verdict tags it."""
    return [
        EvalCase(
            id=item.request_id,
            input={"question": item.question},
            expected={},
            tags=(item.verdict,) if item.verdict else (),
        )
        for item in interactions
    ]


async def score_interactions(
    interactions: Sequence[Interaction],
    scorers: Sequence[Scorer],
    *,
    concurrency: int = 4,
    case_timeout: float | None = None,
    metadata: Mapping[str, str] | None = None,
) -> EvalReport:
    """Score recorded answers with ``scorers`` and return an ordinary eval report.

    The report gates like an offline run: ``require`` for bars,
    ``require_no_regression`` against a saved baseline, ``by_tag("down")`` for
    the answers people rejected.
    """
    answers = {item.request_id: item.answer for item in interactions}

    async def recorded(case: EvalCase) -> str:
        return answers[case.id]

    return await run_eval(
        recorded,
        as_cases(interactions),
        scorers,
        concurrency=concurrency,
        case_timeout=case_timeout,
        metadata=metadata,
    )
```

Export the seven new names from `evaluation/__init__.py`.

- [ ] **Step 4: Run tests, lint, commit**

```bash
python -m pytest packages/ai-agent-lib-core/tests/unit/test_evaluation_online.py -q
python -m ruff check packages/ai-agent-lib-core && python -m mypy && lint-imports
git add -A packages/ai-agent-lib-core
git commit -m "eval: interaction records, deterministic sampling and online scoring"
```

### Task 9: Agent Lab feedback capture (R2.3)

All commands in this task run from `packages/ai-agent-lib-ui` with the UI
environment: `python ../../tools/package_runtime.py ui -m pytest` and
`python ../../tools/package_runtime.py ui -m mypy`.

**Files:**
- Create: `packages/ai-agent-lib-ui/src/ai_agent_lib_ui/contracts/feedback.py`
- Modify: `packages/ai-agent-lib-ui/src/ai_agent_lib_ui/integrations/runtime.py`
- Modify: `packages/ai-agent-lib-ui/src/ai_agent_lib_ui/integrations/data_layer.py`
- Test: `packages/ai-agent-lib-ui/tests/test_feedback.py`

**Interfaces:**
- Consumes: `FeedbackRecord`, `JsonlFeedbackLog` from Task 7; the existing
  `answer_id(run)` and `runtime()` in the UI package.
- Produces: `FeedbackLog` protocol (`async def write(record: FeedbackRecord)
  -> None`) in UI contracts; `Runtime.feedback: FeedbackLog`;
  `DataLayer.upsert_feedback` stores a record instead of raising.

- [ ] **Step 1: Investigate the exact seams (do not skip)**

Read these files and answer each question before coding:
1. `integrations/runtime.py` — how `compose(settings, state)` builds the
   `Runtime` and where the state directory is, so `feedback` can default to
   `JsonlFeedbackLog(state / "feedback.jsonl")`.
2. `integrations/data_layer.py` — confirm the pinned Chainlit `Feedback` type's
   fields (`forId`, `value`, `comment`, `threadId`) in the installed
   `chainlit==2.12.0`, and how existing methods load a thread's runs (the
   pattern used by `get_thread`).
3. `contracts/conversations.py` — the field on `RequestRecord`/`RunRecord`
   carrying the request ID, and how runs are read from a `Conversation` or via
   `store.history`.
4. `tests/` — the existing fixture style for a fake store and principal
   (`test_chat.py`, `test_chainlit_compat.py`) and reuse it.

- [ ] **Step 2: Write the failing tests**

Behavioral content (adapt arrangement to the fixtures found in Step 1):

```python
async def test_feedback_on_an_accepted_answer_is_recorded(...) -> None:
    # Arrange: a runtime whose store holds one completed run; a JsonlFeedbackLog
    # writing to tmp_path / "feedback.jsonl"; Chainlit Feedback with
    # forId=answer_id(run), value=1, comment="helpful", threadId=<thread id>.
    await data_layer.upsert_feedback(feedback)
    records = load_feedback(tmp_path / "feedback.jsonl")
    assert [r.verdict for r in records] == ["up"]
    assert records[0].request_id == run.request_id
    assert records[0].thread_id == thread_id
    assert records[0].comment == "helpful"
    assert records[0].subject  # the signed-in user, never empty


async def test_value_zero_is_a_down_verdict(...) -> None:
    await data_layer.upsert_feedback(feedback_with_value_0)
    assert load_feedback(path)[0].verdict == "down"


async def test_feedback_for_an_unknown_step_is_refused(...) -> None:
    with pytest.raises(HTTPException) as caught:
        await data_layer.upsert_feedback(feedback_with_unknown_for_id)
    assert caught.value.status_code == 404
    assert not path.exists()


async def test_delete_feedback_stays_refused(...) -> None:
    with pytest.raises(HTTPException):
        await data_layer.delete_feedback("any")
```

- [ ] **Step 3: Run to verify failure**

Run (from `packages/ai-agent-lib-ui`):
`python ../../tools/package_runtime.py ui -m pytest tests/test_feedback.py -q`
Expected: FAIL — today `upsert_feedback` raises 404 unconditionally.

- [ ] **Step 4: Implement**

`contracts/feedback.py` (UI contracts must not import integrations/adapters):

```python
"""Where a viewer's judgement of an answer is kept."""

from typing import Protocol

from ai_agent_lib_core.evaluation.feedback import FeedbackRecord

__all__ = ["FeedbackLog", "FeedbackRecord"]


class FeedbackLog(Protocol):
    """Appends judgements; never reads them back on the request path."""

    async def write(self, record: FeedbackRecord) -> None:
        """Append one record."""
        ...
```

`integrations/runtime.py`: add a `feedback: FeedbackLog` member to `Runtime`
and build it in `compose` as
`JsonlFeedbackLog(state / "feedback.jsonl")` (import from
`ai_agent_lib_core.evaluation.feedback`).

`integrations/data_layer.py`: replace the two raising methods:

```python
    async def upsert_feedback(self, feedback: Feedback) -> str:
        """Record a judgement of an accepted answer as append-only evidence."""
        principal = current_principal()
        if not feedback.threadId:
            raise HTTPException(404, "Feedback needs a thread")
        conversation = await runtime().store.get(principal, feedback.threadId)
        run = next(
            (r for r in _completed_runs(conversation) if answer_id(r) == feedback.forId),
            None,
        )
        if run is None:
            raise HTTPException(404, "Feedback target not found")
        record = FeedbackRecord(
            request_id=run.request_id,
            thread_id=feedback.threadId,
            application=conversation.agent_id,
            subject=principal.subject,
            verdict="up" if feedback.value else "down",
            comment=feedback.comment or "",
            created_at=now().isoformat(),
        )
        await runtime().feedback.write(record)
        return answer_id(run)

    async def delete_feedback(self, feedback_id: str) -> bool:
        """Keep judgements append-only; recorded evidence is never unrecorded."""
        raise HTTPException(404, "Feedback cannot be deleted")
```

Adjust `_completed_runs(conversation)`, `run.request_id`, and
`conversation.agent_id` to the real names found in Step 1 — use exactly the
access pattern `get_thread` already uses to enumerate a thread's runs, and
take the application from the same source `get_thread` uses for the agent.
Mypy for `data_layer` allows untyped Chainlit calls per the existing override.

- [ ] **Step 5: Run the UI suite, types, lint, commit**

```bash
cd packages/ai-agent-lib-ui
python ../../tools/package_runtime.py ui -m pytest -q
python ../../tools/package_runtime.py ui -m mypy
cd ../.. && python -m ruff check packages/ai-agent-lib-ui
git add -A packages/ai-agent-lib-ui
git commit -m "ui: capture answer feedback as agentlib.feedback/v1 records"
```

### Task 10: Agent Lab interaction export (R2.4)

Same environment rules as Task 9.

**Files:**
- Create: `packages/ai-agent-lib-ui/src/ai_agent_lib_ui/adapters/interactions.py`
- Test: `packages/ai-agent-lib-ui/tests/test_interactions_export.py`

**Interfaces:**
- Consumes: `Interaction`, `write_interactions` from Task 8; `load_feedback`
  from Task 7; the UI conversation store's thread/run read API (found in
  Task 9 Step 1).
- Produces: `export_interactions(store, principal, out: Path, *,
  feedback_path: Path | None = None) -> int` — writes
  `agentlib.interaction/v1` JSONL for every completed run the principal owns
  and returns the record count; verdicts joined from feedback by
  `request_id`, the latest line winning.

- [ ] **Step 1: Write the failing tests**

Behavioral content (same fake-store fixtures as Task 9):

```python
async def test_export_writes_one_interaction_per_completed_run(tmp_path, ...) -> None:
    # Arrange: a store with thread t-1 holding a completed run (question/answer)
    # and a failed run; no feedback file.
    count = await export_interactions(store, principal, tmp_path / "out.jsonl")
    assert count == 1
    [item] = load_interactions(tmp_path / "out.jsonl")
    assert item.question and item.answer
    assert item.verdict == ""


async def test_export_joins_feedback_by_request_id(tmp_path, ...) -> None:
    # Arrange: one completed run; a feedback.jsonl holding a "down" verdict
    # for its request_id written with JsonlFeedbackLog.
    count = await export_interactions(
        store, principal, tmp_path / "out.jsonl", feedback_path=feedback_file
    )
    assert count == 1
    [item] = load_interactions(tmp_path / "out.jsonl")
    assert item.verdict == "down"
```

- [ ] **Step 2: Run to verify failure**

Run: `python ../../tools/package_runtime.py ui -m pytest tests/test_interactions_export.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `adapters/interactions.py`**

Shape (adapt store calls to the API found in Task 9 Step 1 — list the
principal's threads, then read each thread's turns):

```python
"""Export saved conversations as interaction records for online evals."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core.contracts import Principal
from ai_agent_lib_core.evaluation.feedback import load_feedback
from ai_agent_lib_core.evaluation.online import Interaction, write_interactions

__all__ = ["export_interactions"]


async def export_interactions(
    store: ConversationStore,
    principal: Principal,
    out: Path,
    *,
    feedback_path: Path | None = None,
) -> int:
    """Write every completed exchange the principal owns as interaction JSONL.

    Feedback, when a file is given, is joined by request ID; the latest line
    for a request wins. Returns how many interactions were written. The output
    holds conversation content; keep it beside the UI's other private state.
    """
    verdicts: dict[str, str] = {}
    if feedback_path is not None and feedback_path.exists():
        for record in load_feedback(feedback_path):
            verdicts[record.request_id] = record.verdict
    interactions: list[Interaction] = []
    for thread in await _owned_threads(store, principal):
        for question, run in await _completed_turns(store, principal, thread):
            interactions.append(
                Interaction(
                    request_id=run.request_id,
                    thread_id=thread.thread_id,
                    question=question,
                    answer=run.answer or "",
                    application=thread.agent_id,
                    verdict=verdicts.get(run.request_id, ""),
                )
            )
    write_interactions(out, interactions)
    return len(interactions)
```

Implement `_owned_threads` and `_completed_turns` as small private helpers over
the store API (`list_owned` pages and `history`); type them against the store
protocol in `contracts/chat.py`, not the SQLite class.

- [ ] **Step 4: Run tests, types, lint, commit**

```bash
cd packages/ai-agent-lib-ui
python ../../tools/package_runtime.py ui -m pytest -q
python ../../tools/package_runtime.py ui -m mypy
cd ../.. && python -m ruff check packages/ai-agent-lib-ui
git add -A packages/ai-agent-lib-ui
git commit -m "ui: export saved conversations as interaction records"
```

### Task 11: The evals guide (R2.5)

**Files:**
- Create: `docs/evals.md`
- Modify: `docs/README.md` (one row in the task table, linking the guide)

**Interfaces:** consumes the names produced in Tasks 1–10, exactly as exported
from `ai_agent_lib_core.evaluation`.

- [ ] **Step 1: Write `docs/evals.md`**

Sections, each with a runnable snippet using the real API:
1. **What an eval is here** — offline (dataset → `run_eval` → `require`)
   versus online (capture → export → `sampled` → `score_interactions` →
   `require_no_regression`); both off the request path.
2. **Offline: the generated loop** — `agentlib eval SERVICE`, the report file,
   `agentlib eval SERVICE --accept`, what the baseline gate fails on.
3. **Judges** — `judged`, building the judge from the `judge` model alias, and
   calibrating with `calibrate` before trusting it (include the
   `LabeledCase` snippet).
4. **Online: capture** — Agent Lab feedback (`feedback.jsonl`) and
   `export_interactions`.
5. **Online: scoring** — `load_interactions`, `sampled(rate=..., salt=...)`,
   `score_interactions`, gating with `require`/`require_no_regression`,
   slicing with `report.by_tag("down")`.
6. **Privacy** — interaction and feedback files contain conversation content;
   keep them in the service's `.agentlib/` state, treat like the model trace
   (test data locally, masked credentials, never committed).

- [ ] **Step 2: Link from `docs/README.md`**

Add a row to the task table: `| Measure an agent offline and online |
[Evals guide](evals.md) | Run generated evals, accept a baseline, capture
feedback, and score sampled production answers |`

- [ ] **Step 3: Verify the documentation checks**

Run: `python -m pytest tests/architecture/test_generated_docs.py -q` and the
repository's documentation link check (it ran in prior reviews; find it with
`grep -rl "links" tests/architecture/` and run that test file).
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/evals.md docs/README.md
git commit -m "docs: offline and online evals guide"
```

---

## Phase 3 — HTTP service composition (spec R3)

### Task 12: `AgentService` and `run_agent` (R3.1)

**Files:**
- Create: `packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/http/composition.py`
- Modify: `packages/ai-agent-lib-core/src/ai_agent_lib_core/integrations/http/__init__.py`
- Test: `packages/ai-agent-lib-core/tests/unit/test_http_composition.py`

**Interfaces:**
- Consumes: `agent_app`, `serve`, `ServiceLifecycle` from
  `integrations/http`; `langfuse_tracing`, `LangfuseTracing` from
  `integrations/langgraph/langfuse.py`; `configure_logging`,
  `start_telemetry`, `report_error`, `shows_details` from `observability`;
  `load_service_config` from `config`; `ServiceContainer`.
- Produces:
  - `read_question(given: object) -> str`
  - `AgentService(application, build_graph, respond, question=read_question,
    stream=None, live_updates=True, default_port=8000, app_kwargs={})`
  - `async build_agent_app(service, services, lifecycle, *, tracing=None) -> Any`
  - `run_agent(service: AgentService, argv: Sequence[str] | None = None) -> int`
- Consumed by: Tasks 13–15.

- [ ] **Step 1: Read the reference implementations**

Read `examples/accounts-agent/src/accounts_agent/service.py` and the template
`packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/agent/src/__package__/service.py.jinja`
end to end. `composition.py` is those files' shared behavior, extracted
verbatim where possible — same readiness ordering (listener first, prepare
before readiness), same teardown (`aclose` in `finally`), same
`report_error` exit path.

- [ ] **Step 2: Write the failing tests**

Model the test harness on the existing
`packages/ai-agent-lib-core/tests/unit/test_http_serving.py` and
`test_http_agent.py` (reuse their app-driving helpers and fakes):

```python
"""The shared composition behind every generated agent service."""

from __future__ import annotations

import pytest

from ai_agent_lib_core.contracts import ValidationFailed
from ai_agent_lib_core.integrations.http import AgentService, read_question


def test_read_question_takes_a_non_empty_question() -> None:
    assert read_question({"question": "What is the balance?"}) == "What is the balance?"


@pytest.mark.parametrize("given", [None, {}, {"question": ""}, {"question": 3}, "text"])
def test_read_question_refuses_anything_else(given: object) -> None:
    with pytest.raises(ValidationFailed):
        read_question(given)


async def test_an_agent_service_serves_invoke_and_health(...) -> None:
    # Arrange: AgentService(application="probe", build_graph=<returns a scripted
    # object>, respond=<returns {"answer": question.upper()}>); a started
    # ServiceContainer from the testing kit; build_agent_app; drive the app the
    # way test_http_agent.py does.
    # Assert: /healthz 200; /readyz 503 before prepare and 200 after; POST
    # /invoke with {"input": {"question": "hi"}, ...} returns the answer "HI";
    # a body without a question maps to the 4xx the agent_app validation path
    # already produces.


async def test_build_graph_runs_before_readiness(...) -> None:
    # Arrange: a build_graph that sets a flag; assert /readyz stays unavailable
    # until the lifecycle's prepare ran, matching test_http_startup.py.
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m pytest packages/ai-agent-lib-core/tests/unit/test_http_composition.py -q`
Expected: FAIL — cannot import `AgentService`.

- [ ] **Step 4: Implement `composition.py`**

```python
"""One entry point for an agent service: the wiring every service repeats.

A service declares what is its own — the application name, how its graph is
built, how it responds — and ``run_agent`` owns the rest: configuration,
logging, telemetry, optional Langfuse tracing, container lifecycle, serving
and teardown. ``agent_app`` and ``serve`` remain available for services that
compose by hand.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_agent_lib_core.config import load_service_config
from ai_agent_lib_core.contracts import (
    RequestContext,
    ServiceConfig,
    ValidationFailed,
    kind_of,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.http.agent import agent_app
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle
from ai_agent_lib_core.integrations.http.serving import serve
from ai_agent_lib_core.observability import (
    configure_logging,
    report_error,
    shows_details,
    start_telemetry,
)

__all__ = ["AgentService", "build_agent_app", "read_question", "run_agent"]

Respond = Callable[[Any, ServiceContainer, RequestContext, str], Awaitable[dict[str, Any]]]
StreamSteps = Callable[
    [Any, ServiceContainer, RequestContext, object], AsyncIterator[dict[str, Any]]
]


def read_question(given: object) -> str:
    """The question in a request body: a non-empty ``question`` string.

    Raises:
        ValidationFailed: For anything else, naming what was given by kind only.
    """
    question = given.get("question") if isinstance(given, Mapping) else None
    if not isinstance(question, str) or not question.strip():
        raise ValidationFailed(
            "the input is not a question",
            expected='an object such as {"question": "What is the balance of 4411?"}',
            actual=f"no non-empty 'question'; the input is {kind_of(given)}",
        )
    return question


@dataclass(frozen=True, slots=True)
class AgentService:
    """What one agent service is, separated from how every service runs.

    Attributes:
        application: The agent's name: token audience, policy and audit name.
        build_graph: Builds the compiled graph once, after the container is
            validated and before readiness; MCP discovery belongs inside it.
        respond: Answers one question with the graph.
        question: Reads the question from the request input.
        stream: With one, ``POST /invoke/stream`` sends each step. It receives
            the graph, the container, the context and the raw input.
        live_updates: Whether ``POST /invoke`` negotiates model drafts.
        default_port: The port ``--port`` defaults to.
        app_kwargs: Extra keyword arguments for ``agent_app``, such as
            ``outcomes``, ``inspector``, ``diagnostics`` or ``admission``.
    """

    application: str
    build_graph: Callable[[ServiceContainer], Awaitable[Any]]
    respond: Respond
    question: Callable[[object], str] = read_question
    stream: StreamSteps | None = None
    live_updates: bool = True
    default_port: int = 8000
    app_kwargs: Mapping[str, Any] = field(default_factory=dict)
```

Then `build_agent_app` and `run_agent`, extracted from the accounts-agent
reference read in Step 1 — the same bodies, parameterized by `service`:

```python
async def build_agent_app(
    service: AgentService,
    services: ServiceContainer,
    lifecycle: ServiceLifecycle,
    *,
    tracing: Any | None = None,
) -> Any:
    """Build the agent's HTTP application; the graph is prepared before readiness."""
    graph: Any = None

    async def prepare() -> None:
        nonlocal graph
        graph = await service.build_graph(services)

    lifecycle.prepare(prepare)

    async def run(context: RequestContext, given: Any) -> dict[str, Any]:
        question = service.question(given)
        if tracing is not None:
            return await tracing.run(
                graph, services, context, question, respond=service.respond
            )
        return await service.respond(graph, services, context, question)

    steps: Callable[[RequestContext, Any], AsyncIterator[dict[str, Any]]] | None = None
    if service.stream is not None:
        source = service.stream

        def bound(context: RequestContext, given: Any) -> AsyncIterator[dict[str, Any]]:
            return source(graph, services, context, given)

        steps = bound

    return agent_app(
        services,
        run,
        application=service.application,
        lifecycle=lifecycle,
        stream=steps,
        live_updates=service.live_updates,
        **dict(service.app_kwargs),
    )
```

```python
async def _serve(service: AgentService, config: ServiceConfig, host: str, port: int) -> None:
    from ai_agent_lib_core.integrations.langgraph.langfuse import langfuse_tracing

    async with langfuse_tracing(service.application, config) as tracing:
        services = ServiceContainer(config, model_trace=tracing)

        async def start() -> None:
            await services.start()
            await services.validate()

        lifecycle = ServiceLifecycle(start)
        try:
            app = await build_agent_app(service, services, lifecycle, tracing=tracing)
            await serve(app, lifecycle, host=host, port=port)
        finally:
            await services.aclose()


def run_agent(service: AgentService, argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    parser = argparse.ArgumentParser(
        prog=f"{service.application}-serve", description=run_agent.__doc__
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=service.default_port, help="port")
    arguments = parser.parse_args(argv)
    config: ServiceConfig | None = None
    try:
        config = load_service_config()
        configure_logging(service.application, details=shows_details(config))
        stop_telemetry = start_telemetry(service.application, config)
        try:
            asyncio.run(_serve(service, config, arguments.host, arguments.port))
        finally:
            stop_telemetry()
    except Exception as error:  # noqa: BLE001 - every failure is explained, then the exit code
        return report_error(f"{service.application}-serve", error, details=shows_details(config))
    return 0
```

Match import spellings, the `langfuse_tracing`/`LangfuseTracing` signature and
`tracing.run(...)` call shape, and `agent_app`'s exact parameters to what the
accounts-agent file uses (Step 1). Export the four names from
`integrations/http/__init__.py` with the same guarded-import pattern the
module already uses for serve-extra members. Verify `kind_of` is exported from
`contracts`; if it is not, inline the actual-description as
`"no non-empty 'question' in it"` exactly as the current services do.

- [ ] **Step 5: Run tests, lint, types, import contracts**

```bash
python -m pytest packages/ai-agent-lib-core/tests/unit/test_http_composition.py packages/ai-agent-lib-core/tests/unit/test_http_serving.py packages/ai-agent-lib-core/tests/unit/test_http_agent.py -q
python -m ruff check packages/ai-agent-lib-core && python -m mypy && lint-imports
```
Expected: PASS (the langgraph import inside `_serve` is function-local, so the
HTTP module graph stays clean for contract checks).

- [ ] **Step 6: Commit**

```bash
git add -A packages/ai-agent-lib-core
git commit -m "http: AgentService and run_agent service composition"
```

### Task 13: Template uses the composition (R3.2, template)

**Files:**
- Modify: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/agent/src/__package__/service.py.jinja`
- Test: `packages/ai-agent-lib-cli/tests/generated/` suites

- [ ] **Step 1: Rewrite the template on the helper**

Keep the module docstring, `__all__ = ["build_app", "main"]`, and behavior.
The business content stays (`_question` semantics come from `read_question`;
`respond` and the `/invoke/stream` generator keep their current bodies); the
boilerplate goes:

```python
"""HTTP entry point: serve the agent until the platform stops it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from ai_agent_lib_core import RequestContext, ServiceContainer
from ai_agent_lib_core.integrations.http import (
    AgentService,
    ServiceLifecycle,
    build_agent_app,
    run_agent,
)

from {{ package }}.graph import APPLICATION, build_graph, registered_mcp_tools

__all__ = ["SERVICE", "build_app", "main"]


async def _build_graph(services: ServiceContainer) -> Any:
    # Admission stays closed until discovery and graph compilation succeed.
    return build_graph(services, await registered_mcp_tools(services))


async def _respond(
    graph: Any, services: ServiceContainer, context: RequestContext, question: str
) -> dict[str, Any]:
    result = await graph.ainvoke(
        {"messages": [HumanMessage(question)]}, **services.invocation(context)
    )
    return {"answer": str(result["messages"][-1].content)}


async def _stream(
    graph: Any, services: ServiceContainer, context: RequestContext, given: Any
) -> AsyncIterator[dict[str, Any]]:
    # POST /invoke/stream sends each graph step as a server-sent event.
    ...  # keep the template's existing astream body verbatim, reading the
    # question with SERVICE.question(given)


SERVICE = AgentService(
    application=APPLICATION,
    build_graph=_build_graph,
    respond=_respond,
    stream=_stream,
)


async def build_app(services: ServiceContainer, lifecycle: ServiceLifecycle, **extra: Any) -> Any:
    """Build the agent's HTTP application; kept for callers of the previous shape."""
    return await build_agent_app(SERVICE, services, lifecycle, **extra)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    return run_agent(SERVICE, argv)
```

Where the plan says "keep ... verbatim", copy the template's current code for
that block — the executing agent must not invent new streaming behavior.
Reconcile the previous `build_app(..., tracing=...)` keyword with the
`**extra` passthrough so existing generated tests keep passing.

- [ ] **Step 2: Regenerate and test**

Run: `python -m pytest packages/ai-agent-lib-cli -q`
Expected: PASS, including generated-workspace service tests. Update the
generated-file inventory expectation if it hashes template outputs.

- [ ] **Step 3: Commit**

```bash
git add -A packages/ai-agent-lib-cli
git commit -m "cli: generated services compose through AgentService"
```

### Task 14: accounts-agent uses the composition (R3.2)

**Files:**
- Modify: `examples/accounts-agent/src/accounts_agent/service.py`
- Test: `examples/accounts-agent/tests/test_accounts_agent_service.py` (must
  pass unchanged, or with mechanical import updates only)

- [ ] **Step 1: Migrate** — apply the Task 13 shape to the example, keeping its
  `_question` error message, stream body and `__all__` compatibility
  (`build_app`, `main`).
- [ ] **Step 2: Run** `python -m pytest examples/accounts-agent -q` — PASS.
- [ ] **Step 3: Verify the boilerplate is gone** — the migrated file must not
  call `configure_logging`, `start_telemetry`, `langfuse_tracing` or `serve`
  directly: `grep -n "start_telemetry\|configure_logging\|langfuse_tracing" examples/accounts-agent/src/accounts_agent/service.py` returns nothing.
- [ ] **Step 4: Commit**

```bash
git add -A examples/accounts-agent
git commit -m "examples: accounts-agent on AgentService composition"
```

### Task 15: rmprep uses the composition (R3.2)

**Files:**
- Modify: `examples/rmprep/agents/rmprep/src/rmprep/service.py`
- Test: `examples/rmprep/agents/rmprep/tests/` (must pass unchanged)

- [ ] **Step 1: Read `rmprep/service.py`** and list what it wires beyond the
  template: inspection and outcome providers, and any extra routes. Those
  become `app_kwargs` entries (`outcomes=...`, `inspector=...`, matching
  `agent_app`'s parameters).
- [ ] **Step 2: Migrate** to `AgentService(..., app_kwargs={...})` +
  `run_agent`, keeping rmprep's respond/stream/business code untouched.
- [ ] **Step 3: Run** from the repository root:
  `python -m pytest examples/rmprep -q` — PASS. Then the readiness smoke check
  if the UI stack is set up on this machine: `start-local.bat --smoke-test`
  (Windows) — otherwise note it for CI.
- [ ] **Step 4: Commit**

```bash
git add -A examples/rmprep
git commit -m "examples: rmprep on AgentService composition"
```

---

## Phase 4 — Quality gates (spec R4)

### Task 16: UI import contracts (R4.1)

**Files:**
- Modify: `packages/ai-agent-lib-ui/pyproject.toml`
- Modify: `.github/workflows/ci.yml` (the `ui` job)

- [ ] **Step 1: Add the contracts to the UI pyproject**

```toml
[tool.importlinter]
root_packages = ["ai_agent_lib_ui"]
include_external_packages = true

[[tool.importlinter.contracts]]
name = "UI contracts import nothing else from the package"
type = "forbidden"
source_modules = ["ai_agent_lib_ui.contracts"]
forbidden_modules = [
    "ai_agent_lib_ui.adapters",
    "ai_agent_lib_ui.application",
    "ai_agent_lib_ui.config",
    "ai_agent_lib_ui.integrations",
]

[[tool.importlinter.contracts]]
name = "the UI application layer is framework-free"
type = "forbidden"
source_modules = ["ai_agent_lib_ui.application"]
forbidden_modules = [
    "ai_agent_lib_ui.integrations",
    "chainlit",
    "fastapi",
    "starlette",
]

[[tool.importlinter.contracts]]
name = "only UI integrations imports the browser framework"
type = "forbidden"
source_modules = [
    "ai_agent_lib_ui.adapters",
    "ai_agent_lib_ui.application",
    "ai_agent_lib_ui.config",
    "ai_agent_lib_ui.contracts",
]
forbidden_modules = ["chainlit"]
```

Add `"import-linter>=2.0"` to the UI `dev` extra.

- [ ] **Step 2: Run it locally**

From `packages/ai-agent-lib-ui`:
`python ../../tools/package_runtime.py ui -m importlinter.cli` — if that
module path fails, use the same invocation the root README documents:
`python -c "from importlinter.cli import lint_imports; lint_imports()"` under
the UI interpreter with the UI package directory as the working directory.
Expected: all contracts kept (the framework imports already live only under
`integrations/`). A violation found here is a real finding: move the import
behind `integrations/` rather than weakening the contract.

- [ ] **Step 3: Add the CI step**

In the `ui` job, after the mypy step:

```yaml
      - run: python -c "from importlinter.cli import lint_imports; raise SystemExit(lint_imports())"
        working-directory: packages/ai-agent-lib-ui
```

- [ ] **Step 4: Commit**

```bash
git add packages/ai-agent-lib-ui/pyproject.toml .github/workflows/ci.yml
git commit -m "ui: enforce layer boundaries with import-linter"
```

### Task 17: Coverage gate on core's critical modules (R4.2)

**Files:**
- Modify: `.github/workflows/ci.yml` (the `gate` job)

- [ ] **Step 1: Measure locally**

```bash
python -m pytest packages/ai-agent-lib-core -q --cov=ai_agent_lib_core.contracts --cov=ai_agent_lib_core.config --cov=ai_agent_lib_core.di --cov=ai_agent_lib_core.pipeline --cov-branch --cov-report=term
```

Note the total branch coverage percentage.

- [ ] **Step 2: Add the CI step**

In the `gate` job, guarded to one matrix cell so the gate runs once:

```yaml
      - name: Coverage gate on contracts, config, di and pipeline
        if: matrix.os == 'ubuntu-latest' && matrix.python == '3.11'
        # The specification sets 90; the measured floor on 2026-10-18 was <value>.
        run: >
          python -m pytest packages/ai-agent-lib-core -q
          --cov=ai_agent_lib_core.contracts --cov=ai_agent_lib_core.config
          --cov=ai_agent_lib_core.di --cov=ai_agent_lib_core.pipeline
          --cov-branch --cov-report=term --cov-fail-under=<threshold>
```

Set `<threshold>` to 90 when Step 1 measured ≥ 90; otherwise to the measured
value rounded down to an integer, and record both numbers in the comment —
the gate must hold the current floor, never aspire past it and fail the build.

- [ ] **Step 3: Verify the step passes locally with the chosen threshold, commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: branch-coverage gate on core's critical modules"
```

---

## Phase 5 — Developer experience (spec R5)

### Task 18: POSIX launcher parity (R5.1)

**Files:**
- Create: `setup-local.sh`, `start-local.sh`, `stop-local.sh`, `start-phoenix.sh`
- Modify: `README.md` (Agent Lab section: name both shells)
- Test: `tests/architecture/test_local_launchers.py`

- [ ] **Step 1: Read each `.bat`** and note the tool module it delegates to
  (`start-local.bat` runs `python "%~dp0tools\local_dev.py" %*`; read the
  other three for their modules and arguments).

- [ ] **Step 2: Write the failing test**

```python
"""Both launcher shells delegate to the same tools, so neither drifts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAIRS = [
    ("setup-local.bat", "setup-local.sh"),
    ("start-local.bat", "start-local.sh"),
    ("stop-local.bat", "stop-local.sh"),
    ("start-phoenix.bat", "start-phoenix.sh"),
]


def _tool(text: str) -> str:
    found = re.search(r"tools[\\/](\w+)\.py", text)
    assert found, f"no tools/<module>.py call in: {text!r}"
    return found.group(1)


@pytest.mark.parametrize(("bat", "sh"), PAIRS)
def test_each_shell_pair_calls_the_same_tool(bat: str, sh: str) -> None:
    bat_text = (ROOT / bat).read_text(encoding="utf-8")
    sh_text = (ROOT / sh).read_text(encoding="utf-8")
    assert _tool(bat_text) == _tool(sh_text)
    assert '"$@"' in sh_text  # arguments pass through


@pytest.mark.parametrize(("bat", "sh"), PAIRS)
def test_the_posix_wrapper_is_executable(bat: str, sh: str) -> None:
    assert (ROOT / sh).stat().st_mode & 0o111, f"{sh} is not executable"
```

Run: `python -m pytest tests/architecture/test_local_launchers.py -q` —
Expected: FAIL (files missing).

- [ ] **Step 3: Write the wrappers** (one shown; mirror for each pair with the
  module found in Step 1):

```sh
#!/bin/sh
# Resolve paths from this file so the command also works from another folder.
exec python3 "$(dirname "$0")/tools/local_dev.py" "$@"
```

```bash
chmod +x setup-local.sh start-local.sh stop-local.sh start-phoenix.sh
```

- [ ] **Step 4: Update the README** — in "Run Agent Lab", after the
  Command Prompt sentence, add the bash equivalents (`./start-local.sh`,
  `cat .agentlib/ui-rmprep/access-code`, `./stop-local.sh`).

- [ ] **Step 5: Run the test and the docs checks, commit**

```bash
python -m pytest tests/architecture/test_local_launchers.py -q
python -m pytest tests/architecture/test_generated_docs.py -q
git add setup-local.sh start-local.sh stop-local.sh start-phoenix.sh README.md tests/architecture/test_local_launchers.py
git commit -m "dev: POSIX launchers mirroring the batch entry points"
```

### Task 19: `agentlib update --plan` (R5.2)

**Files:**
- Modify: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/scaffold.py`
- Modify: `packages/ai-agent-lib-cli/src/ai_agent_lib_cli/commands/create.py`
- Modify: `docs/README.md` ("Keep documentation in sync": point the `--diff`
  warning at `--plan`)
- Test: `packages/ai-agent-lib-cli/tests/unit/` (beside the existing update
  tests — find them with `grep -rln "def test.*update" packages/ai-agent-lib-cli/tests/unit/`)

**Interfaces:**
- Produces: `Scaffolder.plan(root: Path) -> UpdateReport` (no writes;
  `updated` means "would be updated"); `agentlib update --plan [--diff]`.

- [ ] **Step 1: Write the failing tests** (reuse the fixtures of the existing
  update tests found above):

```python
def test_plan_lists_changes_without_writing(...) -> None:
    # Arrange: a generated workspace; make one generated file stale (edit the
    # template-written content via an older generation or truncate it) and
    # developer-edit another.
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = scaffolder.plan(root)
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before  # nothing was written
    assert report.updated  # the stale file is listed as would-be updated
    assert report.kept  # the edited file is listed with its new text


def test_update_plan_flag_previews_and_exits_clean(...) -> None:
    result = runner.invoke(cli, ["update", "--plan", "--workspace", str(root)])
    assert result.exit_code == 0, result.output
    assert "would update" in result.output
    # And the tree is untouched (same before/after comparison as above).
```

Run them — Expected: FAIL (`plan` does not exist).

- [ ] **Step 2: Implement `Scaffolder.plan`**

Read `Scaffolder.update` (`scaffold.py:331`). Extract its walk into a private
`_update(root, *, write: bool) -> UpdateReport`: identical logic, with the
file-writing statement executed only when `write` is true. `update` calls
`self._update(root, write=True)`; `plan` calls
`self._update(root, write=False)`. Docstring for `plan`: *"What update would
do, without doing it: updated and created mean 'would be'."*

- [ ] **Step 3: Implement the flag in `commands/create.py`**

```python
@click.option("--plan", "plan_only", is_flag=True, help="Preview every change; write nothing.")
```

In the command body:

```python
    report = tools.scaffolder().plan(root) if plan_only else tools.scaffolder().update(root)
```

When `plan_only`, print with would-be labels:

```python
    verb = {"updated": "would update", "created": "would create"} if plan_only else {}
    for label, paths in (("updated", report.updated), ("created", report.created)):
        for path in paths:
            click.echo(f"  {verb.get(label, label)}  {path.as_posix()}")
```

Keep the `kept`/`--diff` behavior for both modes. Update the command
docstring: *"--plan previews every change and writes nothing."*

- [ ] **Step 4: Run, lint, docs, commit**

```bash
python -m pytest packages/ai-agent-lib-cli -q
python -m ruff check packages/ai-agent-lib-cli && python -m mypy
git add -A packages/ai-agent-lib-cli docs/README.md
git commit -m "cli: agentlib update --plan is a true dry run"
```

---

## Phase 6 — Structure (spec R6.2, R6.3)

### Task 20: Remove the concluded Chainlit spike (R6.3)

**Files:**
- Delete: `experiments/chainlit-phase0/` (entire directory)
- Modify: any document linking into it

- [ ] **Step 1: Find the references**

```bash
grep -rln "chainlit-phase0" --include="*.md" --include="*.py" --include="*.toml" --include="*.yml" . | grep -v experiments/
```

- [ ] **Step 2: Rewrite each reference** to point at
  `docs/frontend-phase0-review.md` (the recorded decision) with past-tense
  wording — e.g. the docs index line about the "original Chainlit trial" says
  the spike was removed after the decision and lives in git history.

- [ ] **Step 3: Delete and verify**

```bash
git rm -r experiments/chainlit-phase0
python -m pytest tests/architecture -q
python -m pytest packages/ai-agent-lib-cli/tests -q
```

Also run the documentation link test found in Task 11 Step 3.
Expected: PASS — nothing in the gate imports the spike.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "repo: remove the concluded chainlit-phase0 spike"
```

### Task 21: ADR 0004 — tracing configuration ownership (R6.2)

**Files:**
- Create: `docs/adr/0004-tracing-configuration.md`
- Modify: `docs/adr/README.md` (add the entry)

- [ ] **Step 1: Write the ADR** in the exact structure of ADR 0001–0003
  (Context / Decision / Consequences / Owner):

  - **Context:** Langfuse and Phoenix settings are resolved by dedicated
    loaders in `config/langfuse.py` and `config/phoenix.py` using
    `LANGFUSE_*`, `PHOENIX_*` and `EAP_LANGFUSE_ENABLED`, outside the binding
    table; the October `doctor` blind spot came from this second path; the
    binding table's "unknown `EAP_` names fail startup" guarantee does not
    cover these sections; tracing code spans `config/`, `observability/` and
    `integrations/langgraph/`.
  - **Decision:** record the chosen direction. Default recommendation: keep
    third-party variable names (they are the vendors' documented names, like
    `AWS_PROFILE`), but register them in the binding table as listed
    third-party reads the way `AWS_*`/`OTEL_*` already are, keep
    `EAP_LANGFUSE_ENABLED` as a bound variable, and require every optional
    integration to resolve through a loader that `doctor`, `config explain`
    and startup all share (the Langfuse fix generalized as a rule).
  - **Consequences:** new optional integrations must register their variables
    and shared loader before shipping; no behavioral change to current
    deployments; a later consolidation of tracing modules is possible but not
    decided here.
  - **Owner:** library maintainers.

- [ ] **Step 2: Verify docs checks and commit**

```bash
python -m pytest tests/architecture/test_generated_docs.py -q
git add docs/adr/0004-tracing-configuration.md docs/adr/README.md
git commit -m "adr: 0004 tracing configuration ownership"
```

---

## Final verification (whole plan)

- [ ] From the repository root: `python -m ruff check .`,
  `python -m ruff format --check .`, `python -m mypy`, `lint-imports`,
  `python -m pytest` — all pass.
- [ ] From `packages/ai-agent-lib-ui`:
  `python ../../tools/package_runtime.py ui -m pytest`,
  `... -m mypy`, and the UI lint-imports invocation from Task 16 — all pass.
- [ ] `python docs/guide/build_guide.py --check` passes.
- [ ] Generate a fresh fake-model agent per the README quickstart and run
  `agentlib check` and `agentlib eval helper` in it: check passes; eval
  reports the skip-until-real-model behavior; `agentlib eval helper --accept`
  without a report names the fix.
- [ ] Deferred work recorded, not implemented: CLI regrouping (spec R6.1) and
  `agentlib lab` get their own plans.

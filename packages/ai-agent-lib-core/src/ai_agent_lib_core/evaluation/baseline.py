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
    except (OSError, ValueError) as problem:
        raise ValidationFailed(
            f"the baseline {path} cannot be read",
            expected="the JSON an eval writes with report.to_json()",
            actual=str(problem),
            fix="write the baseline with agentcraft2 eval SERVICE --accept",
        ) from problem
    wrong = not isinstance(data, dict) or not isinstance(data.get("means"), dict)
    if wrong or data.get("schema", REPORT_SCHEMA) != REPORT_SCHEMA:
        raise ValidationFailed(
            f"the baseline {path} is not an eval report",
            expected=f'an object with "means" and schema {REPORT_SCHEMA}',
            actual="another shape or schema",
            fix="write the baseline with agentcraft2 eval SERVICE --accept",
        )
    results = data.get("results", [])
    try:
        return SavedReport(
            means={str(k): float(v) for k, v in data["means"].items()},
            case_scores={
                str(r["id"]): {str(k): float(v) for k, v in r.get("scores", {}).items()}
                for r in results
                if isinstance(r, dict) and "id" in r
            },
            metadata={str(k): str(v) for k, v in data.get("metadata", {}).items()},
        )
    except (TypeError, ValueError, AttributeError) as problem:
        raise ValidationFailed(
            f"the baseline {path} is not an eval report",
            expected="means, scores and metadata as the eval writes them",
            actual=str(problem),
            fix="write the baseline with agentcraft2 eval SERVICE --accept",
        ) from problem


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
        fix="fix the regression, or accept the new report: agentcraft2 eval SERVICE --accept",
    )

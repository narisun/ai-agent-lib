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


def test_a_different_schema_is_refused(tmp_path: Path) -> None:
    """A baseline with a different schema value is refused with ValidationFailed."""
    path = tmp_path / "baseline.json"
    path.write_text('{"means": {"y": 0.5}, "schema": "other/v1"}', encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_report(path)


def test_a_pre_v1_report_without_schema_loads(tmp_path: Path) -> None:
    """A pre-v1 report (no schema key) with means dict and results list loads successfully."""
    path = tmp_path / "baseline.json"
    report_json = (
        '{"means": {"y": 0.5}, "results": '
        '[{"id": "a", "scores": {"y": 1.0}}, {"id": "b", "scores": {"y": 0.0}}]}'
    )
    path.write_text(report_json, encoding="utf-8")
    saved = load_report(path)
    assert saved.means == {"y": 0.5}
    assert saved.case_scores == {"a": {"y": 1.0}, "b": {"y": 0.0}}


def test_non_numeric_mean_raises_validation_failed(tmp_path: Path) -> None:
    """A baseline with non-numeric mean value raises ValidationFailed, not raw ValueError."""
    path = tmp_path / "baseline.json"
    path.write_text('{"means": {"y": "not-a-number"}}', encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_report(path)


def test_null_scores_raises_validation_failed(tmp_path: Path) -> None:
    """A baseline with scores: null in a result raises ValidationFailed, not raw AttributeError."""
    path = tmp_path / "baseline.json"
    path.write_text(
        '{"means": {"y": 0.5}, "results": [{"id": "a", "scores": null}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed):
        load_report(path)


def test_invalid_utf8_raises_validation_failed(tmp_path: Path) -> None:
    """A file with invalid UTF-8 bytes raises ValidationFailed, not raw UnicodeDecodeError."""
    path = tmp_path / "baseline.json"
    path.write_bytes(b"\xff\xfe{")
    with pytest.raises(ValidationFailed):
        load_report(path)

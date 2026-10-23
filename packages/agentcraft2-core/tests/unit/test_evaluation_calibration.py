"""Checking a judge against answers a person has graded."""

from __future__ import annotations

import pytest

from agentcraft2_core.evaluation import EvalCase, LabeledCase, calibrate
from agentcraft2_core.evaluation.scorers import Score, Scorer


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
    with pytest.raises(ValueError, match="from 0 to 1"):
        LabeledCase(EvalCase(id="a", input={}), output="x", human=1.5)

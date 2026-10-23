"""Checking a judge against answers a person has graded, before trusting it."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean
from typing import Any

from agentcraft2_core.evaluation.cases import EvalCase
from agentcraft2_core.evaluation.scorers import Scorer

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

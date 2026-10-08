"""Scorers: how a case's output is judged, as a number from 0 to 1."""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ai_agent_lib_core.evaluation.cases import EvalCase

__all__ = ["Score", "Scorer", "Verdict", "exact", "judged", "scored_by"]


@dataclass(frozen=True, slots=True)
class Score:
    """One scorer's judgement of one case.

    Attributes:
        name: The scorer's name, which is the metric's name in the report.
        value: From 0, wrong, to 1, right.
        note: Why, for the report. Never more than the scorer saw.
    """

    name: str
    value: float
    note: str = ""


class Scorer(Protocol):
    """Judges a case's output."""

    @property
    def name(self) -> str:
        """The metric's name."""
        ...

    async def __call__(self, case: EvalCase, output: Any) -> Score:
        """Return the score of ``output`` for ``case``."""
        ...


def _field(output: Any, name: str) -> Any:
    if isinstance(output, Mapping):
        return output.get(name)
    return getattr(output, name, None)


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, enum.Enum) else value


class _Exact:
    def __init__(self, field: str, name: str | None) -> None:
        self._field = field
        self.name = name or field

    async def __call__(self, case: EvalCase, output: Any) -> Score:
        if self._field not in case.expected:
            return Score(self.name, 0.0, f"the case expects no {self._field!r}")
        got, wanted = _plain(_field(output, self._field)), case.expected[self._field]
        return Score(self.name, 1.0 if got == wanted else 0.0)


def exact(field: str, *, name: str | None = None) -> Scorer:
    """Score 1 when the output's ``field`` equals the case's expected ``field``.

    The output may be a Pydantic model, a dataclass or a mapping. An enum is
    compared by its value.
    """
    return _Exact(field, name)


class _ScoredBy:
    def __init__(
        self, name: str, judge: Callable[[EvalCase, Any], float | bool | Awaitable[float | bool]]
    ) -> None:
        self.name = name
        self._judge = judge

    async def __call__(self, case: EvalCase, output: Any) -> Score:
        result = self._judge(case, output)
        if isinstance(result, Awaitable):
            result = await result
        return Score(self.name, float(result))


def scored_by(
    name: str, judge: Callable[[EvalCase, Any], float | bool | Awaitable[float | bool]]
) -> Scorer:
    """Score with a function of the case and the output: a bool, or a number from 0 to 1."""
    return _ScoredBy(name, judge)


class Verdict(BaseModel):
    """A judge's verdict on one answer."""

    score: int = Field(ge=1, le=5, description="1 is unacceptable, 5 is excellent.")
    reason: str = Field(max_length=300, description="Why, in one or two sentences.")


Judge = Callable[[list[Any]], Awaitable[Verdict]]
"""Asks a model for a verdict: takes the messages, returns the verdict."""

_JUDGE_INSTRUCTIONS = (
    "You grade one answer against a rubric. Read the case, the expected facts and the "
    "answer. Give a score from 1 to 5 and a short reason. Judge only what the rubric asks."
)


class _Judged:
    def __init__(self, judge: Judge, rubric: str, name: str, show: Callable[[Any], str]) -> None:
        self._judge = judge
        self._rubric = rubric
        self.name = name
        self._show = show

    async def __call__(self, case: EvalCase, output: Any) -> Score:
        verdict = await self._judge(
            [
                SystemMessage(_JUDGE_INSTRUCTIONS),
                HumanMessage(
                    f"Rubric: {self._rubric}\n\nCase: {dict(case.input)}\n\n"
                    f"Expected: {dict(case.expected)}\n\nAnswer: {self._show(output)}"
                ),
            ]
        )
        return Score(self.name, (verdict.score - 1) / 4, verdict.reason)


def judged(
    judge: Judge,
    rubric: str,
    *,
    name: str = "judged",
    show: Callable[[Any], str] = str,
) -> Scorer:
    """Score by asking a model to grade the output against ``rubric``.

    Use it only for what cannot be matched, such as tone or faithfulness, and
    check the judge against answers a person has graded first. Build ``judge``
    from the ``judge`` model alias, so the eval names a role, not a model::

        verdicts = services.model("judge").with_structured_output(Verdict)
        scorer = judged(verdicts.ainvoke, "Is the answer polite and correct?")

    Args:
        judge: Returns a :class:`Verdict` for a list of messages.
        rubric: What the judge grades.
        name: The metric's name.
        show: Turns the output into the text the judge reads.
    """
    return _Judged(judge, rubric, name, show)

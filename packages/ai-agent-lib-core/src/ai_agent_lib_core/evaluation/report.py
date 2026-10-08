"""What an eval found: per case, per metric, and whether it meets its bar."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import mean

from ai_agent_lib_core.contracts import ValidationFailed
from ai_agent_lib_core.evaluation.cases import EvalCase
from ai_agent_lib_core.evaluation.scorers import Score

__all__ = ["CaseResult", "EvalReport"]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """How one case went.

    Attributes:
        case: The case.
        scores: One per scorer. Every score is 0 when the task failed.
        error_type: The type of the error the task raised, if it did.
        seconds: How long the task took.
    """

    case: EvalCase
    scores: tuple[Score, ...]
    error_type: str | None = None
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class EvalReport:
    """The results of one eval run.

    Attributes:
        results: One per case, in the order of the cases.
        metrics: The names of the scorers, in order.
    """

    results: tuple[CaseResult, ...]
    metrics: tuple[str, ...] = field(default=())

    @property
    def means(self) -> dict[str, float]:
        """The mean score of each metric over every case."""
        return {
            metric: mean(
                score.value for r in self.results for score in r.scores if score.name == metric
            )
            if self.results
            else 0.0
            for metric in self.metrics
        }

    def by_tag(self, tag: str) -> dict[str, float]:
        """The mean score of each metric over the cases with ``tag``."""
        chosen = [r for r in self.results if tag in r.case.tags]
        return EvalReport(tuple(chosen), self.metrics).means

    @property
    def failed_cases(self) -> list[CaseResult]:
        """The cases whose task raised an error."""
        return [r for r in self.results if r.error_type is not None]

    def below(self, bars: Mapping[str, float]) -> dict[str, float]:
        """Return the metrics whose mean is below its bar, with the mean."""
        means = self.means
        unknown = sorted(set(bars) - set(means))
        if unknown:
            raise KeyError(f"no scorer is called {', '.join(unknown)}; scorers: {list(means)}")
        return {metric: means[metric] for metric, bar in bars.items() if means[metric] < bar}

    def require(self, bars: Mapping[str, float]) -> None:
        """Raise unless every metric meets its bar: the assertion an eval test ends with.

        Raises:
            ValidationFailed: Naming each metric below its bar, the cases that
                failed it and the cases whose task raised.
        """
        short = self.below(bars)
        if not short:
            return
        worst = {
            metric: [
                r.case.id
                for r in self.results
                for s in r.scores
                if s.name == metric and s.value < 1
            ][:10]
            for metric in short
        }
        raise ValidationFailed(
            f"the eval is below its bar on {', '.join(short)}",
            expected="; ".join(f"{metric} >= {bars[metric]:.2f}" for metric in short),
            actual="; ".join(
                f"{metric} = {value:.2f} (cases not right: {', '.join(worst[metric]) or 'none'})"
                for metric, value in short.items()
            )
            + (f"; tasks failed on {len(self.failed_cases)} cases" if self.failed_cases else ""),
            fix="read the report's failing cases, change the prompt or the model, and run again",
        )

    def summary(self) -> list[str]:
        """A few lines for a terminal: each metric's mean, and the failures."""
        lines = [f"{len(self.results)} cases"]
        lines += [f"  {metric:<20} {value:.2f}" for metric, value in self.means.items()]
        for result in self.failed_cases:
            lines.append(f"  case {result.case.id}: the task raised {result.error_type}")
        return lines

    def to_json(self) -> str:
        """The report as JSON, to keep with the commit that produced it."""
        return json.dumps(
            {
                "cases": len(self.results),
                "means": self.means,
                "results": [
                    {
                        "id": r.case.id,
                        "tags": list(r.case.tags),
                        "error_type": r.error_type,
                        "seconds": round(r.seconds, 3),
                        "scores": {s.name: s.value for s in r.scores},
                        "notes": {s.name: s.note for s in r.scores if s.note},
                    }
                    for r in self.results
                ],
            },
            indent=2,
        )


def _metrics(scorers: Sequence[object]) -> tuple[str, ...]:
    return tuple(str(getattr(scorer, "name", "")) for scorer in scorers)

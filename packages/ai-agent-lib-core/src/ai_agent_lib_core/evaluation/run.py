"""Running a task over every case and scoring what comes back."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ai_agent_lib_core.evaluation.cases import EvalCase
from ai_agent_lib_core.evaluation.report import CaseResult, EvalReport, _metrics
from ai_agent_lib_core.evaluation.scorers import Score, Scorer

__all__ = ["run_eval"]


async def run_eval(
    task: Callable[[EvalCase], Awaitable[Any]],
    cases: Sequence[EvalCase],
    scorers: Sequence[Scorer],
    *,
    concurrency: int = 4,
) -> EvalReport:
    """Run ``task`` on every case, at most ``concurrency`` at a time, and score each output.

    A case whose task raises scores 0 on every metric and is reported by the
    error's type; the run goes on with the other cases.

    Args:
        task: Runs the agent, or the part being measured, for one case.
        cases: The dataset.
        scorers: How each output is judged. Their names are the report's metrics.
        concurrency: How many cases run at once.
    """
    names = _metrics(scorers)
    if len(set(names)) != len(names):
        raise ValueError(f"two scorers share a name: {list(names)}")
    gate = asyncio.Semaphore(max(concurrency, 1))

    async def one(case: EvalCase) -> CaseResult:
        async with gate:
            started = time.perf_counter()
            try:
                output = await task(case)
            except Exception as error:  # noqa: BLE001 - one failed case must not end the run
                seconds = time.perf_counter() - started
                failed = tuple(Score(name, 0.0, "the task failed") for name in names)
                return CaseResult(case, failed, type(error).__name__, seconds)
            seconds = time.perf_counter() - started
            scores = tuple([await scorer(case, output) for scorer in scorers])
            return CaseResult(case, scores, None, seconds)

    results = await asyncio.gather(*(one(case) for case in cases))
    return EvalReport(tuple(results), names)

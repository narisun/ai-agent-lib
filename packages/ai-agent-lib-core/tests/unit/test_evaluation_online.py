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

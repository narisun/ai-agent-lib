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

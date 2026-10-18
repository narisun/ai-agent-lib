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
            schema_repr = data.get("schema") if isinstance(data, dict) else None
            actual = f"schema {schema_repr!r}" if isinstance(data, dict) else "not an object"
            raise ValidationFailed(
                f"{where} is not a feedback record",
                expected=f"schema {FEEDBACK_SCHEMA}",
                actual=actual,
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

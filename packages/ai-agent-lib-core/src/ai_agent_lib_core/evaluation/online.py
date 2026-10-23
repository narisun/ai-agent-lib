"""Scoring what production already answered: records, sampling and the runner.

Interactions are exported from what production already saved: the UI package's
``export_interactions`` joins its conversation store with the feedback log and
writes interaction files. This module loads those files, draws a sample and
scores it, in a separate process and with the same scorers offline evals use.
The append-only artifacts written on the request path are the UI store and the
feedback log, not the interaction files here. The task given to
:func:`run_eval` only returns the recorded answer; the scorers, typically
:func:`~ai_agent_lib_core.evaluation.judged`, do any model calls.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_agent_lib_core.contracts import ValidationFailed, is_schema
from ai_agent_lib_core.evaluation.cases import EvalCase
from ai_agent_lib_core.evaluation.report import EvalReport
from ai_agent_lib_core.evaluation.run import run_eval
from ai_agent_lib_core.evaluation.scorers import Scorer

__all__ = [
    "INTERACTION_SCHEMA",
    "Interaction",
    "as_cases",
    "load_interactions",
    "sampled",
    "score_interactions",
    "write_interactions",
]

INTERACTION_SCHEMA = "agentcraft2.interaction/v1"
"""The identity each interaction line carries; load_interactions checks it."""


@dataclass(frozen=True, slots=True)
class Interaction:
    """One production exchange: what was asked and what was answered.

    Attributes:
        request_id: The request, as the agent reported it.
        thread_id: The conversation the request belongs to.
        question: What was asked.
        answer: What the agent answered.
        application: The agent that answered.
        verdict: ``up`` or ``down`` when a person judged it; empty otherwise.
    """

    request_id: str
    thread_id: str
    question: str
    answer: str
    application: str = ""
    verdict: str = ""


def write_interactions(path: Path, interactions: Sequence[Interaction]) -> None:
    """Write interactions as JSONL, one object per line, replacing the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"schema": INTERACTION_SCHEMA, **asdict(item)}, ensure_ascii=False)
        for item in interactions
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load_interactions(path: Path) -> list[Interaction]:
    """Read every interaction in a JSONL file. Blank lines are skipped.

    Raises:
        ValidationFailed: If a line is not JSON or carries another schema.
            The message names the line.
    """
    interactions: list[Interaction] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name}, line {number}"
        try:
            data = json.loads(line)
        except json.JSONDecodeError as problem:
            raise ValidationFailed(
                f"{where} is not JSON",
                expected="one interaction object per line",
                actual=str(problem),
            ) from problem
        if not isinstance(data, dict) or not is_schema(data.get("schema"), INTERACTION_SCHEMA):
            actual_schema = (
                f"schema {data.get('schema')!r}" if isinstance(data, dict) else "not an object"
            )
            raise ValidationFailed(
                f"{where} is not an interaction",
                expected=f"schema {INTERACTION_SCHEMA}",
                actual=actual_schema,
            )
        interactions.append(
            Interaction(
                request_id=str(data.get("request_id", "")),
                thread_id=str(data.get("thread_id", "")),
                question=str(data.get("question", "")),
                answer=str(data.get("answer", "")),
                application=str(data.get("application", "")),
                verdict=str(data.get("verdict", "")),
            )
        )
    return interactions


def sampled(
    interactions: Sequence[Interaction], *, rate: float, salt: str = ""
) -> list[Interaction]:
    """A deterministic sample: the same inputs select the same interactions anywhere.

    An interaction is chosen by hashing ``salt`` and its request ID, so a rerun,
    another machine, or a second scorer sees the identical sample. Change the
    salt to draw a different sample at the same rate.

    Args:
        interactions: What production recorded.
        rate: The fraction to keep, from 0 to 1.
        salt: Varies the draw without touching the data.
    """
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"the sample rate is a number from 0 to 1, not {rate!r}")

    threshold = rate * 0x100000000  # rate 1.0 keeps every draw, 0.0 keeps none

    def chosen(item: Interaction) -> bool:
        digest = hashlib.sha256((salt + item.request_id).encode("utf-8")).hexdigest()
        return int(digest[:8], 16) < threshold

    return [item for item in interactions if chosen(item)]


def as_cases(interactions: Sequence[Interaction]) -> list[EvalCase]:
    """Turn interactions into cases: the request ID names the case, the verdict tags it."""
    return [
        EvalCase(
            id=item.request_id,
            input={"question": item.question},
            expected={},
            tags=(item.verdict,) if item.verdict else (),
        )
        for item in interactions
    ]


async def score_interactions(
    interactions: Sequence[Interaction],
    scorers: Sequence[Scorer],
    *,
    concurrency: int = 4,
    case_timeout: float | None = None,
    metadata: Mapping[str, str] | None = None,
) -> EvalReport:
    """Score recorded answers with ``scorers`` and return an ordinary eval report.

    The report gates like an offline run: ``require`` for bars,
    ``require_no_regression`` against a saved baseline, ``by_tag("down")`` for
    the answers people rejected.
    """
    answers = {item.request_id: item.answer for item in interactions}

    async def recorded(case: EvalCase) -> str:
        return answers[case.id]

    return await run_eval(
        recorded,
        as_cases(interactions),
        scorers,
        concurrency=concurrency,
        case_timeout=case_timeout,
        metadata=metadata,
    )

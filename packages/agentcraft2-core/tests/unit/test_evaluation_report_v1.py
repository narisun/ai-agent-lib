"""The eval report identifies itself and records where it came from."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from agentcraft2_core.evaluation import EvalCase, dataset_digest, run_eval
from agentcraft2_core.evaluation.scorers import exact


async def _answer(case: EvalCase) -> dict[str, int]:
    return {"y": 1}


async def test_report_json_carries_schema_and_metadata() -> None:
    case = EvalCase(id="a", input={"q": "x"}, expected={"y": 1})
    report = await run_eval(_answer, [case], [exact("y")], metadata={"model": "fake"})
    data = json.loads(report.to_json())
    assert data["schema"] == "agentcraft2.eval-report/v1"
    assert data["metadata"] == {"model": "fake"}
    assert data["means"] == {"y": 1.0}


async def test_metadata_defaults_to_empty() -> None:
    case = EvalCase(id="a", input={}, expected={"y": 1})
    report = await run_eval(_answer, [case], [exact("y")])
    assert report.metadata == {}
    assert json.loads(report.to_json())["metadata"] == {}


def test_dataset_digest_is_the_sha256_of_the_bytes(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_bytes(b'{"input": {}}\n')
    expected = "sha256:" + hashlib.sha256(b'{"input": {}}\n').hexdigest()
    assert dataset_digest(cases) == expected


async def test_case_timeout_fails_only_the_slow_case() -> None:
    async def slow_or_fast(case: EvalCase) -> dict[str, int]:
        if case.id == "slow":
            await asyncio.Event().wait()
        return {"y": 1}

    cases = [
        EvalCase(id="slow", input={}, expected={"y": 1}),
        EvalCase(id="fast", input={}, expected={"y": 1}),
    ]
    report = await run_eval(slow_or_fast, cases, [exact("y")], case_timeout=0.05)
    by_id = {r.case.id: r for r in report.results}
    assert by_id["slow"].error_type == "TimeoutError"
    assert by_id["slow"].scores[0].value == 0.0
    assert by_id["fast"].scores[0].value == 1.0

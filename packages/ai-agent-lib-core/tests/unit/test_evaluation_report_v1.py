"""The eval report identifies itself and records where it came from."""

from __future__ import annotations

import json

from ai_agent_lib_core.evaluation import EvalCase, run_eval
from ai_agent_lib_core.evaluation.scorers import exact


async def _answer(case: EvalCase) -> dict[str, int]:
    return {"y": 1}


async def test_report_json_carries_schema_and_metadata() -> None:
    case = EvalCase(id="a", input={"q": "x"}, expected={"y": 1})
    report = await run_eval(_answer, [case], [exact("y")], metadata={"model": "fake"})
    data = json.loads(report.to_json())
    assert data["schema"] == "agentlib.eval-report/v1"
    assert data["metadata"] == {"model": "fake"}
    assert data["means"] == {"y": 1.0}


async def test_metadata_defaults_to_empty() -> None:
    case = EvalCase(id="a", input={}, expected={"y": 1})
    report = await run_eval(_answer, [case], [exact("y")])
    assert report.metadata == {}
    assert json.loads(report.to_json())["metadata"] == {}

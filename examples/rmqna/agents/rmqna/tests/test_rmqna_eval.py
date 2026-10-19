"""rmqna with a real model, scored over evals/cases.jsonl.

Unit tests prove the plumbing with a scripted model; this measures the agent.
It runs only when asked, and only with a real model set in the service's .env:

    agentlib eval rmqna

The report is written to .agentlib/eval-report.json. Keep it with the change
that produced it, and raise the bar as the agent gets better.

When evals/baseline.json exists — written by `agentlib eval rmqna --accept` —
the run also fails on regressions against it. `agentlib eval rmqna --accept`
leaves a marker beside the report for this run only, so it skips just that
gate to move the bar on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ai_agent_lib_core import ServiceContainer
from ai_agent_lib_core.evaluation import (
    EvalCase,
    dataset_digest,
    load_cases,
    load_report,
    require_no_regression,
    run_eval,
    scored_by,
)
from ai_agent_lib_core.testing import load_eval_config

from rmqna import APPLICATION, answer, build_graph

SERVICE = Path(__file__).resolve().parents[1]
CASES = SERVICE / "evals" / "cases.jsonl"
REPORT = SERVICE / ".agentlib" / "eval-report.json"
BASELINE = SERVICE / "evals" / "baseline.json"
ACCEPT_MARKER = SERVICE / ".agentlib" / "eval-accept.flag"


def mentions(case: EvalCase, output: str) -> bool:
    """The answer names what the case expects it to name."""
    return str(case.expected["mentions"]).lower() in output.lower()


@pytest.mark.eval
@pytest.mark.enable_socket
async def test_rmqna_meets_its_bar(tmp_path: Path) -> None:
    config = load_eval_config(SERVICE / ".env", state_dir=tmp_path)
    if config.model.provider == "fake":
        pytest.skip("set a real model provider and model ID in the service's .env")
    async with ServiceContainer(config) as services:
        await services.validate()
        # The agent alone, without the tools of its MCP servers: this measures it.
        graph = build_graph(services)

        async def ask(case: EvalCase) -> str:
            # A conversation of its own for every case, so no case sees another.
            context = await services.authenticate(
                None, application=APPLICATION, thread_id=f"eval-{case.id}"
            )
            return await answer(graph, services, context, str(case.input["question"]))

        report = await run_eval(
            ask,
            load_cases(CASES),
            [scored_by("mentions", mentions)],
            case_timeout=120.0,
            metadata={
                "application": APPLICATION,
                "model": f"{config.model.provider}:{config.model.model_id or ''}",
                "dataset": dataset_digest(CASES),
            },
        )
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report.to_json(), encoding="utf-8")
    print("\n".join(report.summary()))
    if BASELINE.exists() and not ACCEPT_MARKER.exists():
        # A passing run may still be worse than the last accepted one. agentlib
        # eval --accept leaves ACCEPT_MARKER to move the bar on purpose.
        require_no_regression(report, load_report(BASELINE), tolerance=0.05)
    report.require({"mentions": 0.8})

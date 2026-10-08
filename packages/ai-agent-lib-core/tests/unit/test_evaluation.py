"""The eval kit: cases from a file, scorers, a report and a bar."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from ai_agent_lib_core import Principal, RequestContext, bind_request_context
from ai_agent_lib_core.contracts import ConfigurationError, Section, ValidationFailed
from ai_agent_lib_core.evaluation import (
    EvalCase,
    Verdict,
    exact,
    judged,
    load_cases,
    run_eval,
    scored_by,
)
from ai_agent_lib_core.testing import (
    FakeChatModelProvider,
    Fakes,
    load_eval_config,
    structured_reply,
)

CALLER = RequestContext(
    principal=Principal(subject="u-1", tenant="t-1"),
    application="support-agent",
    request_id="r-1",
    thread_id="eval",
)


class Triage(BaseModel):
    """How to route one customer message."""

    category: Literal["billing", "fraud", "other"]
    urgent: bool


def write_cases(path: Path, *cases: dict[str, object]) -> Path:
    lines = ["# triage cases", "", *(json.dumps(case) for case in cases)]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


CASES: tuple[dict[str, object], ...] = (
    {
        "id": "abroad",
        "input": {"text": "Card used abroad"},
        "expected": {"category": "fraud", "urgent": True},
        "tags": ["fraud"],
    },
    {
        "id": "twice",
        "input": {"text": "Charged twice"},
        "expected": {"category": "billing", "urgent": False},
    },
    {"id": "hello", "input": {"text": "Hello"}, "expected": {"category": "other", "urgent": False}},
)


async def triage(model: Any, text: str) -> Triage:
    with bind_request_context(CALLER):
        result: Triage = await model.ainvoke([HumanMessage(text)])
    return result


def test_cases_are_read_from_json_lines(tmp_path: Path) -> None:
    cases = load_cases(write_cases(tmp_path / "cases.jsonl", *CASES))
    assert [case.id for case in cases] == ["abroad", "twice", "hello"]
    assert cases[0].input == {"text": "Card used abroad"}
    assert cases[0].tags == ("fraud",)


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("[1, 2]", "is not a case"),
        ('{"expected": {}}', 'no "input" object'),
        ('{"input": {}, "answer": 1}', "unknown keys: answer"),
        ("{not json", "is not JSON"),
    ],
)
def test_a_bad_line_says_where_and_what_it_should_be(
    tmp_path: Path, line: str, problem: str
) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        load_cases(path)
    assert "cases.jsonl, line 1" in caught.value.message
    assert problem in str(caught.value)


async def test_an_eval_runs_every_case_scores_it_and_holds_a_bar(tmp_path: Path) -> None:
    replies = [
        structured_reply(Triage(category="fraud", urgent=True)),
        structured_reply(Triage(category="other", urgent=False)),  # wrong: billing
        structured_reply(Triage, category="refund"),  # not a valid answer at all
    ]
    cases = load_cases(write_cases(tmp_path / "cases.jsonl", *CASES))
    fakes = Fakes(model=FakeChatModelProvider(replies))
    async with fakes.container() as services:
        model = services.model().with_structured_output(Triage)
        report = await run_eval(
            lambda case: triage(model, case.input["text"]),
            cases,
            [exact("category"), exact("urgent")],
            concurrency=1,
        )
    assert report.means == pytest.approx({"category": 1 / 3, "urgent": 2 / 3})
    assert report.by_tag("fraud") == {"category": 1.0, "urgent": 1.0}
    assert [r.error_type for r in report.results] == [None, None, "ValidationFailed"]
    assert report.below({"category": 0.9, "urgent": 0.5}) == pytest.approx({"category": 1 / 3})
    with pytest.raises(ValidationFailed) as caught:
        report.require({"category": 0.9})
    assert caught.value.expected == "category >= 0.90"
    assert caught.value.actual is not None
    assert "cases not right: twice, hello" in caught.value.actual
    assert "tasks failed on 1 cases" in caught.value.actual
    report.require({"urgent": 0.5})
    summary = report.summary()
    assert summary[0] == "3 cases"
    assert "  case hello: the task raised ValidationFailed" in summary
    saved = json.loads(report.to_json())
    assert saved["results"][1]["scores"] == {"category": 0.0, "urgent": 1.0}


async def test_a_judge_grades_what_cannot_be_matched() -> None:
    fakes = Fakes(model=FakeChatModelProvider([structured_reply(Verdict(score=4, reason="ok"))]))
    case = EvalCase(id="1", input={"question": "q"}, expected={"facts": "f"})
    async with fakes.container() as services:
        verdicts = services.model().with_structured_output(Verdict)

        async def judge(messages: list[object]) -> Verdict:
            with bind_request_context(CALLER):
                verdict: Verdict = await verdicts.ainvoke(messages)
            return verdict

        score = await judged(judge, "Is it polite?", name="polite")(case, "Dear customer...")
    assert (score.name, score.value, score.note) == ("polite", 0.75, "ok")


async def test_a_function_can_score() -> None:
    short = scored_by("short", lambda case, output: len(output) < 10)
    case = EvalCase(id="1", input={})
    assert (await short(case, "brief")).value == 1.0
    assert (await short(case, "far too long an answer")).value == 0.0


def test_an_eval_config_keeps_the_real_model_and_reads_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("EAP_MODEL_PROVIDER=anthropic\n", encoding="utf-8")
    monkeypatch.setenv("EAP_MODEL_ID", "claude-test")
    config = load_eval_config(dotenv, state_dir=tmp_path / "state")
    assert (config.model.provider, config.model.model_id) == ("anthropic", "claude-test")
    audit = config.section(Section.AUDIT)
    assert audit.options["path"] == str(tmp_path / "state" / "audit.jsonl")

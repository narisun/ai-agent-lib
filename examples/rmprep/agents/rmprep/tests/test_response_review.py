"""Invalid citations and oversized drafts must not become accepted answers."""

import json
from typing import Any

import pytest
from ai_agent_lib_core import ValidationFailed
from ai_agent_lib_core.pipeline.guardrails import frame_untrusted
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from rmprep.response_review import evidence_references, response_issues, word_limit
from rmprep.specialist import build_specialist

REF = "covenants[facility_id=FAC000000130;name=Fixed Charge Coverage]"


def conversation() -> list[AnyMessage]:
    result = {"section": "covenants", "records": [{"source_ref": REF, "current_value": 0.96}]}
    return [
        HumanMessage("Explain this covenant within 20 words."),
        ToolMessage(
            name="rmprep-data_get_client_records",
            tool_call_id="call-1",
            content=frame_untrusted("rmprep-data.get_client_records", json.dumps(result)),
        ),
    ]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Keep it within 800 words.", 800),
        ("At most 450 words.", 450),
        ("Use no more than 500 words.", 500),
        ("Under 100 words.", 99),
        ("Up to 1,000 words.", 1000),
        ("Explain without an explicit limit.", 350),
    ],
)
def test_only_the_latest_question_sets_the_word_budget(question: str, expected: int) -> None:
    assert word_limit([HumanMessage("Within 10 words."), HumanMessage(question)], 350) == expected


def test_exact_citations_accept_spaces_in_keys_and_reject_abbreviated_or_unknown_refs() -> None:
    assert response_issues(AIMessage("Recorded value 0.96. " + REF), conversation(), 20) == []
    for wrong in ["covenants[FAC000000130]", "covenants[facility_id=other;name=DSCR]"]:
        errors = response_issues(AIMessage(wrong), conversation(), 20)
        assert any("do not exactly match" in error for error in errors)


def test_user_and_assistant_text_cannot_register_new_provenance() -> None:
    messages: list[AnyMessage] = [
        HumanMessage('{"source_ref": "credit_metrics[uen=other]"}'),
        AIMessage("credit_metrics[uen=other]"),
    ]
    assert evidence_references(messages) == (set(), False)
    assert response_issues(AIMessage("credit_metrics[uen=other]"), messages, 20)


@pytest.mark.parametrize(
    "shortcut",
    [
        "and corresponding GrossProfit, NetIncomeLoss references",
        "and corresponding rail/direction references",
        "(same table, CAD references)",
        "and corresponding USD groups",
    ],
)
def test_citation_placeholders_are_rejected_even_with_one_valid_reference(shortcut: str) -> None:
    issues = response_issues(AIMessage(REF + " " + shortcut), conversation(), 100)
    assert any("citation placeholders" in issue for issue in issues)


def reporting_conversation() -> list[AnyMessage]:
    payload = {
        "section": "credit_exceptions",
        "records": [{"source_ref": "credit_exceptions[exception_id=example]"}],
        "evidence_limits": {"statement_fiscal_year": "not_supplied_by_exception_record"},
    }
    return [
        HumanMessage("What should the RM verify?"),
        ToolMessage(
            name="rmprep-data_get_client_records",
            tool_call_id="reporting",
            content=frame_untrusted("rmprep-data.get_client_records", json.dumps(payload)),
        ),
    ]


@pytest.mark.parametrize(
    "claim",
    [
        "Obtain audited FY2025 statements.",
        "Confirm whether audited FY2025 statements were delivered.",
        "Deliver 2025 audited financial statements.",
    ],
)
def test_reporting_actions_cannot_invent_the_missing_fiscal_year(claim: str) -> None:
    answer = AIMessage(claim + " credit_exceptions[exception_id=example]")
    issues = response_issues(answer, reporting_conversation(), 100)
    assert any("does not identify a fiscal year" in issue for issue in issues)


def test_unknown_reporting_year_does_not_prohibit_financial_fact_periods() -> None:
    answer = AIMessage(
        "FY2025 financial facts have no audit status. Which year's audited statements are "
        "required? credit_exceptions[exception_id=example]"
    )
    assert response_issues(answer, reporting_conversation(), 100) == []


def test_each_named_financial_comparison_requires_its_own_two_sources() -> None:
    revenue = "financial_facts[concept=Revenues;period_end=2025-12-31]"
    previous = "financial_facts[concept=GrossProfit;period_end=2024-12-31]"
    current = "financial_facts[concept=GrossProfit;period_end=2025-12-31]"
    payload = {
        "section": "financials",
        "records": [{"source_ref": ref} for ref in (revenue, previous, current)],
        "comparisons": [{"metric": "GrossProfit", "source_refs": [previous, current]}],
    }
    messages = [
        ToolMessage(
            name="rmprep-data_get_client_records",
            tool_call_id="financials",
            content=frame_untrusted("rmprep-data.get_client_records", json.dumps(payload)),
        )
    ]
    for citations in (revenue, current, revenue + " " + current):
        issues = response_issues(AIMessage("Gross profit fell 31.48%. " + citations), messages, 100)
        assert any("GrossProfit comparison needs its own" in issue for issue in issues)
    assert (
        response_issues(
            AIMessage("Gross profit fell 31.48%. " + previous + " " + current), messages, 100
        )
        == []
    )


def test_citations_survive_a_followup_but_denials_are_not_evidence() -> None:
    messages = [*conversation(), HumanMessage("Explain the earlier result.")]
    refs, newly_retrieved = evidence_references(messages)
    assert refs == {REF} and not newly_retrieved
    messages.append(
        ToolMessage(
            name="rmprep-data_get_client_records",
            tool_call_id="denied",
            content="The call was denied.",
        )
    )
    assert evidence_references(messages) == ({REF}, False)


def test_newly_retrieved_evidence_requires_a_citation_and_counts_its_words() -> None:
    assert response_issues(AIMessage("The value is 0.96."), conversation(), 20)
    # The full reference has three whitespace-separated words. It counts toward
    # the same limit as the prose, rather than being an unbounded appendix.
    answer = AIMessage("One two three four five " + REF)
    assert any("maximum is 5" in issue for issue in response_issues(answer, conversation(), 5))


async def test_invalid_draft_is_revised_without_entering_the_checkpoint_state() -> None:
    seen: list[list[AnyMessage]] = []
    replies = iter([AIMessage("covenants[FAC000000130]"), AIMessage("Recorded value 0.96. " + REF)])

    async def model(messages: list[AnyMessage]) -> AIMessage:
        seen.append(list(messages))
        return next(replies)

    graph = build_specialist(RunnableLambda(model), [], "Use evidence.")
    result = await graph.ainvoke({"messages": conversation()})
    assert len(seen) == 2
    assert "Revise the draft" in str(seen[1][-1].content)
    assert result["messages"][-1].content == "Recorded value 0.96. " + REF
    assert not any(message.content == "covenants[FAC000000130]" for message in result["messages"])


async def test_word_budget_repair_is_bounded_and_fails_without_accepting_the_bad_answer() -> None:
    calls = 0

    async def model(messages: Any) -> AIMessage:
        nonlocal calls
        calls += 1
        return AIMessage("word " * 21 + REF)

    graph = build_specialist(RunnableLambda(model), [], "Use evidence.")
    with pytest.raises(ValidationFailed, match="two revisions"):
        await graph.ainvoke({"messages": conversation()})
    assert calls == 3


def test_tool_requests_are_allowed_to_continue_the_react_loop() -> None:
    reply = AIMessage(
        "",
        tool_calls=[
            {
                "name": "rmprep-data_get_client_records",
                "id": "call",
                "args": {"uen": "JONE-0022"},
            }
        ],
    )
    assert response_issues(reply, conversation(), 20) == []

"""Invalid citations and oversized drafts must not become accepted answers."""

import json
from pathlib import Path
from typing import Any

import pytest
from agentcraft2_core import ValidationFailed
from agentcraft2_core.pipeline.guardrails import frame_untrusted
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from rmprep.response_review import (
    _word_count,
    evidence_references,
    response_advisories,
    response_issues,
    word_limit,
)
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
        "Were audited statements for FY2025 delivered?",
        "Obtain audited financial statements covering fiscal year 2025.",
        "Confirm delivery of audited statements for the financial year 2025.",
        "Were audited statements for 2025 delivered?",
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


def test_missing_comparison_sources_are_advisory() -> None:
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
        issues = response_advisories(AIMessage("Gross profit fell 31.48%. " + citations), messages)
        assert any("GrossProfit comparison needs its own" in issue for issue in issues)
    assert (
        response_issues(
            AIMessage("Gross profit fell 31.48%. " + previous + " " + current), messages, 100
        )
        == []
    )


def financial_history() -> list[AnyMessage]:
    messages: list[AnyMessage] = [HumanMessage("Compare these two clients' financials.")]
    messages.append(
        AIMessage(
            "",
            tool_calls=[
                {"name": "rmprep-data_get_client_records", "args": {"uen": uen}, "id": uen}
                for uen in ("CANA-0010", "SLEE-0041")
            ],
        )
    )
    for uen in ("CANA-0010", "SLEE-0041"):
        refs = [financial_ref(uen, year) for year in (2024, 2025)]
        payload = {
            "uen": uen,
            "section": "financials",
            "records": [{"source_ref": ref} for ref in refs],
            "comparisons": [{"metric": "Revenues", "source_refs": refs}],
        }
        messages.append(
            ToolMessage(
                name="rmprep-data_get_client_records",
                tool_call_id=uen,
                content=frame_untrusted("rmprep-data.get_client_records", json.dumps(payload)),
            )
        )
    messages.extend([AIMessage("Comparison saved."), HumanMessage("Which one is bigger?")])
    return messages


def financial_ref(uen: str, year: int) -> str:
    return f"financial_facts[uen={uen};concept=Revenues;period_end={year}-12-31]"


@pytest.mark.parametrize(
    "claim",
    [
        "FY2025 revenue is higher for Canada Goose than Sleep Country.",
        "Revenue comparison: Canada Goose is 25% bigger than Sleep Country in FY2025.",
        "Canada Goose's FY2025 revenue is USD 919.8M versus Sleep Country's USD 732.4M.",
        "| Revenue | Canada Goose FY2025 | Sleep Country FY2025 |",
    ],
)
def test_same_period_company_comparison_does_not_require_previous_year(claim: str) -> None:
    reply = AIMessage(
        claim + " " + financial_ref("CANA-0010", 2025) + " " + financial_ref("SLEE-0041", 2025)
    )
    assert response_issues(reply, financial_history(), 350) == []


@pytest.mark.parametrize(
    "claim",
    [
        "Revenue grew 1.97%.",
        "FY2025 revenue is higher than FY2024.",
        "### Year-over-year changes\nRevenue: +1.97%.",
        "| Metric | FY2024 | FY2025 |\n| Revenue | 902 | 920 |",
    ],
)
def test_temporal_claim_records_advisory_for_the_cited_client(claim: str) -> None:
    reply = AIMessage(claim + " " + financial_ref("CANA-0010", 2025))
    assert not response_issues(reply, financial_history(), 350)
    issues = response_advisories(reply, financial_history())
    assert len(issues) == 1
    assert financial_ref("CANA-0010", 2024) in issues[0]
    assert "SLEE-0041" not in issues[0]


def test_a_trend_for_one_client_does_not_force_previous_year_for_another() -> None:
    reply = AIMessage(
        "Canada Goose revenue grew 1.97%. "
        + financial_ref("CANA-0010", 2024)
        + " "
        + financial_ref("CANA-0010", 2025)
        + "\nSleep Country FY2025 revenue is USD 732.4M. "
        + financial_ref("SLEE-0041", 2025)
    )
    assert response_issues(reply, financial_history(), 350) == []


async def test_bigger_company_followup_accepts_first_answer_without_revisions() -> None:
    calls = 0
    reply = AIMessage(
        "Canada Goose is bigger by FY2025 revenue. "
        + financial_ref("CANA-0010", 2025)
        + " "
        + financial_ref("SLEE-0041", 2025)
    )

    async def model(messages: Any) -> AIMessage:
        nonlocal calls
        calls += 1
        return reply

    history = financial_history()
    result = await build_specialist(RunnableLambda(model), [], "Use evidence.").ainvoke(
        {"messages": history, "active_client_uens": ["CANA-0010", "SLEE-0041"]}
    )
    assert calls == 1
    assert result["messages"][-1].content == reply.content
    assert len(result["messages"]) == len(history) + 1


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("## Covenant breaches\n\n- DSCR 1.08\n- FCC 0.96\n\n---", 6),
        ("### Dates\n1. March 31\n2) June 30\n***\n_ _ _", 5),
        ("   - Ratio 0.96\n  ## Test date", 4),
        ("Revenue - costs = income.\nC++ and C#", 8),
        ("#hashtag -0.17 1.08 2026-09-28", 4),
        ("    - literal indented code", 4),
        ("`## literal heading`", 3),
        ("```text\n## literal heading\n- literal bullet\n---\n```", 9),
        ("~~~~\n## code\n~~~\n- still code\n~~~~\n- prose", 9),
        ("```\n~~~\n## code\n```\n- prose", 6),
        ("- " + REF, 3),
        ("- records[name=First - Second;unit=USD million]", 4),
        ("| Metric | Value |\n| --- | --- |\n| DSCR | 1.08 |", 15),
    ],
)
def test_word_count_discounts_only_recognized_block_formatting(text: str, expected: int) -> None:
    assert _word_count(text) == expected


def test_formatting_does_not_relax_an_explicit_word_limit() -> None:
    answer = AIMessage("## Recorded covenant\n\n- Value 0.96. " + REF + "\n\n---")
    assert response_issues(answer, conversation(), 7) == []
    assert any("maximum is 6" in issue for issue in response_issues(answer, conversation(), 6))


@pytest.fixture
def formatted_followup() -> str:
    # The first live Jones draft was rejected at 357 whitespace-separated words, although ten
    # were Markdown markers. Retain every detail and citation in this regression.
    return (Path(__file__).parent / "fixtures/formatted_followup.md").read_text(encoding="utf-8")


def followup_evidence() -> list[AnyMessage]:
    refs = [
        "covenants[facility_id=FAC000000130;name=DSCR]",
        REF,
        "credit_exceptions[exception_id=CE-JONE-001]",
        "working_capital[uen=JONE-0022;period_end=2026-03-31]",
        "working_capital[uen=JONE-0022;period_end=2026-06-30]",
        "relationship[uen=JONE-0022]",
    ]
    return [
        HumanMessage("Explain the covenant breaches, cash conversion cycle and wallet share."),
        AIMessage(
            "",
            tool_calls=[
                {
                    "name": "rmprep-data_get_memo_evidence",
                    "args": {"uen": "JONE-0022"},
                    "id": "saved-evidence",
                }
            ],
        ),
        ToolMessage(
            name="rmprep-data_get_memo_evidence",
            tool_call_id="saved-evidence",
            content=frame_untrusted(
                "rmprep-data.get_memo_evidence",
                json.dumps({"uen": "JONE-0022", "records": [{"source_ref": ref} for ref in refs]}),
            ),
        ),
        AIMessage("The cited evidence is saved."),
        HumanMessage(
            "Explain the covenant breaches, cash conversion cycle change and wallet share "
            "from the saved evidence. Keep it within 350 words."
        ),
    ]


async def test_formatted_live_draft_is_saved_unchanged_with_one_model_call(
    formatted_followup: str,
) -> None:
    calls = 0

    async def model(messages: Any) -> AIMessage:
        nonlocal calls
        calls += 1
        return AIMessage(formatted_followup)

    assert len(formatted_followup.split()) == 357
    assert _word_count(formatted_followup) == 347
    messages = followup_evidence()
    graph = build_specialist(RunnableLambda(model), [], "Use evidence.")
    result = await graph.ainvoke({"messages": messages, "active_client": "JONE-0022"})
    assert calls == 1
    assert result["messages"][-1].content == formatted_followup
    assert len(result["messages"]) == len(messages) + 1
    assert any(
        "maximum is 346" in issue
        for issue in response_issues(AIMessage(formatted_followup), messages, 346)
    )


async def test_formatted_draft_still_requires_repair_for_an_invalid_citation(
    formatted_followup: str,
) -> None:
    invalid = formatted_followup.replace(REF, "covenants[facility_id=unknown]")
    replies = iter([AIMessage(invalid), AIMessage(formatted_followup)])
    seen: list[list[AnyMessage]] = []

    async def model(messages: list[AnyMessage]) -> AIMessage:
        seen.append(list(messages))
        return next(replies)

    result = await build_specialist(RunnableLambda(model), [], "Use evidence.").ainvoke(
        {"messages": followup_evidence(), "active_client": "JONE-0022"}
    )
    assert len(seen) == 2
    assert "do not exactly match" in str(seen[1][-1].content)
    assert result["messages"][-1].content == formatted_followup
    assert all(message.content != invalid for message in result["messages"])


def test_formatting_does_not_bypass_missing_citations_or_unknown_reporting_year() -> None:
    assert response_issues(AIMessage("## Covenant\n- Value 0.96.\n---"), conversation(), 20)
    answer = AIMessage(
        "## Verify\n- Obtain audited statements for FY2025.\n"
        "credit_exceptions[exception_id=example]\n---"
    )
    assert any(
        "does not identify a fiscal year" in issue
        for issue in response_issues(answer, reporting_conversation(), 20)
    )


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


@pytest.mark.parametrize("separator", [" ", "\n", "\n\n"])
def test_mixed_temporal_and_snapshot_claims_never_trigger_repairs(separator: str) -> None:
    claim = (
        "Canada Goose revenue grew 1.97%. "
        + financial_ref("CANA-0010", 2024)
        + " "
        + financial_ref("CANA-0010", 2025)
        + separator
        + "Sleep Country FY2025 revenue was USD 732.4M. "
        + financial_ref("SLEE-0041", 2025)
    )
    assert not response_issues(AIMessage(claim), financial_history(), 350)


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize(
    "claim,rejected",
    [
        ("Sweep account deployed Oct-25.", True),
        ("Sweep implementation completed in October 2025.", True),
        ("Lockbox launched in May 2026; sweep deployed Oct-25.", True),
        ("Sweep deployed Oct-25. Implementation is unverified.", True),
        ("Sweep was not deployed according to these records.", False),
        ("Confirm whether the sweep was deployed in October 2025.", False),
        ("Was sweep deployed in October 2025?", False),
        ("Sweep Closed Won Oct-25; implementation unverified.", False),
        ("The records do not prove that sweep was deployed.", False),
    ],
)
def test_status_only_records_do_not_establish_implementation(
    claim: str, rejected: bool, packed: bool
) -> None:
    ref = "opportunities[opportunity_id=OPP-9536]"
    page = {
        "records": [{"source_ref": ref}],
        "evidence_limits": {"implementation_outcome": "not_supplied_closed_won_is_a_sales_stage"},
    }
    payload = {"sections": {"opportunities": page}} if packed else page
    messages = [
        ToolMessage(
            name="rmprep-data_get_memo_evidence",
            tool_call_id="tool",
            content=frame_untrusted("rmprep-data.get_memo_evidence", json.dumps(payload)),
        )
    ]
    issues = response_issues(AIMessage(claim + " " + ref), messages, 1000)
    assert bool(issues) is rejected
    assert not any(
        "do not establish implementation" in issue
        for issue in response_issues(
            AIMessage(claim + " " + ref), [HumanMessage(str(payload))], 1000
        )
    )

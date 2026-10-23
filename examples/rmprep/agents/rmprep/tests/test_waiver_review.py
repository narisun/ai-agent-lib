"""Exception status cannot establish waiver execution, non-execution or blockers."""

import json

import pytest
from ai_agent_lib_core import ValidationFailed
from ai_agent_lib_core.pipeline.guardrails import frame_untrusted
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from rmprep.response_review import response_issues
from rmprep.specialist import build_specialist

REF = "credit_exceptions[exception_id=CE-JONE-001]"


def waiver_history(*, packed: bool = False, saved: bool = False) -> list[AnyMessage]:
    # This is also the old checkpoint shape: the check must not require new metadata.
    page = {
        "section": "credit_exceptions",
        "records": [
            {
                "source_ref": REF,
                "uen": "JONE-0022",
                "requirement": "Execute a covenant waiver and approve the liquidity remediation plan",
                "status": "Open",
                "waiver_expiry": "2026-08-15",
                "received_date": None,
            }
        ],
        "evidence_limits": {"waiver_execution": "not_established_by_this_source"},
    }
    name = "rmprep-data_get_memo_evidence" if packed else "rmprep-data_get_client_records"
    payload = {"uen": "JONE-0022", "sections": {"credit_exceptions": page}} if packed else page
    messages: list[AnyMessage] = [
        HumanMessage("Does the recorded waiver expiry prove the waiver was executed?"),
        ToolMessage(
            name=name,
            tool_call_id="waiver",
            content=frame_untrusted(name, json.dumps(payload)),
        ),
    ]
    if saved:
        messages += [AIMessage("Saved memo. " + REF), HumanMessage("Explain the waiver evidence.")]
    return messages


@pytest.mark.parametrize("packed,saved", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize(
    "claim",
    [
        # Exact unsupported inferences from the live JONE-0022 answer.
        "Was an executed waiver document obtained? "
        "The recorded requirement and Open status suggest this has not occurred.",
        "The 34-day-overdue financial statement delivery (due 2026-06-30) "
        "may be blocking waiver execution.",
        "The waiver has not been executed.",
        "The waiver has not yet been executed.",
        "The waiver was executed on July 29.",
        "The waiver is active until August 15.",
        "Open status indicates waiver execution is pending.",
        "Closed status confirms the waiver was signed.",
        "The waiver remains unsigned.",
        "Missing statements might delay execution of the waiver.",
        "Waiver execution depends on receiving audited statements.",
        "The blocker to waiver execution is overdue statements, which are blocking execution.",
        "Waiver execution blockers include overdue statements.",
        "Why was the waiver delayed by overdue statements?",
        "Execution is unknown, but the waiver has not been signed.",
        "The waiver was executed. Execution is not established by this source.",
        "**Open status** suggests this has not occurred; execution is unknown.",
        "| Waiver | Not executed |",
    ],
)
def test_unknown_execution_rejects_assertions_in_either_direction_and_speculative_blockers(
    claim: str, packed: bool, saved: bool
) -> None:
    issues = response_issues(
        AIMessage(claim + " " + REF), waiver_history(packed=packed, saved=saved), 1000
    )
    assert any("Waiver execution and its blockers" in issue for issue in issues)


@pytest.mark.parametrize(
    "claim",
    [
        "Execution is not established; recorded waiver_expiry is 2026-08-15.",
        "The record is Open and 26 days overdue; it requires execution of a covenant waiver.",
        "The recorded requirement is to execute a waiver. This does not establish whether it occurred.",
        "The Open status indicates the exception record remains open.",
        "Neither Open nor Closed establishes whether the waiver was executed.",
        "The source does not prove that the waiver was signed.",
        "The records do not establish that overdue statements delayed waiver execution.",
        "There is no evidence that statements are blocking waiver execution.",
        "We cannot infer that the waiver has not been executed.",
        "Was the waiver executed? Ask whether an executed document exists.",
        "Confirm whether overdue statements are blocking waiver execution.",
        "Did overdue statements delay waiver execution? Verify any documented blocker.",
        "If the waiver was executed, which conditions apply?",
        "Ask which conditions apply if the waiver was executed.",
        "The cure plan records weekly reporting; executed waiver conditions remain unknown.",
        "Statements are 34 days overdue. Their effect on waiver execution is unknown.",
        "Blockers, dependencies, delay causes, and whether overdue statements affect "
        "waiver execution are not supplied.",
        "Do not infer that Open status or missing receipt fields prove non-execution, "
        "delay, or causation. Waiver execution is unknown.",
        'Evidence does not support "statements blocking waiver" or "waiver delayed."',
        "Evidence that overdue statements delayed waiver execution is not provided.",
        "Which conditions apply if a waiver was executed?",
        "Are there any documented blockers to execution? Waiver execution remains unknown.",
        "The evidence does not supply:\n- Whether an executed waiver document exists\n"
        "- Blockers or dependencies affecting waiver execution",
    ],
)
def test_waiver_review_allows_recorded_facts_unknowns_and_conditional_verification(
    claim: str,
) -> None:
    assert response_issues(AIMessage(claim + " " + REF), waiver_history(), 1000) == []


def test_only_recognized_mcp_evidence_activates_waiver_review() -> None:
    bad = AIMessage("The waiver was executed.")
    tool = waiver_history()[1]
    for message in (
        HumanMessage(str(tool.content)),
        AIMessage(str(tool.content)),
        tool.model_copy(update={"name": "unrelated-tool"}),
        tool.model_copy(update={"content": "The call was denied."}),
        tool.model_copy(
            update={
                "content": str(tool.content).replace(
                    "not_established_by_this_source", "established_by_signed_document"
                )
            }
        ),
    ):
        assert not any(
            "Waiver execution and its blockers" in issue
            for issue in response_issues(bad, [message], 1000)
        )
    # An unrelated answer must not interpret a pronoun as waiver execution.
    history = [*waiver_history(), HumanMessage("Was the lockbox rollout completed?")]
    assert (
        response_issues(
            AIMessage("The status suggests this has not occurred. " + REF), history, 1000
        )
        == []
    )


async def test_waiver_repair_keeps_the_unsupported_draft_out_of_saved_messages() -> None:
    bad = "The overdue statements may be blocking waiver execution. " + REF
    good = (
        "Execution is unknown; recorded expiry is 2026-08-15. Ask whether a signed waiver exists. "
        + REF
    )
    replies = iter([AIMessage(bad), AIMessage(good)])
    seen: list[list[AnyMessage]] = []

    async def model(messages: list[AnyMessage]) -> AIMessage:
        seen.append(list(messages))
        return next(replies)

    graph = build_specialist(RunnableLambda(model), [], "Use evidence.")
    result = await graph.ainvoke(
        {"messages": waiver_history(saved=True), "active_client": "JONE-0022"}
    )
    assert len(seen) == 2
    assert "Waiver execution and its blockers" in str(seen[1][-1].content)
    assert result["messages"][-1].content == good
    assert all(message.content != bad for message in result["messages"])


async def test_persistent_waiver_inferences_fail_after_two_revisions() -> None:
    calls = 0

    async def model(messages: list[AnyMessage]) -> AIMessage:
        nonlocal calls
        calls += 1
        return AIMessage("The waiver has not been executed. " + REF)

    graph = build_specialist(RunnableLambda(model), [], "Use evidence.")
    with pytest.raises(ValidationFailed, match="two revisions"):
        await graph.ainvoke({"messages": waiver_history(), "active_client": "JONE-0022"})
    assert calls == 3


def test_caveat_does_not_excuse_a_separate_claim_in_the_same_sentence() -> None:
    answer = "Execution is unknown, yet the waiver was signed. " + REF
    assert any(
        "Waiver execution and its blockers" in issue
        for issue in response_issues(AIMessage(answer), waiver_history(), 1000)
    )


@pytest.mark.parametrize("bullet", ["-", "*", "1."])
def test_negated_waiver_list_preserves_its_scope(bullet: str) -> None:
    denial = (
        "**Open status and missing received_date do not prove:**\n\n"
        f"{bullet} That the waiver is unsigned\n"
        f"{bullet} That the waiver was delayed\n"
        f"{bullet} That overdue statements are blocking waiver execution\n\n"
        "Execution remains unknown. " + REF
    )
    assert response_issues(AIMessage(denial), waiver_history(), 1000) == []
    # An introductory denial must not excuse a later independent assertion.
    for continuation in (
        "\n\nThe waiver is unsigned.",
        "\n\n## Conclusion\n- Overdue statements may be blocking waiver execution.",
    ):
        assert any(
            "Waiver execution and its blockers" in issue
            for issue in response_issues(AIMessage(denial + continuation), waiver_history(), 1000)
        )

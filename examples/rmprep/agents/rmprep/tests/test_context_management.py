"""Memo selection, evidence isolation and bounded input with deterministic model decisions."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from ai_agent_lib_core import Principal, RequestContext, ValidationFailed
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.pipeline.guardrails import frame_untrusted
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from rmprep.context import ContextBudget, ContextBuilder, estimate_tokens
from rmprep.graph import APPLICATION, answer, build_graph, response
from rmprep.memos import MemoVersion, memo_from_turn, merge_memos
from rmprep.router import router
from rmprep.selection import select_context
from rmprep.specialist import build_specialist
from rmprep.state import State

JONES = "JONE-0022"
SHIN = "SHIN-0039"


def evidence(uen: str, meeting_date: str) -> dict[str, Any]:
    return {
        "status": "found",
        "uen": uen,
        "meeting_date": meeting_date,
        "sections": {
            "profile": {
                "records": [
                    {
                        "client_name": "Jones Dairy Farm" if uen == JONES else "Shin Company",
                        "source_ref": f"clients[uen={uen}]",
                    }
                ]
            },
            "covenants": {
                "records": [
                    {"source_ref": f"covenants[uen={uen};date={meeting_date}]", "value": 1.04}
                ],
                "evidence_limits": {"waiver": "not established"},
            },
            "financials": {
                "records": [
                    {"source_ref": f"financials[uen={uen};date={meeting_date}]", "value": 100}
                ]
            },
        },
        "limitations": ["Snapshot; not a live banking feed."],
    }


def snapshot(uen: str, day: str, index: int) -> tuple[list[AnyMessage], MemoVersion]:
    ref = f"covenants[uen={uen};date={day}]"
    messages: list[AnyMessage] = [
        HumanMessage(f"memo: {uen} as of {day}", id=f"human-{index}"),
        call(uen, day).model_copy(update={"id": f"call-{index}"}),
        ToolMessage(json.dumps(evidence(uen, day)), tool_call_id="tool-call", id=f"tool-{index}"),
        AIMessage(f"Memo {index}: {uen} {day}. {ref}", id=f"answer-{index:012}"),
    ]
    messages[2] = messages[2].model_copy(
        update={
            "name": "rmprep-data_get_memo_evidence",
            "content": frame_untrusted(
                "rmprep-data.get_memo_evidence", json.dumps(evidence(uen, day))
            ),
        }
    )
    memo = memo_from_turn(messages, [], "2026-10-11T10:00:00+00:00")
    assert memo is not None
    return messages, memo


def call(uen: str, day: str, *, name: str = "get_memo_evidence") -> AIMessage:
    return AIMessage(
        "",
        tool_calls=[
            {
                "name": "rmprep-data_" + name,
                "args": {
                    "uen": uen,
                    "meeting_date" if name == "get_memo_evidence" else "end_date": day,
                },
                "id": "tool-call",
            }
        ],
    )


@tool("rmprep-data_get_memo_evidence")
async def memo_tool(uen: str, meeting_date: str) -> str:
    """Retrieve a deterministic evidence pack for one client and meeting date."""
    return frame_untrusted("rmprep-data.get_memo_evidence", json.dumps(evidence(uen, meeting_date)))


def caller(thread: str = "context-test") -> RequestContext:
    return RequestContext(
        principal=Principal(subject="developer", tenant="bank"),
        application=APPLICATION,
        request_id="req-1",
        thread_id=thread,
    )


def catalog() -> tuple[list[AnyMessage], list[MemoVersion]]:
    history: list[AnyMessage] = []
    memos: list[MemoVersion] = []
    for index, (uen, day) in enumerate(
        [(JONES, "2026-07-03"), (JONES, "2026-08-03"), (SHIN, "2026-08-03")], 1
    ):
        messages, memo = snapshot(uen, day, index)
        memo["version"] = 1 + sum(item["uen"] == uen for item in memos)
        history.extend(messages)
        memos.append(memo)
    return history, memos


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Explain the previous memo.", [0]),
        ("Explain version 1.", [0]),
        ("Explain the July memo.", [0]),
        ("Explain the memo as of 2026-07-03.", [0]),
        ("Explain the latest memo.", [1]),
        ("Explain this memo.", [1]),
        ("Compare version 1 and version 2.", [0, 1]),
        ("Compare versions 1 and 2.", [0, 1]),
        ("Compare the first memo with the latest memo.", [0, 1]),
        ("Compare the July memo with the August memo.", [0, 1]),
        ("Compare the latest memo with the previous memo.", [0, 1]),
        ("Explain the SHIN-0039 memo.", [2]),
        ("Explain the Shin Company memo.", [2]),
    ],
)
def test_selects_versions_by_client_date_and_reference(question: str, expected: list[int]) -> None:
    _, memos = catalog()
    selection = select_context(question, memos, memos[1]["memo_id"], JONES, creating_memo=False)
    assert selection["memo_ids"] == [memos[index]["memo_id"] for index in expected]
    assert not selection["clarification"]


def test_explicit_memo_id_can_switch_back_to_a_different_client() -> None:
    _, memos = catalog()
    selection = select_context(
        f"Explain {memos[0]['memo_id']}", memos, memos[2]["memo_id"], SHIN, creating_memo=False
    )
    assert selection["uen"] == JONES
    assert selection["meeting_date"] == "2026-07-03"


@pytest.mark.parametrize(
    "question",
    [
        "Explain memo-ffffffffffff.",
        "Explain version 9.",
        "Explain the September memo.",
        "Explain the memo as of 2025-07-03.",
        "Explain yesterday's memo.",
        "Explain the memo for another company.",
    ],
)
def test_missing_historical_references_require_clarification(question: str) -> None:
    _, memos = catalog()
    selection = select_context(question, memos, memos[1]["memo_id"], JONES, creating_memo=False)
    assert selection["clarification"]


def test_same_date_versions_and_multiple_older_memos_are_ambiguous() -> None:
    _, memos = catalog()
    third: MemoVersion = {**memos[1], "memo_id": "memo-000000000004", "version": 3}
    memos.append(third)
    for question in ["Explain the August memo.", "Explain an earlier memo."]:
        selected = select_context(question, memos, third["memo_id"], JONES, creating_memo=False)
        assert selected["clarification"]
    same_date = [memos[1], third]
    selected = select_context(
        "Explain the August memo.", same_date, third["memo_id"], JONES, creating_memo=False
    )
    assert selected["clarification"]


def test_old_memo_selection_excludes_newer_and_other_client_evidence() -> None:
    messages, memos = catalog()
    question = "Explain the JONE-0022 July memo's covenants."
    state: State = {"messages": [*messages, HumanMessage(question, id="current")], "memos": memos}
    before = deepcopy(state)
    selection = select_context(question, memos, memos[-1]["memo_id"], SHIN, creating_memo=False)
    built = ContextBuilder().build(state, "System", selection)
    wire = str([message.content for message in built.messages])
    assert "2026-07-03" in wire and "SHIN-0039" not in wire
    assert "Memo 2:" not in wire and "Memo 3:" not in wire
    selected_tool = next(message for message in built.messages if isinstance(message, ToolMessage))
    assert '"covenants": {' in str(selected_tool.content)
    assert '"financials": {' not in str(selected_tool.content)
    assert "waiver" in str(selected_tool.content)
    assert state == before


def test_long_discussion_is_bounded_without_losing_selected_memo_or_tool_pairs() -> None:
    messages, memos = catalog()
    selection = select_context(
        "Explain this memo.", memos, memos[0]["memo_id"], JONES, creating_memo=False
    )
    for index in range(70):
        messages.extend(
            [
                HumanMessage(f"Discussion {index}: " + "detail " * 80, id=f"q-{index}"),
                AIMessage(
                    "Background answer " * 100,
                    id=f"a-{index}",
                    additional_kwargs={"rmprep_context": selection},
                ),
            ]
        )
    messages.append(HumanMessage("Explain this memo.", id="current"))
    state: State = {"messages": messages, "memos": memos}
    budget = ContextBudget(
        input_tokens=5_000, question_tokens=1_000, recent_tokens=1_000, tool_schema_tokens=100
    )
    built = ContextBuilder(budget).build(state, "System", selection)
    assert estimate_tokens(built.messages) + budget.tool_schema_tokens <= budget.input_tokens
    assert len(state["messages"]) == 153
    assert memos[0]["content"] in [message.content for message in built.messages]
    assert built.diagnostics["omitted_turns"] > 60  # type: ignore[operator]
    assert built.diagnostics["discussion_summary"]
    for index, message in enumerate(built.messages):
        if isinstance(message, ToolMessage):
            assert isinstance(built.messages[index - 1], AIMessage)
            assert built.messages[index - 1].tool_calls[0]["id"] == message.tool_call_id  # type: ignore[union-attr]


def test_oversized_current_question_is_not_silently_truncated() -> None:
    selection = select_context("Hello", [], "", "", creating_memo=False)
    budget = ContextBudget(
        input_tokens=1_000, question_tokens=50, recent_tokens=100, tool_schema_tokens=100
    )
    with pytest.raises(ValidationFailed, match="Split it"):
        ContextBuilder(budget).build(
            {"messages": [HumanMessage("question " * 100)]}, "System", selection
        )


async def test_multiple_memos_are_immutable_and_historical_selection_survives_later_turns() -> None:
    kit = Fakes(
        model=FakeChatModelProvider(
            [
                call(JONES, "2026-07-03"),
                "July memo. covenants[uen=JONE-0022;date=2026-07-03]",
                call(JONES, "2026-08-03"),
                "August memo. covenants[uen=JONE-0022;date=2026-08-03]",
                call(SHIN, "2026-08-03"),
                "Shin memo. covenants[uen=SHIN-0039;date=2026-08-03]",
                "July explanation.",
                "More July details.",
            ]
        )
    )
    async with kit.container() as services:
        graph = build_graph(services, [memo_tool])
        for uen, day in [(JONES, "2026-07-03"), (JONES, "2026-08-03"), (SHIN, "2026-08-03")]:
            result = await answer(graph, services, caller(), f"memo: {uen} as of {day}")
            assert "Memo reference: memo-" in result
        with bind_request_context(caller()):
            saved = await graph.aget_state(services.invocation(caller())["config"])
        memos: list[MemoVersion] = saved.values["memos"]
        assert [memo["version"] for memo in memos] == [1, 2, 1]
        frozen = deepcopy(memos)
        await answer(graph, services, caller(), f"Explain {memos[0]['memo_id']}.")
        await answer(graph, services, caller(), "Explain this memo in more detail.")
        with bind_request_context(caller()):
            saved = await graph.aget_state(services.invocation(caller())["config"])
        assert saved.values["memos"] == frozen
        assert saved.values["active_memo_id"] == memos[0]["memo_id"]
        assert saved.values["active_client"] == JONES
    for prompt in kit.model.models[0].calls[-2:]:
        wire = str([message.content for message in prompt])
        assert "July memo." in wire and "August memo." not in wire and "Shin memo." not in wire
    with pytest.raises(ValueError, match="cannot be changed"):
        merge_memos(frozen, [{**frozen[0], "content": "changed"}])


async def test_memo_output_publishes_only_new_accepted_versions_and_preserves_answer_api() -> None:
    kit = Fakes(
        model=FakeChatModelProvider(
            [
                call(JONES, "2026-07-03"),
                "July memo. covenants[uen=JONE-0022;date=2026-07-03]",
                "A follow-up answer.",
                call(JONES, "2026-08-03"),
                "August memo. covenants[uen=JONE-0022;date=2026-08-03]",
                call(SHIN, "2026-08-03"),
                "Shin memo. covenants[uen=SHIN-0039;date=2026-08-03]",
            ]
        )
    )
    async with kit.container() as services:
        graph = build_graph(services, [memo_tool])
        first = await response(graph, services, caller(), f"memo: {JONES} as of 2026-07-03")
        assert first["artifacts"][0]["content"] == first["answer"]
        assert first["artifacts"][0]["thread_id"] == caller().thread_id
        assert first["artifacts"][0]["request_id"] == caller().request_id
        followup = await response(graph, services, caller(), "Explain this memo.")
        assert followup == {"answer": "A follow-up answer."}
        second = await response(graph, services, caller(), f"memo: {JONES} as of 2026-08-03")
        assert [first["artifacts"][0]["version"], second["artifacts"][0]["version"]] == [1, 2]
        assert first["artifacts"][0]["document_id"] == second["artifacts"][0]["document_id"]
        # Existing callers of answer() still receive a string.
        third = await answer(graph, services, caller(), f"memo: {SHIN} as of 2026-08-03")
        assert "Shin memo" in third


async def test_ambiguous_reference_does_not_call_model_or_change_active_memo() -> None:
    messages, memos = catalog()
    graph = build_specialist(
        RunnableLambda(lambda _: pytest.fail("Model must not run")), [], "System"
    )
    result = await graph.ainvoke(
        {
            "messages": [*messages, HumanMessage("Explain version 9.")],
            "memos": memos,
            "active_memo_id": memos[1]["memo_id"],
            "active_client": JONES,
        }
    )
    assert "Please specify" in result["messages"][-1].content
    assert result["active_memo_id"] == memos[1]["memo_id"]


async def test_wrong_client_tool_call_is_repaired_before_execution() -> None:
    messages, memos = catalog()
    responses = iter(
        [
            call(SHIN, "2026-07-03"),
            call(JONES, "2026-07-03"),
            AIMessage("Saved covenant. covenants[uen=JONE-0022;date=2026-07-03]"),
        ]
    )
    seen: list[list[AnyMessage]] = []

    def model(items: list[AnyMessage]) -> AIMessage:
        seen.append(list(items))
        return next(responses)

    graph = build_specialist(RunnableLambda(model), [memo_tool], "System")
    result = await graph.ainvoke(
        {
            "messages": [
                *messages,
                HumanMessage(f"Retrieve the evidence for {memos[0]['memo_id']}."),
            ],
            "memos": memos,
            "active_memo_id": memos[-1]["memo_id"],
            "active_client": SHIN,
        }
    )
    new_tools = [
        message
        for message in result["messages"][len(messages) :]
        if isinstance(message, ToolMessage)
    ]
    assert (
        len(new_tools) == 1 and JONES in new_tools[0].content and SHIN not in new_tools[0].content
    )
    assert "Use only the selected client" in str(seen[1][-1].content)


async def test_refresh_uses_today_and_keeps_original_memo_date() -> None:
    messages, memos = catalog()
    responses = iter(
        [
            call(JONES, "2026-07-03"),
            call(JONES, "2026-10-11"),
            AIMessage("Updated evidence. covenants[uen=JONE-0022;date=2026-10-11]"),
        ]
    )
    graph = build_specialist(
        RunnableLambda(lambda _: next(responses)),
        [memo_tool],
        "System",
        now=lambda: datetime(2026, 10, 11, tzinfo=UTC),
    )
    result = await graph.ainvoke(
        {
            "messages": [
                *messages,
                HumanMessage(
                    f"What has changed since {memos[0]['memo_id']}? Retrieve current evidence."
                ),
            ],
            "memos": memos,
            "active_memo_id": memos[0]["memo_id"],
            "active_client": JONES,
        }
    )
    assert result["memos"] == memos
    assert result["context_diagnostics"]["mode"] == "refresh"
    assert "2026-10-11" in result["messages"][-2].content
    assert result["context_diagnostics"]["retrieval_date"] == "2026-10-11"


async def test_client_lookup_alone_cannot_support_a_refresh_answer() -> None:
    messages, memos = catalog()
    graph = build_specialist(RunnableLambda(lambda _: AIMessage("No changes found.")), [], "System")
    with pytest.raises(ValidationFailed, match="two revisions"):
        await graph.ainvoke(
            {
                "messages": [
                    *messages,
                    HumanMessage("What has changed since this memo?"),
                    ToolMessage(
                        '{"resolved_uen":"JONE-0022"}',
                        name="rmprep-data_find_clients",
                        tool_call_id="lookup",
                    ),
                ],
                "memos": memos,
                "active_memo_id": memos[0]["memo_id"],
                "active_client": JONES,
            }
        )


def test_new_client_by_name_or_explicit_date_does_not_inherit_previous_memo() -> None:
    _, memos = catalog()
    for question in [
        "memo: Prepare a memo for Another Company.",
        "What are the covenants for Another Company?",
    ]:
        selection = select_context(
            question, memos, memos[0]["memo_id"], JONES, creating_memo=question.startswith("memo:")
        )
        assert not selection["memo_ids"] and not selection["uen"]
        assert selection["mode"] == "new"
    selection = select_context(
        "Show JONE-0022 payments from 2026-09-01 through 2026-09-30.",
        memos,
        memos[0]["memo_id"],
        JONES,
        creating_memo=False,
    )
    assert not selection["memo_ids"]
    assert selection["meeting_date"] == "2026-09-30"
    same_client = select_context(
        "memo: Prepare a new memo for this client.",
        memos,
        memos[0]["memo_id"],
        JONES,
        creating_memo=True,
    )
    assert same_client["uen"] == JONES
    assert same_client["meeting_date"] == "2026-07-03"


async def test_switch_to_client_without_a_memo_clears_previous_document() -> None:
    messages, memos = catalog()
    captured: list[list[AnyMessage]] = []

    def model(items: list[AnyMessage]) -> AIMessage:
        captured.append(items)
        return AIMessage("Please provide the requested client details.")

    graph = build_specialist(RunnableLambda(model), [], "System")
    result = await graph.ainvoke(
        {
            "messages": [*messages, HumanMessage("Explain WAWO-0046's credit position.")],
            "memos": memos,
            "active_memo_id": memos[0]["memo_id"],
            "active_client": JONES,
        }
    )
    assert result["active_client"] == "WAWO-0046"
    assert result["active_memo_id"] == ""
    assert "JONE-0022" not in str([message.content for message in captured[0]])


async def test_model_input_budget_also_applies_after_a_large_tool_response() -> None:
    messages, memos = catalog()
    huge_result = ToolMessage("large " * 20_000, tool_call_id="huge", id="huge")
    graph = build_specialist(
        RunnableLambda(lambda _: pytest.fail("Oversized input must not reach model")),
        [],
        "System",
        context_builder=ContextBuilder(
            ContextBudget(
                input_tokens=3_000, question_tokens=1_000, recent_tokens=500, tool_schema_tokens=100
            )
        ),
    )
    with pytest.raises(ValidationFailed, match="one memo section"):
        await graph.ainvoke(
            {
                "messages": [
                    *messages,
                    HumanMessage("Explain this memo."),
                    AIMessage("", tool_calls=[{"name": "large", "args": {}, "id": "huge"}]),
                    huge_result,
                ],
                "memos": memos,
                "active_memo_id": memos[0]["memo_id"],
                "active_client": JONES,
            }
        )


async def test_rejected_drafts_do_not_create_versions() -> None:
    messages, memos = catalog()
    selected = select_context(
        "memo: JONE-0022 as of 2026-07-03.", memos, memos[0]["memo_id"], JONES, creating_memo=True
    )
    responses = iter(
        [
            AIMessage("covenants[not-a-real-reference]"),
            AIMessage("Accepted memo. covenants[uen=JONE-0022;date=2026-07-03]"),
        ]
    )
    graph = build_specialist(RunnableLambda(lambda _: next(responses)), [memo_tool], "System")
    result = await graph.ainvoke(
        {"messages": messages[:3], "memos": memos, "route": "memoagent", "selection": selected}
    )
    assert len(result["memos"]) == 4
    assert result["memos"][-1]["version"] == 3
    assert "not-a-real-reference" not in str(result["memos"])


def test_new_memo_retrieval_does_not_reuse_old_evidence() -> None:
    messages, memos = catalog()
    question = "memo: JONE-0022 as of 2026-10-11."
    selection = select_context(question, memos, memos[0]["memo_id"], JONES, creating_memo=True)
    built = ContextBuilder().build(
        {"messages": [*messages, HumanMessage(question)], "memos": memos}, "System", selection
    )
    assert len(built.messages) == 2
    assert built.messages[-1].content == question


def test_comparison_contains_both_selected_snapshots_and_no_other_client() -> None:
    messages, memos = catalog()
    question = "Compare version 1 with version 2."
    selection = select_context(question, memos, memos[1]["memo_id"], JONES, creating_memo=False)
    built = ContextBuilder().build(
        {"messages": [*messages, HumanMessage(question)], "memos": memos}, "System", selection
    )
    assert built.diagnostics["memo_ids"] == [memos[0]["memo_id"], memos[1]["memo_id"]]
    assert [
        message.content
        for message in built.messages
        if isinstance(message, AIMessage) and not message.tool_calls
    ] == [memos[0]["content"], memos[1]["content"]]
    assert SHIN not in str([message.content for message in built.messages])


def test_older_checkpoints_are_indexed_without_rewriting_messages_or_inventing_dates() -> None:
    messages, _ = catalog()
    state: State = {
        "messages": [*messages, HumanMessage("Explain the JONE-0022 July memo.")],
        "memos": [],
    }
    original = deepcopy(state["messages"])
    routed = router(state)
    assert routed["context_version"] == 2
    assert state["messages"] == original
    adopted: Any = routed["memos"]
    assert len(adopted) == 3
    assert all(memo["generated_at"] == "unknown (legacy checkpoint)" for memo in adopted)
    assert [memo["version"] for memo in adopted] == [1, 2, 1]
    assert routed["selection"]["memo_ids"] == [adopted[0]["memo_id"]]  # type: ignore[index]


@pytest.mark.parametrize("event_date", ["October 2025", "2025-10-20", "last month"])
@pytest.mark.parametrize("multiple", [False, True])
def test_dates_in_memo_contents_do_not_select_a_historical_document(
    event_date: str, multiple: bool
) -> None:
    _, memos = catalog()
    active = memos[1]
    selection = select_context(
        f"Recheck the memo's statement that the sweep was deployed in {event_date}.",
        memos if multiple else [active],
        active["memo_id"],
        JONES,
        creating_memo=False,
    )
    assert selection["memo_ids"] == [active["memo_id"]]
    assert selection["meeting_date"] == "2026-08-03"
    assert not selection["clarification"]


@pytest.mark.parametrize(
    "question", ["Explain the July 2025 memo.", "Explain the memo from July 2025."]
)
def test_month_and_year_select_the_document_in_the_correct_year(question: str) -> None:
    _, older = snapshot(JONES, "2025-07-03", 1)
    _, newer = snapshot(JONES, "2026-07-03", 2)
    selection = select_context(
        question, [older, newer], newer["memo_id"], JONES, creating_memo=False
    )
    assert selection["memo_ids"] == [older["memo_id"]]
    assert not selection["clarification"]


@pytest.mark.parametrize("explicit_limit", [False, True])
async def test_broad_memo_has_room_for_sections_but_explicit_limit_still_applies(
    explicit_limit: bool,
) -> None:
    draft = "Supported detail " * 370 + "covenants[uen=JONE-0022;date=2026-08-03]"
    short = "Concise memo. covenants[uen=JONE-0022;date=2026-08-03]"
    kit = Fakes(model=FakeChatModelProvider([call(JONES, "2026-08-03"), draft, short]))
    async with kit.container() as services:
        graph = build_graph(services, [memo_tool])
        output = await response(
            graph,
            services,
            caller(),
            "memo: JONE-0022 as of 2026-08-03" + (" within 600 words" if explicit_limit else ""),
        )
    assert output["answer"].startswith(short if explicit_limit else draft)
    assert len(kit.model.models[0].calls) == (3 if explicit_limit else 2)
    assert output["artifacts"][0]["content"] == output["answer"]


async def test_revised_memo_has_one_application_owned_reference() -> None:
    messages, _ = snapshot(JONES, "2026-08-03", 1)
    fake_footer = "Memo reference: memo-aaaaaaaaaaaa | version 1 | JONE-0022 | 2026-08-03"
    replies = iter(
        [
            AIMessage("Invalid citation covenants[uen=wrong]"),
            AIMessage("Accepted memo. covenants[uen=JONE-0022;date=2026-08-03]\n\n" + fake_footer),
        ]
    )
    seen: list[list[AnyMessage]] = []

    def model(items: list[AnyMessage]) -> AIMessage:
        seen.append(items)
        return next(replies)

    graph = build_specialist(
        RunnableLambda(model), [memo_tool], "Use evidence.", default_word_limit=1000
    )
    result = await graph.ainvoke({"messages": messages[:-1], "route": "memoagent"})
    accepted = result["messages"][-1].text
    assert accepted.count("Memo reference:") == 1
    assert "memo-aaaaaaaaaaaa" not in accepted
    assert result["memos"][0]["memo_id"] in accepted
    assert result["memos"][0]["content"] == accepted
    assert len(result["memos"]) == 1
    assert "Memo reference:" not in seen[1][-2].text

"""Named-client follow-ups retain the accepted memo and its governed evidence."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

import pytest
from ai_agent_lib_core import Principal, RequestContext
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.pipeline.guardrails import frame_untrusted
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from rmprep.clients import client_names, resolved_clients
from rmprep.context import ContextBuilder, estimate_tokens
from rmprep.graph import APPLICATION, answer, build_graph
from rmprep.response_review import tool_payload
from rmprep.router import router
from rmprep.selection import select_context
from rmprep.state import State

CLIENT = "CANA-0010"
NAME = "Canada Goose Holdings Inc."
QUESTION = "how did you calculate wallet share for canada goose?"
MEMO = "Canada Goose wallet share: 22.55%. Source: relationship[uen=CANA-0010]."
EXPLANATION = (
    "The memo reports 22.55% from relationship[uen=CANA-0010]. "
    "It is a recorded percentage, not a calculation I performed. "
    "The source does not supply the numerator, denominator or methodology."
)


def lookup_payload(*, status: str = "resolved", truncated: bool = False) -> dict[str, Any]:
    return {
        "status": status,
        "truncated": truncated,
        "resolved_uen": CLIENT,
        "matches": [{"uen": CLIENT, "name": NAME}],
    }


def lookup_messages(**overrides: Any) -> list[AnyMessage]:
    return [
        AIMessage(
            "",
            id="lookup-call",
            tool_calls=[
                {
                    "name": "rmprep-data_find_clients",
                    "args": {"search": "Canada Goose"},
                    "id": "lookup",
                }
            ],
        ),
        ToolMessage(
            frame_untrusted("rmprep-data.find_clients", json.dumps(lookup_payload(**overrides))),
            name="rmprep-data_find_clients",
            tool_call_id="lookup",
            id="lookup-result",
        ),
    ]


def evidence_payload() -> dict[str, Any]:
    return {
        "status": "found",
        "uen": CLIENT,
        "meeting_date": "2026-10-13",
        "sections": {
            "profile": {"records": [{"client_name": NAME, "source_ref": f"clients[uen={CLIENT}]"}]},
            "relationship": {
                "records": [
                    {"wallet_share_pct": "22.55", "source_ref": f"relationship[uen={CLIENT}]"}
                ],
                "evidence_limits": {"methodology": "Not provided in the source."},
            },
            "financials": {"records": [{"details": "Unrelated financial detail. " * 3000}]},
        },
        "limitations": ["Curated snapshot; calculation inputs unavailable."],
    }


def saved_memo() -> list[AnyMessage]:
    return [
        HumanMessage("prepare rm prep for canada goose", id="question-1"),
        *lookup_messages(),
        AIMessage(
            "",
            id="evidence-call",
            tool_calls=[
                {
                    "name": "rmprep-data_get_memo_evidence",
                    "args": {"uen": CLIENT, "meeting_date": "2026-10-13"},
                    "id": "evidence",
                }
            ],
        ),
        ToolMessage(
            frame_untrusted("rmprep-data.get_memo_evidence", json.dumps(evidence_payload())),
            name="rmprep-data_get_memo_evidence",
            tool_call_id="evidence",
            id="evidence-result",
        ),
        AIMessage(
            MEMO,
            id="memo-answer",
            additional_kwargs={"rmprep_context": {"uen": CLIENT, "memo_ids": [], "mode": "new"}},
        ),
    ]


def assert_wallet_context(messages: list[AnyMessage]) -> None:
    assert MEMO in [message.content for message in messages]
    result = next(message for message in messages if message.id == "evidence-result")
    payload = tool_payload(result)
    assert list(payload["sections"]) == ["relationship"]
    assert payload["sections"]["relationship"]["records"][0]["wallet_share_pct"] == "22.55"
    assert payload["limitations"] == evidence_payload()["limitations"]
    assert payload["context_omitted_sections"] == ["financials", "profile"]
    for message in messages:
        if isinstance(message, ToolMessage):
            assert any(
                call["id"] == message.tool_call_id
                for prior in messages
                if isinstance(prior, AIMessage)
                for call in prior.tool_calls
            )


def test_successful_lookup_remembers_search_and_canonical_name() -> None:
    history = lookup_messages()
    assert client_names(history, []) == {CLIENT: ["Canada Goose", NAME]}
    assert resolved_clients(history) == {CLIENT}
    assert tool_payload(history[-1]) == {}  # A lookup is not financial evidence.
    without_call = client_names(history[-1:], [])
    assert without_call == {CLIENT: [NAME]}  # Never infer a search term from the result alone.
    assert client_names([AIMessage(f"Canada Goose is {CLIENT}")], []) == {}


@pytest.mark.parametrize("overrides", [{"status": "ambiguous"}, {"truncated": True}])
def test_unresolved_lookup_cannot_establish_an_alias(overrides: dict[str, Any]) -> None:
    assert client_names(lookup_messages(**overrides), []) == {}
    assert resolved_clients(lookup_messages(**overrides)) == set()


@pytest.mark.parametrize("question", [QUESTION, "Explain the memo for Canada Goose."])
def test_shared_name_requires_clarification(question: str) -> None:
    selection = select_context(
        question,
        [],
        "",
        CLIENT,
        creating_memo=False,
        known_names={CLIENT: ["Canada Goose"], "OTHR-0001": ["Canada Goose"]},
    )
    assert selection["clarification"]
    assert not selection["uen"]


def test_existing_version_one_thread_recovers_memo_without_rewriting_history() -> None:
    state: State = {
        "messages": [
            *saved_memo(),
            HumanMessage(QUESTION, id="question-2"),
            AIMessage("I cannot see the earlier exchange.", id="bad-answer"),
            HumanMessage(QUESTION, id="question-3"),
        ],
        "memos": [],
        "context_version": 1,
        "active_client": CLIENT,
        "active_memo_id": "",
    }
    original = deepcopy(state)
    repaired = cast(State, {**state, **router(state)})
    assert state == original
    assert repaired["context_version"] == 2
    assert len(repaired["memos"]) == 1
    memo = repaired["memos"][0]
    assert memo["generated_at"] == "unknown (legacy checkpoint)"
    assert memo["content"] == MEMO
    assert repaired["selection"]["memo_ids"] == [memo["memo_id"]]
    assert repaired["selection"]["uen"] == CLIENT
    assert repaired["selection"]["mode"] == "snapshot"
    built = ContextBuilder().build(repaired, "System", repaired["selection"])
    assert_wallet_context(built.messages)
    assert "bad-answer" not in [message.id for message in built.messages]
    assert "memos" not in router(repaired)  # Migration runs once; the accepted version is stable.
    assert state == original


@pytest.mark.parametrize("tagged_other_client", [True, False])
def test_known_alias_retains_bounded_qa_history_without_a_versioned_memo(
    tagged_other_client: bool,
) -> None:
    messages = saved_memo()
    messages[0] = HumanMessage("What is the wallet share for Canada Goose?", id="question-1")
    other_messages: list[AnyMessage] = [
        HumanMessage("Show Another Company's financials.", id="other-question"),
        AIMessage(
            "",
            id="other-call",
            tool_calls=[
                {
                    "name": "rmprep-data_get_client_records",
                    "args": {"uen": "OTHR-0001"},
                    "id": "other-tool",
                }
            ],
        ),
        ToolMessage(
            frame_untrusted(
                "rmprep-data.get_client_records",
                json.dumps({"status": "found", "uen": "OTHR-0001", "records": []}),
            ),
            name="rmprep-data_get_client_records",
            tool_call_id="other-tool",
            id="other-result",
        ),
        AIMessage(
            "Other client's answer.",
            id="other-answer",
            additional_kwargs=(
                {"rmprep_context": {"uen": "OTHR-0001", "memo_ids": []}}
                if tagged_other_client
                else {}
            ),
        ),
    ]
    state: State = {
        "messages": [*other_messages, *messages, HumanMessage(QUESTION, id="question-2")],
        "memos": [],
        "context_version": 2,
        "active_client": CLIENT,
    }
    repaired = cast(State, {**state, **router(state)})
    assert not repaired["selection"]["memo_ids"]
    assert repaired["selection"]["uen"] == CLIENT
    assert repaired["selection"]["mode"] == "snapshot"
    assert estimate_tokens(messages) > 6000
    built = ContextBuilder().build(repaired, "System", repaired["selection"])
    assert_wallet_context(built.messages)
    assert built.diagnostics["omitted_turns"] == 1
    assert "other-answer" not in [message.id for message in built.messages]
    assert "financials" in tool_payload(messages[4])["sections"]


def test_unknown_client_excludes_the_saved_canada_goose_memo() -> None:
    state: State = {
        "messages": [
            *saved_memo(),
            HumanMessage("How did you calculate wallet share for Another Company?", id="current"),
        ]
    }
    repaired = cast(State, {**state, **router(state)})
    selection = repaired["selection"]
    assert selection["mode"] == "new"
    assert not selection["memo_ids"] and not selection["uen"]
    built = ContextBuilder().build(repaired, "System", selection)
    assert len(built.messages) == 2
    assert MEMO not in str(built.messages)


@tool("rmprep-data_find_clients")
async def lookup_tool(search: str) -> str:
    """Look up the canonical client identity in the governed registry."""
    return frame_untrusted("rmprep-data.find_clients", json.dumps(lookup_payload()))


@tool("rmprep-data_get_memo_evidence")
async def evidence_tool(uen: str, meeting_date: str) -> str:
    """Retrieve the client's memo evidence pack."""
    return frame_untrusted("rmprep-data.get_memo_evidence", json.dumps(evidence_payload()))


async def test_exact_reported_questions_share_memo_evidence_through_the_graph() -> None:
    history = saved_memo()
    assert isinstance(history[1], AIMessage) and isinstance(history[3], AIMessage)
    kit = Fakes(model=FakeChatModelProvider([history[1], history[3], MEMO, EXPLANATION]))
    caller = RequestContext(
        principal=Principal(subject="developer", tenant="bank"),
        application=APPLICATION,
        request_id="named-context",
        thread_id="canada-goose-regression",
    )
    async with kit.container() as services:
        graph = build_graph(services, [lookup_tool, evidence_tool])
        memo = await answer(graph, services, caller, "prepare rm prep for canada goose")
        assert "Memo reference: memo-" in memo
        response = await answer(graph, services, caller, QUESTION)
        assert response == EXPLANATION
        with bind_request_context(caller):
            saved = await graph.aget_state(services.invocation(caller)["config"])
    assert len(kit.model.models[0].calls) == 4
    prompts = kit.model.models[0].calls
    assert '"uen": "CANA-0010"' in str(prompts[1][0].content)  # Lookup binds before retrieval.
    assert memo in [message.content for message in prompts[-1]]
    source = next(
        tool_payload(m)
        for m in prompts[-1]
        if isinstance(m, ToolMessage) and "sections" in tool_payload(m)
    )
    assert list(source["sections"]) == ["relationship"]
    assert source["sections"]["relationship"]["records"][0]["wallet_share_pct"] == "22.55"
    assert len(saved.values["memos"]) == 1
    assert saved.values["active_client"] == CLIENT
    assert saved.values["selection"]["mode"] == "snapshot"
    assert saved.values["context_diagnostics"]["omitted_turns"] == 0


async def test_next_question_persists_recovered_memo_in_an_existing_thread() -> None:
    history = saved_memo()
    # Anthropic can store its accepted answer as content blocks when streaming.
    history[-1] = AIMessage([{"type": "text", "text": MEMO}], id="memo-answer")
    history += [
        HumanMessage(QUESTION, id="question-2"),
        AIMessage("I cannot see the earlier exchange.", id="bad-answer"),
    ]
    original = deepcopy(history)
    kit = Fakes(model=FakeChatModelProvider([EXPLANATION]))
    caller = RequestContext(
        principal=Principal(subject="developer", tenant="bank"),
        application=APPLICATION,
        request_id="recover-context",
        thread_id="existing-canada-goose-thread",
    )
    async with kit.container() as services:
        graph = build_graph(services)
        with bind_request_context(caller):
            await graph.aupdate_state(
                services.invocation(caller)["config"],
                {
                    "messages": history,
                    "memos": [],
                    "context_version": 1,
                    "active_client": CLIENT,
                    "active_memo_id": "",
                },
                as_node="qnaagent",
            )
        assert await answer(graph, services, caller, QUESTION) == EXPLANATION
        with bind_request_context(caller):
            saved = await graph.aget_state(services.invocation(caller)["config"])
    assert saved.values["messages"][: len(history)] == original
    assert len(saved.values["memos"]) == 1
    memo = saved.values["memos"][0]
    assert memo["content"] == MEMO
    assert saved.values["active_memo_id"] == memo["memo_id"]
    assert saved.values["selection"]["memo_ids"] == [memo["memo_id"]]
    assert saved.values["context_version"] == 2
    prompt = kit.model.models[0].calls[0]
    assert "memo-answer" in [message.id for message in prompt]
    assert "bad-answer" not in [message.id for message in prompt]

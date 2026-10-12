"""Routing, graph composition and conversation isolation with injected providers."""

from dataclasses import replace

import pytest
from ai_agent_lib_core import Principal, RequestContext
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import FakeChatModelProvider, Fakes

from rmprep import APPLICATION, answer, build_graph
from rmprep.prompts import MEMO_PROMPT, QNA_PROMPT
from rmprep.router import choose_route


def context(subject: str = "u-1", thread: str = "meeting-1") -> RequestContext:
    return RequestContext(
        principal=Principal(subject=subject, tenant="t-1"),
        application=APPLICATION,
        request_id="request-1",
        thread_id=thread,
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Prepare a meeting memo for JONE-0022.", "memoagent"),
        ("Please refresh the memo for JONE-0022.", "memoagent"),
        ("  MEMO: JONE-0022", "memoagent"),
        ("qna: How do I prepare a memo?", "qnaagent"),
        ("Explain the credit section of the memo.", "qnaagent"),
        ("What does 'prepare a memo' mean?", "qnaagent"),
        ("What is the credit utilization?", "qnaagent"),
        ("Before our next meeting, could you prepare a memo for JONE-0022?", "memoagent"),
        ("I have several questions. Explain what 'generate a memo' means.", "qnaagent"),
    ],
)
def test_routing(question: str, expected: str) -> None:
    assert choose_route(question) == expected


async def test_three_nodes_share_history_and_apply_the_specialists_prompt() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["sample memo", "sample explanation", "new answer"]))
    async with fakes.container() as services:
        graph = build_graph(services)
        assert set(graph.nodes) == {"__start__", "router", "memoagent", "qnaagent"}
        caller = context()
        assert await answer(graph, services, caller, "memo: JONE-0022") == "sample memo"
        assert await answer(graph, services, caller, "Explain the memo.") == "sample explanation"
        assert await answer(graph, services, caller, "Refresh the memo.") == "new answer"
        with bind_request_context(caller):
            snapshot = await graph.aget_state(services.invocation(caller)["config"])
        assert snapshot.values["route"] == "memoagent"
        assert len(snapshot.values["messages"]) == 6

    prompts = fakes.model.models[0].calls
    assert all(
        str(call[0].content).startswith(expected)
        for call, expected in zip(prompts, [MEMO_PROMPT, QNA_PROMPT, MEMO_PROMPT], strict=True)
    )
    assert [message.content for message in prompts[1][1:]] == [
        "memo: JONE-0022",
        "sample memo",
        "Explain the memo.",
    ]


async def test_other_callers_tenants_and_threads_do_not_inherit_the_memo() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["private memo", "one", "two", "three"]))
    async with fakes.container() as services:
        graph = build_graph(services)
        caller = context()
        await answer(graph, services, caller, "memo: JONE-0022")
        for other in [
            context(subject="u-2"),
            context(thread="meeting-2"),
            replace(caller, principal=Principal(subject="u-1", tenant="t-2")),
        ]:
            await answer(graph, services, other, "What client are we discussing?")

    for prompt in fakes.model.models[0].calls[1:]:
        assert [message.content for message in prompt[1:]] == ["What client are we discussing?"]

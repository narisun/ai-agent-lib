"""A whole graph built the ordinary LangGraph way, running on governed parts."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ai_agent_lib_core.adapters import FakeChatModelProvider
from ai_agent_lib_core.contracts import AuditOutcome, PolicyDenied, Principal, RequestContext
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.pipeline import frame_untrusted
from ai_agent_lib_core.testing import Fakes

ROLE_MARKER = "role-marker-a11ce"


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def lookup_balance(account: str) -> str:
    """Return the balance of an account."""
    return f"The balance of account {account} is 1,250.00 USD."


def context(tenant: str = "t-9", subject: str = "u-1", thread: str = "th-1") -> RequestContext:
    return RequestContext(
        principal=Principal(subject=subject, tenant=tenant, roles=frozenset({ROLE_MARKER})),
        application="accounts-agent",
        request_id="r-1",
        thread_id=thread,
    )


def build_agent(
    services: ServiceContainer, *, sync_model_node: bool = False
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Build a tool-calling agent exactly as a LangGraph author would."""
    tools: list[BaseTool] = services.tools([lookup_balance], read_only=["lookup_balance"])
    model: BaseChatModel = services.model("default")
    model_with_tools = model.bind_tools(tools)

    async def agent(state: State) -> dict[str, list[AnyMessage]]:
        return {"messages": [await model_with_tools.ainvoke(state["messages"])]}

    def agent_sync(state: State) -> dict[str, list[AnyMessage]]:
        return {"messages": [model_with_tools.invoke(state["messages"])]}

    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("agent", agent_sync if sync_model_node else agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(**services.compile_kwargs())


def script() -> FakeChatModelProvider:
    return FakeChatModelProvider(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_balance", "args": {"account": "4411"}, "id": "c-1"}],
            ),
            "Your balance is 1,250.00 USD.",
        ]
    )


def question(text: str = "What is the balance of account 4411?") -> dict[str, list[AnyMessage]]:
    return {"messages": [HumanMessage(text)]}


@pytest.mark.parametrize("sync_model_node", [False, True])
async def test_the_agent_answers_using_a_governed_model_and_tool(sync_model_node: bool) -> None:
    fakes = Fakes(model=script())
    async with fakes.container() as services:
        graph = build_agent(services, sync_model_node=sync_model_node)
        result = await graph.ainvoke(question(), **services.invocation(context()))

    kinds = [type(message) for message in result["messages"]]
    assert kinds == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert result["messages"][2].content == frame_untrusted(
        "lookup_balance", "The balance of account 4411 is 1,250.00 USD."
    )
    assert result["messages"][-1].content == "Your balance is 1,250.00 USD."

    assert [record.event for record in fakes.audit.records] == [
        "model.call",
        "tool.call",
        "model.call",
    ]
    assert {record.outcome for record in fakes.audit.records} == {AuditOutcome.SUCCESS}
    assert {(r.tenant, r.subject, r.application) for r in fakes.audit.records} == {
        ("t-9", "u-1", "accounts-agent")
    }
    assert fakes.audit.records[1].attributes["tool"] == "lookup_balance"
    assert fakes.audit.records[1].attributes["read_only"] is True


async def test_nothing_from_the_conversation_reaches_the_audit_log() -> None:
    fakes = Fakes(model=script())
    async with fakes.container() as services:
        await build_agent(services).ainvoke(question(), **services.invocation(context()))
    logged = " ".join(str(record.to_dict()) for record in fakes.audit.records)
    assert "4411" not in logged
    assert "1,250.00" not in logged
    assert "balance of account" not in logged


async def test_a_thread_keeps_its_conversation_between_requests() -> None:
    fakes = Fakes(model=FakeChatModelProvider(["first answer", "second answer"]))
    async with fakes.container() as services:
        graph = build_agent(services)
        await graph.ainvoke(question("first"), **services.invocation(context()))
        result = await graph.ainvoke(question("second"), **services.invocation(context()))
    assert [message.content for message in result["messages"]] == [
        "first",
        "first answer",
        "second",
        "second answer",
    ]


@pytest.mark.parametrize("other", [{"tenant": "t-other"}, {"subject": "u-other"}])
async def test_another_caller_using_the_same_thread_name_sees_nothing(
    other: dict[str, str],
) -> None:
    fakes = Fakes(model=FakeChatModelProvider(["for the first caller", "for the other caller"]))
    async with fakes.container() as services:
        graph = build_agent(services)
        await graph.ainvoke(question("private question"), **services.invocation(context()))
        result = await graph.ainvoke(question("hello"), **services.invocation(context(**other)))
    assert [message.content for message in result["messages"]] == ["hello", "for the other caller"]


async def test_the_request_context_never_enters_graph_state_or_the_checkpoint() -> None:
    fakes = Fakes(model=script())
    async with fakes.container() as services:
        graph = build_agent(services)
        run = services.invocation(context())
        result = await graph.ainvoke(question(), **run)
        snapshot = await graph.aget_state(run["config"])
    assert set(result) == {"messages"}
    assert ROLE_MARKER not in repr(result)
    assert ROLE_MARKER not in repr(snapshot.values)
    assert ROLE_MARKER not in repr(snapshot.metadata)
    assert ROLE_MARKER not in repr(run["config"])
    model_prompts = [message for call in fakes.model.models[0].calls for message in call]
    assert ROLE_MARKER not in repr(model_prompts)


async def test_running_without_a_request_context_is_denied_at_the_first_model_call() -> None:
    fakes = Fakes(model=script())
    async with fakes.container() as services:
        graph = build_agent(services)
        run = services.invocation(context())
        with pytest.raises(PolicyDenied) as caught:
            await graph.ainvoke(question(), run["config"])
    assert caught.value.reason_code == "identity_missing"
    assert fakes.model.models[0].calls == []
    assert [record.outcome for record in fakes.audit.records] == [AuditOutcome.DENIED]


async def test_a_raw_thread_id_is_refused_by_the_checkpointer() -> None:
    async with Fakes(model=script()).container() as services:
        graph = build_agent(services)
        with pytest.raises(PolicyDenied) as caught:
            await graph.ainvoke(
                question(), {"configurable": {"thread_id": "th-1"}}, context=context()
            )
    assert caught.value.reason_code == "unscoped_thread"


async def test_invocation_requires_a_request_context() -> None:
    async with Fakes().container() as services:
        with pytest.raises(TypeError, match="RequestContext"):
            services.invocation({"tenant": "t-9"})  # type: ignore[arg-type]

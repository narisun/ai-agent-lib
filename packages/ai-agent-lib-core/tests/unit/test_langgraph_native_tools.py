"""Governed tools keep the native LangGraph tool features working."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, ToolException, tool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode, ToolRuntime, tools_condition
from langgraph.types import Command

from ai_agent_lib_core.adapters import FakeChatModelProvider
from ai_agent_lib_core.contracts import GuardrailPoint, Principal, RequestContext
from ai_agent_lib_core.pipeline import bind_request_context, frame_untrusted
from ai_agent_lib_core.testing import Fakes

CONTEXT = RequestContext(
    principal=Principal(subject="u-1", tenant="t-9"),
    application="accounts-agent",
    request_id="r-1",
    thread_id="th-1",
)
SECRET_IN_STATE = "state-marker-7f3a"


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    note: str


@tool
def count_messages(account: str, state: Annotated[dict[str, Any], InjectedState]) -> str:
    """Say how long the conversation is."""
    return f"{account}: {len(state['messages'])} messages, note={state['note']}"


@tool
async def remember(note: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command[Any]:
    """Keep a note in the graph state."""
    return Command(
        update={"note": note, "messages": [ToolMessage("noted", tool_call_id=tool_call_id)]}
    )


@tool
def remember_sync(note: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command[Any]:
    """Keep a note in the graph state, from a synchronous tool."""
    return Command(
        update={"note": note, "messages": [ToolMessage("noted", tool_call_id=tool_call_id)]}
    )


@tool(response_format="content_and_artifact")
def statement(account: str) -> tuple[str, dict[str, Any]]:
    """Return a statement and its raw rows."""
    return f"statement for {account}", {"rows": [[account, 100]]}


@tool
def who_is_asking(account: str, runtime: ToolRuntime[RequestContext, dict[str, Any]]) -> str:
    """Say which call and which application this is."""
    return f"{account}: call {runtime.tool_call_id} for {runtime.context.application}"


@tool
def fragile(account: str) -> str:
    """Fail in a way the tool itself reports."""
    raise ToolException(f"no ledger for {account}")


fragile.handle_tool_error = True


def _calls(name: str, arguments: dict[str, Any]) -> FakeChatModelProvider:
    return FakeChatModelProvider(
        [
            AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": "call-1"}]),
            "done",
        ]
    )


async def _run(fakes: Fakes, inner: BaseTool) -> dict[str, Any]:
    """Run one tool call through an ordinary graph with a ``ToolNode``."""
    async with fakes.container() as services:
        tools = services.tools([inner])
        model = services.model().bind_tools(tools)

        async def agent(state: State) -> dict[str, list[AnyMessage]]:
            return {"messages": [await model.ainvoke(state["messages"])]}

        builder = StateGraph(State, context_schema=RequestContext)
        builder.add_node("agent", agent)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", tools_condition)
        builder.add_edge("tools", "agent")
        graph = builder.compile(**services.compile_kwargs())
        result: dict[str, Any] = await graph.ainvoke(
            {"messages": [HumanMessage(SECRET_IN_STATE)], "note": "none"},
            **services.invocation(CONTEXT),
        )
        return result


def _tool_message(result: dict[str, Any]) -> ToolMessage:
    (message,) = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    return message


def _tool_record(fakes: Fakes) -> dict[str, Any]:
    (record,) = [r for r in fakes.audit.records if r.event == "tool.call"]
    return dict(record.attributes)


async def test_graph_state_reaches_the_tool_but_is_not_treated_as_tool_input() -> None:
    fakes = Fakes(model=_calls("count_messages", {"account": "4411"}))

    result = await _run(fakes, count_messages)

    assert _tool_message(result).content == frame_untrusted(
        "count_messages", "4411: 2 messages, note=none"
    )
    # Policy and guardrails were shown what the model wrote, not the conversation.
    (checked,) = [text for point, text in fakes.guardrails.checked if point == "tool.input"]
    assert checked == '{"account": "4411"}'
    assert SECRET_IN_STATE not in checked


async def test_the_model_never_sees_or_supplies_an_injected_argument() -> None:
    async with Fakes().container() as services:
        (governed,) = services.tools([count_messages])
    assert set(governed.tool_call_schema.model_json_schema()["properties"]) == {"account"}  # type: ignore[union-attr]


@pytest.mark.parametrize("inner", [remember, remember_sync], ids=["async", "sync"])
async def test_a_tool_can_update_graph_state_with_a_command(inner: BaseTool) -> None:
    fakes = Fakes(model=_calls(inner.name, {"note": "call back on Friday"}))

    result = await _run(fakes, inner)

    assert result["note"] == "call back on Friday"
    assert _tool_message(result).content == "noted"
    assert _tool_message(result).tool_call_id == "call-1"
    # A Command is the tool author's own instruction: passed back, and recorded as not framed.
    assert _tool_record(fakes)["result_framed"] is False


async def test_an_artifact_stays_on_the_tool_message_and_the_content_is_framed() -> None:
    fakes = Fakes(model=_calls("statement", {"account": "4411"}))

    message = _tool_message(await _run(fakes, statement))

    assert message.content == frame_untrusted("statement", "statement for 4411")
    assert message.artifact == {"rows": [["4411", 100]]}
    assert message.tool_call_id == "call-1"
    assert _tool_record(fakes)["result_framed"] is True
    # The result guardrail was shown the content, never the artifact.
    (checked,) = [text for point, text in fakes.guardrails.checked if point == "tool.result"]
    assert checked == "statement for 4411"


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")  # raised by LangGraph itself
async def test_the_tool_runtime_reaches_the_tool() -> None:
    fakes = Fakes(model=_calls("who_is_asking", {"account": "4411"}))

    message = _tool_message(await _run(fakes, who_is_asking))

    assert message.content == frame_untrusted(
        "who_is_asking", "4411: call call-1 for accounts-agent"
    )
    (checked,) = [text for point, text in fakes.guardrails.checked if point == "tool.input"]
    assert checked == '{"account": "4411"}'


async def test_an_error_the_tool_reports_itself_keeps_its_status_and_is_recorded() -> None:
    fakes = Fakes(model=_calls("fragile", {"account": "4411"}))

    message = _tool_message(await _run(fakes, fragile))

    assert message.status == "error"
    assert message.content == frame_untrusted("fragile", "no ledger for 4411")
    assert _tool_record(fakes)["tool_error"] is True


async def test_a_direct_call_with_plain_arguments_returns_plain_content() -> None:
    fakes = Fakes()
    async with fakes.container() as services:
        (governed,) = services.tools([statement])
        with bind_request_context(CONTEXT):
            result = await governed.ainvoke({"account": "4411"})

    # Without a model tool call there is no message to carry an artifact, as in LangChain.
    assert result == frame_untrusted("statement", "statement for 4411")
    assert fakes.guardrails.checked[0] == (GuardrailPoint.TOOL_INPUT, '{"account": "4411"}')

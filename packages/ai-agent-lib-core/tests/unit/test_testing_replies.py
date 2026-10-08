"""Scripted replies are written as a test thinks of them; the helpers spell the names."""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from ai_agent_lib_core import Principal, RequestContext, bind_request_context
from ai_agent_lib_core.testing import (
    FakeChatModelProvider,
    Fakes,
    calls_tool,
    calls_tools,
    structured_reply,
)

CALLER = RequestContext(
    principal=Principal(subject="u-1", tenant="t-1"),
    application="support-agent",
    request_id="r-1",
    thread_id="th-1",
)


class Triage(BaseModel):
    """How to route one customer message."""

    category: Literal["billing", "fraud", "other"]
    urgent: bool


def test_a_tool_call_names_the_tool_as_a_model_sees_it() -> None:
    reply = calls_tool("directory.people_by_team", team="payments")
    assert reply.tool_calls == [
        {
            "name": "directory_people_by_team",
            "args": {"team": "payments"},
            "id": "call-1",
            "type": "tool_call",
        }
    ]
    both = calls_tools(("greet", {"name": "Ada"}), ("greet", {"name": "Bo"}))
    assert [call["id"] for call in both.tool_calls] == ["call-1", "call-2"]


def test_a_tool_argument_may_be_called_tool() -> None:
    assert calls_tool("lookup", tool="hammer").tool_calls[0]["args"] == {"tool": "hammer"}


async def test_a_structured_reply_is_what_with_structured_output_reads() -> None:
    fakes = Fakes(
        model=FakeChatModelProvider(
            [
                structured_reply(Triage(category="fraud", urgent=True)),
                structured_reply(Triage, category="other", urgent=False),
            ]
        )
    )
    async with fakes.container() as services:
        model = services.model().with_structured_output(Triage)
        with bind_request_context(CALLER):
            first = await model.ainvoke([HumanMessage("My card was used abroad.")])
            second = await model.ainvoke([HumanMessage("Hello.")])
    assert first == Triage(category="fraud", urgent=True)
    assert second == Triage(category="other", urgent=False)


def test_a_json_schema_reply_uses_the_schema_title() -> None:
    schema = {"title": "Account answer", "type": "object", "properties": {}}
    assert structured_reply(schema).tool_calls[0]["name"] == "Account_answer"


async def test_a_governed_tool_runs_from_a_scripted_call() -> None:
    def greet(name: str) -> str:
        """Greet a person."""
        return f"Hello, {name}!"

    fakes = Fakes()
    async with fakes.container() as services:
        (tool,) = services.tools([greet])
        call = calls_tool("greet", name="Ada").tool_calls[0]
        with bind_request_context(CALLER):
            message = await tool.ainvoke(call)
    assert "Hello, Ada!" in str(message.content)

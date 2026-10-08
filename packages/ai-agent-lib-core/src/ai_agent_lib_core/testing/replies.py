"""What a scripted model replies, written the way a test thinks about it.

A model that decides to call a tool replies with an ``AIMessage`` whose
``tool_calls`` name the tool as the model saw it. An MCP tool and a structured
output are offered under a name changed for model APIs, so a test that spells
the message by hand must know that ``directory.people_by_team`` is offered as
``directory_people_by_team``. These helpers know it instead::

    replies = [calls_tool("directory.people_by_team", team="payments"), "Two people."]
    replies = [structured_reply(Triage(category="fraud", urgent=True, reason="abroad"))]
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from ai_agent_lib_core.contracts.structured import StructuredOutput, model_safe_name

__all__ = ["calls_tool", "calls_tools", "structured_reply"]


def calls_tool(tool: str, /, **arguments: object) -> AIMessage:
    """Return the reply of a model that calls one tool with ``arguments``.

    Args:
        tool: The tool's name: a function's name, or an MCP tool's registered
            name such as ``directory.people_by_team``.
        **arguments: What the model passes to the tool.
    """
    return calls_tools((tool, arguments))


def calls_tools(*calls: tuple[str, Mapping[str, object]]) -> AIMessage:
    """Return the reply of a model that calls several tools at once, in order."""
    return AIMessage(
        content="",
        tool_calls=[
            {"name": model_safe_name(tool), "args": dict(arguments), "id": f"call-{index}"}
            for index, (tool, arguments) in enumerate(calls, start=1)
        ],
    )


def structured_reply(
    schema: BaseModel | type[BaseModel] | StructuredOutput | Mapping[str, Any],
    /,
    **values: object,
) -> AIMessage:
    """Return the reply of a model that answers through a structured output.

    Pass an instance of the Pydantic model, or the schema and the values::

        structured_reply(Triage(category="fraud", urgent=True, reason="abroad"))
        structured_reply(Triage, category="refund")   # a reply that does not match

    Args:
        schema: The answer itself, as a model instance, or the schema it answers.
        **values: The answer's fields, when ``schema`` is not an instance.
    """
    if isinstance(schema, BaseModel):
        output, answer = StructuredOutput(type(schema)), schema.model_dump(mode="json")
    else:
        output = schema if isinstance(schema, StructuredOutput) else StructuredOutput(schema)
        answer = dict(values)
    return AIMessage(content="", tool_calls=[{"name": output.name, "args": answer, "id": "call-1"}])

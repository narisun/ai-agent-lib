"""The agent graph: ordinary LangGraph over the library's seams.

The graph is the template's: one model node, one tool node, the tools of ``rmqna-data``. The flow
that makes it an analyst (clarify, resolve a name, write SQL, repair from the server's error,
re-examine an empty result, answer with its basis) lives in the system prompt, ``rmqna.prompts``,
not in extra nodes: there is one generation step and a bounded repair loop, as the design asks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, TypedDict

from ai_agent_lib_core import RequestContext, ServiceContainer
from ai_agent_lib_core.integrations.langgraph import static_system_prompt
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from rmqna.prompts import system_prompt

__all__ = [
    "APPLICATION",
    "State",
    "answer",
    "ask",
    "build_graph",
    "registered_mcp_tools",
    "steps",
]

APPLICATION = "rmqna"
"""The agent's name in the registry, in policy and in the audit log."""


class State(TypedDict):
    """The graph state: the conversation so far."""

    messages: Annotated[list[AnyMessage], add_messages]


async def registered_mcp_tools(services: ServiceContainer) -> list[BaseTool]:
    """Return the tools of every MCP server the registry lists for this agent."""
    entry = services.registry.agents.get(APPLICATION)
    tools: list[BaseTool] = []
    for server in entry.mcp_servers if entry is not None else ():
        tools += await services.mcp_tools(server)  # touchpoint 2
    return tools


def build_graph(
    services: ServiceContainer, mcp_tools: Sequence[BaseTool] = ()
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Build the agent graph from governed parts.

    Args:
        services: A started service container.
        mcp_tools: Governed tools of registered MCP servers, already loaded.
    """
    # Touchpoint 1, the agent's own tools, is unused: everything this agent can do is a tool of
    # rmqna-data, loaded by registered_mcp_tools (touchpoint 2) and governed like any other.
    tools = list(mcp_tools)
    model = services.model("default").bind_tools(tools)  # touchpoint 3

    async def agent(state: State) -> dict[str, list[AnyMessage]]:
        # The whole prompt (instructions, schema document, examples) is one static block, the same
        # on every request: the provider can cache it, and nothing in it varies by caller or
        # question. A new message each time, so no request shares a message object with another.
        instructions = static_system_prompt(system_prompt())
        reply = await model.ainvoke([instructions, *state["messages"]])
        return {"messages": [reply]}

    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(**services.compile_kwargs())  # touchpoint 4


async def answer(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    services: ServiceContainer,
    context: RequestContext,
    question: str,
) -> str:
    """Run a built graph for one question on behalf of ``context``."""
    result = await graph.ainvoke(
        {"messages": [HumanMessage(question)]},
        **services.invocation(context),  # touchpoint 5
    )
    return str(result["messages"][-1].text)


async def steps(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    services: ServiceContainer,
    context: RequestContext,
    question: str,
) -> AsyncIterator[dict[str, Any]]:
    """Run a built graph for one question and yield each step as it ends.

    Each step names the graph node that ran and the messages it added, so a
    caller sees tool calls and the answer as they happen.
    """
    async for update in graph.astream(
        {"messages": [HumanMessage(question)]},
        stream_mode="updates",
        **services.invocation(context),
    ):
        for node, change in update.items():
            added = change.get("messages", []) if isinstance(change, dict) else []
            yield {
                "node": node,
                "messages": [
                    {
                        "type": message.type,
                        "content": str(message.content),
                        "tool_calls": [call["name"] for call in getattr(message, "tool_calls", [])],
                    }
                    for message in added
                ],
            }


async def ask(services: ServiceContainer, context: RequestContext, question: str) -> str:
    """Build the graph, with the tools of its MCP servers, and ask one question.

    The command line uses this. A service builds the graph once and calls
    :func:`answer` for every request.
    """
    graph = build_graph(services, await registered_mcp_tools(services))
    return await answer(graph, services, context, question)

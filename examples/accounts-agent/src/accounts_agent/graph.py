"""The agent graph: ordinary LangGraph, with five touchpoints into the library."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from accounts_agent.tools import lookup_balance
from ai_agent_lib_core import RequestContext, ServiceContainer

__all__ = ["APPLICATION", "State", "ask", "build_graph", "registered_mcp_tools"]

APPLICATION = "accounts-agent"

SYSTEM_PROMPT = (
    "You answer questions about accounts and their balances. Use your tools to find "
    "the facts; never guess one. If something is not found, or a tool refuses, say so."
)


class State(TypedDict):
    """The graph state: the conversation so far."""

    messages: Annotated[list[AnyMessage], add_messages]


async def registered_mcp_tools(services: ServiceContainer) -> list[BaseTool]:
    """Return the tools of every MCP server the registry lists for this agent.

    An agent that is not in the agent registry has none, which is how the
    example runs offline with nothing else started.
    """
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
    tools = services.tools([lookup_balance], read_only=["lookup_balance"])  # touchpoint 1
    tools += mcp_tools
    model = services.model("default").bind_tools(tools)  # touchpoint 3

    async def agent(state: State) -> dict[str, list[AnyMessage]]:
        reply = await model.ainvoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])
        return {"messages": [reply]}

    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(**services.compile_kwargs())  # touchpoint 4


async def ask(services: ServiceContainer, context: RequestContext, question: str) -> str:
    """Ask the agent one question on behalf of ``context`` and return its answer.

    This command-line example builds the graph for its one question. A service
    builds it once at startup and reuses it for every request.
    """
    graph = build_graph(services, await registered_mcp_tools(services))
    result = await graph.ainvoke(
        {"messages": [HumanMessage(question)]},
        **services.invocation(context),  # touchpoint 5
    )
    return str(result["messages"][-1].content)

"""One agent service: route each question to the memo or Q&A specialist."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from agentcraft2_core import RequestContext, ServiceContainer
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from rmprep.artifacts import memo_artifacts
from rmprep.context import ContextBuilder
from rmprep.prompts import MEMO_PROMPT, QNA_PROMPT
from rmprep.router import destination, router
from rmprep.specialist import build_specialist
from rmprep.state import State

__all__ = [
    "APPLICATION",
    "State",
    "answer",
    "ask",
    "build_graph",
    "registered_mcp_tools",
    "response",
    "steps",
]

APPLICATION = "rmprep"
"""The agent's name in the registry, in policy and in the audit log."""


async def registered_mcp_tools(services: ServiceContainer) -> list[BaseTool]:
    """Return the tools of every MCP server the registry lists for this agent."""
    entry = services.registry.agents.get(APPLICATION)
    tools: list[BaseTool] = []
    for server in entry.mcp_servers if entry is not None else ():
        tools += await services.mcp_tools(server)
    return tools


def build_graph(
    services: ServiceContainer,
    mcp_tools: Sequence[BaseTool] = (),
    *,
    context_builder: ContextBuilder | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Build the agent graph from governed parts.

    Args:
        services: A started service container.
        mcp_tools: Governed tools of registered MCP servers, already loaded.
        context_builder: Optional policy for assembling bounded model input.
    """
    # Both specialists use the same provider and governed tools, but different prompts.
    model = services.model("default").bind_tools(mcp_tools)
    builder = StateGraph(State, context_schema=RequestContext)
    builder.add_node("router", router)
    builder.add_node(
        "memoagent",
        build_specialist(
            model,
            mcp_tools,
            MEMO_PROMPT,
            default_word_limit=1000,
            now=services.clock.now,
            context_builder=context_builder,
        ),
    )
    builder.add_node(
        "qnaagent",
        build_specialist(
            model, mcp_tools, QNA_PROMPT, now=services.clock.now, context_builder=context_builder
        ),
    )
    builder.add_edge(START, "router")
    builder.add_conditional_edges("router", destination)
    builder.add_edge("memoagent", END)
    builder.add_edge("qnaagent", END)
    # The parent owns the conversation. Both subgraphs receive its saved messages.
    return builder.compile(**services.compile_kwargs())


async def answer(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    services: ServiceContainer,
    context: RequestContext,
    question: str,
) -> str:
    """Run a built graph for one question on behalf of ``context``."""
    return str((await response(graph, services, context, question))["answer"])


async def response(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    services: ServiceContainer,
    context: RequestContext,
    question: str,
) -> dict[str, Any]:
    """Return the accepted answer and any memo produced by this invocation."""
    result = await graph.ainvoke(
        {"messages": [HumanMessage(question)]},
        **services.invocation(context),
    )
    output: dict[str, Any] = {"answer": str(result["messages"][-1].text)}
    if artifacts := memo_artifacts(result, context):
        output["artifacts"] = artifacts
    return output


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
    async for namespace, update in graph.astream(
        {"messages": [HumanMessage(question)]},
        stream_mode="updates",
        subgraphs=True,
        **services.invocation(context),
    ):
        for node, change in update.items():
            # Subgraphs stream individual changes. Their final parent update also
            # contains the old conversation, so do not emit that a second time.
            if not namespace and node in {"memoagent", "qnaagent"}:
                continue
            specialist = namespace[0].split(":", 1)[0] if namespace else None
            added = change.get("messages", []) if isinstance(change, dict) else []
            yield {
                "node": f"{specialist}/{node}" if specialist else node,
                "route": change.get("route") if isinstance(change, dict) else None,
                "messages": [
                    {
                        "type": message.type,
                        "content": str(message.content),
                        "tool_calls": [call["name"] for call in getattr(message, "tool_calls", [])],
                    }
                    for message in added
                ],
            }


async def ask(
    services: ServiceContainer,
    context: RequestContext,
    question: str,
    *,
    context_builder: ContextBuilder | None = None,
) -> str:
    """Build the graph, with the tools of its MCP servers, and ask one question.

    The command line uses this. A service builds the graph once and calls
    :func:`answer` for every request.
    """
    graph = build_graph(
        services, await registered_mcp_tools(services), context_builder=context_builder
    )
    return await answer(graph, services, context, question)

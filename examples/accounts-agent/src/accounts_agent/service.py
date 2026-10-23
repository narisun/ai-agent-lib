"""HTTP entry point: serve the agent until the platform stops it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from accounts_agent.graph import APPLICATION, build_graph, registered_mcp_tools
from agentcraft2_core import RequestContext, ServiceContainer
from agentcraft2_core.integrations.http import (
    AgentService,
    ServiceLifecycle,
    build_agent_app,
    run_agent,
)
from agentcraft2_core.integrations.langgraph import AgentTracing

__all__ = ["SERVICE", "build_app", "main"]


async def _build_graph(services: ServiceContainer) -> Any:
    # Admission stays closed until discovery and graph compilation succeed.
    return build_graph(services, await registered_mcp_tools(services))


async def _respond(
    graph: Any, services: ServiceContainer, context: RequestContext, question: str
) -> dict[str, Any]:
    result = await graph.ainvoke(
        {"messages": [HumanMessage(question)]}, **services.invocation(context)
    )
    return {"answer": str(result["messages"][-1].content)}


async def _stream(
    graph: Any, services: ServiceContainer, context: RequestContext, given: Any
) -> AsyncIterator[dict[str, Any]]:
    # POST /invoke/stream sends each graph step as a server-sent event.
    async for update in graph.astream(
        {"messages": [HumanMessage(SERVICE.question(given))]},
        stream_mode="updates",
        **services.invocation(context),
    ):
        for node, change in update.items():
            added = change.get("messages", []) if isinstance(change, dict) else []
            yield {
                "node": node,
                "messages": [
                    {"type": message.type, "content": str(message.content)} for message in added
                ],
            }


SERVICE = AgentService(
    application=APPLICATION,
    build_graph=_build_graph,
    respond=_respond,
    stream=_stream,
    default_port=8000,
)


async def build_app(
    services: ServiceContainer,
    lifecycle: ServiceLifecycle,
    *,
    tracing: AgentTracing | None = None,
) -> Any:
    """Build the agent's HTTP application; kept for callers of the previous shape."""
    return await build_agent_app(SERVICE, services, lifecycle, tracing=tracing)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    return run_agent(SERVICE, argv)


if __name__ == "__main__":
    raise SystemExit(main())

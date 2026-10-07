"""HTTP entry point: serve the agent until the platform stops it."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from accounts_agent.graph import APPLICATION, build_graph, registered_mcp_tools
from ai_agent_lib_core import AgentLibError, RequestContext, ServiceContainer, ValidationFailed
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app, serve

__all__ = ["build_app", "main"]


async def build_app(services: ServiceContainer, lifecycle: ServiceLifecycle) -> Any:
    """Build the agent's HTTP application.

    The graph is built once, here, and reused for every request. Each request
    brings its own caller: the entry point turns the bearer token into a
    request context before the graph runs.

    Args:
        services: A started service container.
        lifecycle: The service's lifecycle.
    """
    graph = build_graph(services, await registered_mcp_tools(services))

    async def run(context: RequestContext, given: Any) -> dict[str, str]:
        question = given.get("question") if isinstance(given, dict) else None
        if not isinstance(question, str) or not question.strip():
            raise ValidationFailed("the input must be an object with a 'question'")
        result = await graph.ainvoke(
            {"messages": [HumanMessage(question)]}, **services.invocation(context)
        )
        return {"answer": str(result["messages"][-1].content)}

    return agent_app(services, run, application=APPLICATION, lifecycle=lifecycle)


async def _serve(host: str, port: int) -> None:
    async with ServiceContainer.from_env() as services:
        # serve() runs services.validate() once it is listening, so the health
        # route answers while the readiness route still says no.
        lifecycle = ServiceLifecycle(services.validate)
        await serve(await build_app(services, lifecycle), lifecycle, host=host, port=port)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    parser = argparse.ArgumentParser(prog="accounts-agent-serve", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    arguments = parser.parse_args(argv)
    try:
        asyncio.run(_serve(arguments.host, arguments.port))
    except AgentLibError as error:
        sys.stderr.write(f"accounts-agent-serve: {type(error).__name__}: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

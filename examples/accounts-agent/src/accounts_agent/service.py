"""HTTP entry point: serve the agent until the platform stops it."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from accounts_agent.graph import APPLICATION, build_graph, registered_mcp_tools
from ai_agent_lib_core import RequestContext, ServiceConfig, ServiceContainer, ValidationFailed
from ai_agent_lib_core.config import load_service_config
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app, serve
from ai_agent_lib_core.observability import (
    configure_logging,
    report_error,
    shows_details,
    start_telemetry,
)

__all__ = ["build_app", "main"]


async def build_app(services: ServiceContainer, lifecycle: ServiceLifecycle) -> Any:
    """Build the agent's HTTP application.

    The graph is prepared once after the listener starts and before readiness.
    Each request brings its own caller: the entry point turns the bearer token into a
    request context before the graph runs.

    Args:
        services: The container used by request handlers. The lifecycle startup
            callback must start and validate it before graph preparation runs.
        lifecycle: Receives deferred preparation before serving starts.
    """
    graph: Any = None

    async def prepare() -> None:
        # Admission stays closed until discovery and graph compilation succeed.
        nonlocal graph
        graph = build_graph(services, await registered_mcp_tools(services))

    lifecycle.prepare(prepare)

    async def run(context: RequestContext, given: Any) -> dict[str, str]:
        result = await graph.ainvoke(
            {"messages": [HumanMessage(_question(given))]}, **services.invocation(context)
        )
        return {"answer": str(result["messages"][-1].content)}

    async def stream(context: RequestContext, given: Any) -> AsyncIterator[dict[str, Any]]:
        # POST /invoke/stream sends each graph step as a server-sent event.
        async for update in graph.astream(
            {"messages": [HumanMessage(_question(given))]},
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

    return agent_app(services, run, application=APPLICATION, lifecycle=lifecycle, stream=stream)


def _question(given: Any) -> str:
    question = given.get("question") if isinstance(given, dict) else None
    if not isinstance(question, str) or not question.strip():
        raise ValidationFailed(
            "the input is not a question",
            expected='an object such as {"question": "What is the balance of 4411?"}',
            actual="no non-empty 'question' in it",
        )
    return question


async def _serve(config: ServiceConfig, host: str, port: int) -> None:
    services = ServiceContainer(config)

    async def start() -> None:
        await services.start()
        await services.validate()

    lifecycle = ServiceLifecycle(start)
    try:
        # The listener is available during adapter startup and graph preparation.
        await serve(await build_app(services, lifecycle), lifecycle, host=host, port=port)
    finally:
        await services.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    parser = argparse.ArgumentParser(prog="accounts-agent-serve", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    arguments = parser.parse_args(argv)
    config: ServiceConfig | None = None
    try:
        config = load_service_config()
        # One line of JSON per event on standard output. On a developer's
        # machine a line about an error also says what the error said.
        configure_logging(APPLICATION, details=shows_details(config))
        # Traces and metrics go to the collector when the settings turn them on.
        stop_telemetry = start_telemetry(APPLICATION, config)
        try:
            asyncio.run(_serve(config, arguments.host, arguments.port))
        finally:
            stop_telemetry()  # the last spans and metrics are flushed
    except Exception as error:  # noqa: BLE001 - every failure is explained, then the exit code
        return report_error("accounts-agent-serve", error, details=shows_details(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

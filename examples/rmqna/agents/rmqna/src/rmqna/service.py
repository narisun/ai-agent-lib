"""HTTP entry point: serve the agent until the platform stops it."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Sequence
from typing import Any

from ai_agent_lib_core import RequestContext, ServiceConfig, ServiceContainer
from ai_agent_lib_core.integrations.http import (
    AgentService,
    OutcomeJournal,
    ServiceLifecycle,
    SqliteOutcomeJournal,
    build_agent_app,
    run_agent,
)
from ai_agent_lib_core.integrations.langgraph import AgentTracing

from rmqna.graph import APPLICATION, answer, build_graph, registered_mcp_tools, steps
from rmqna.settings import ENV_FILE

__all__ = ["SERVICE", "build_app", "main"]


async def _build_graph(services: ServiceContainer) -> Any:
    # Admission stays closed until discovery and graph compilation succeed.
    return build_graph(services, await registered_mcp_tools(services))


async def _respond(
    graph: Any, services: ServiceContainer, context: RequestContext, question: str
) -> dict[str, Any]:
    return {"answer": await answer(graph, services, context, question)}


async def _stream(
    graph: Any, services: ServiceContainer, context: RequestContext, given: Any
) -> AsyncIterator[dict[str, Any]]:
    # POST /invoke/stream sends each step as a server-sent event.
    async for step in steps(graph, services, context, SERVICE.question(given)):
        yield step


def _outcomes(config: ServiceConfig) -> OutcomeJournal | None:
    # Local-only: a deployed service gets no durable journal from this library.
    if config.deployment_env == "local":
        return SqliteOutcomeJournal(ENV_FILE.parent / ".agentlib" / "outcomes.sqlite")
    return None


SERVICE = AgentService(
    application=APPLICATION,
    build_graph=_build_graph,
    respond=_respond,
    stream=_stream,
    default_port=8200,
    env_file=ENV_FILE,
    outcomes=_outcomes,
)


async def build_app(
    services: ServiceContainer,
    lifecycle: ServiceLifecycle,
    *,
    outcomes: OutcomeJournal | None = None,
    tracing: AgentTracing | None = None,
) -> Any:
    """Build the agent's HTTP application; kept for callers of the previous shape.

    ``outcomes`` is ``None`` by default here, as it always was for a caller of
    this shape; ``main`` is the only caller that runs the generated service
    for real, and it resolves the durable journal from ``SERVICE.outcomes``
    against the deployment's resolved configuration.
    """
    service = dataclasses.replace(
        SERVICE, app_kwargs={**dict(SERVICE.app_kwargs), "outcomes": outcomes}
    )
    return await build_agent_app(service, services, lifecycle, tracing=tracing)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    return run_agent(SERVICE, argv)


if __name__ == "__main__":
    raise SystemExit(main())

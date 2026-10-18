"""One entry point for an agent service: the wiring every service repeats.

A service declares what is its own — the application name, how its graph is
built, how it responds — and ``run_agent`` owns the rest: configuration,
logging, telemetry, optional agent tracing (Phoenix), container lifecycle, serving
and teardown. ``agent_app`` and ``serve`` remain available for services that
compose by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_agent_lib_core.config import load_service_config
from ai_agent_lib_core.contracts import (
    RequestContext,
    ServiceConfig,
    ValidationFailed,
    kind_of,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.http.agent import agent_app
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle
from ai_agent_lib_core.integrations.http.outcomes import OutcomeJournal
from ai_agent_lib_core.integrations.http.serving import serve
from ai_agent_lib_core.observability import (
    configure_logging,
    report_error,
    shows_details,
    start_telemetry,
)

if TYPE_CHECKING:
    # Type-only: a runtime import here would pull langgraph into the HTTP module
    # graph, which the import contracts forbid outside integrations.langgraph.
    from ai_agent_lib_core.integrations.langgraph import AgentTracing

__all__ = ["AgentService", "build_agent_app", "read_question", "run_agent"]

Respond = Callable[[Any, ServiceContainer, RequestContext, str], Awaitable[dict[str, Any]]]
"""Answers one question with the compiled graph.

It is given the graph, the service container, the caller's request context and
the question, and returns the response body, which must be something JSON can
hold. This is where a service decides how its graph is invoked.
"""

StreamSteps = Callable[
    [Any, ServiceContainer, RequestContext, object], AsyncIterator[dict[str, Any]]
]
"""Answers one question with the compiled graph and yields each step as it happens.

It is given the graph, the service container, the caller's request context and
the raw request input (not yet read as a question), and yields one JSON-ready
item per step, such as the update of one graph node.
"""


def read_question(given: object) -> str:
    """The question in a request body: a non-empty ``question`` string.

    Raises:
        ValidationFailed: For anything else, naming what was given by kind only.
    """
    question = given.get("question") if isinstance(given, Mapping) else None
    if not isinstance(question, str) or not question.strip():
        raise ValidationFailed(
            "the input is not a question",
            expected='an object such as {"question": "What is the balance of 4411?"}',
            actual=f"no non-empty 'question'; the input is {kind_of(given)}",
        )
    return question


@dataclass(frozen=True, slots=True)
class AgentService:
    """What one agent service is, separated from how every service runs.

    Attributes:
        application: The agent's name: token audience, policy and audit name.
        build_graph: Builds the compiled graph once, after the container is
            validated and before readiness; MCP discovery belongs inside it.
        respond: Answers one question with the graph.
        question: Reads the question from the request input.
        stream: With one, ``POST /invoke/stream`` sends each step. It receives
            the graph, the container, the context and the raw input.
        live_updates: Whether ``POST /invoke`` negotiates model drafts.
        default_port: The port ``--port`` defaults to.
        app_kwargs: Extra keyword arguments for ``agent_app``, such as
            ``inspector``, ``diagnostics`` or ``admission``.
        app_extras: Builds more keyword arguments for ``agent_app`` from what
            only exists once the service is composed: the live service
            container and a zero-argument getter for the prepared graph (the
            same object ``respond`` receives). ``build_agent_app`` calls it
            once, while building the app, and merges the mapping it returns
            over ``app_kwargs``, so on a shared key the extras win (that
            includes ``outcomes``: extras also override a journal supplied by
            the ``outcomes`` factory below). The getter returns the graph only
            once readiness has passed, which every invoke request implies; the
            inspection routes may see None. Call it per request, never while
            composing.
        env_file: The service's own ``.env`` file. ``run_agent`` reads both
            the service configuration and the Phoenix tracing settings from
            it, with the process environment taking precedence. Without one,
            both read the process environment and a ``.env`` in the working
            directory.
        outcomes: Builds the service's outcome journal from the resolved
            configuration; return None for no journal. Called once per
            serve, so the choice can depend on the deployment environment.
    """

    application: str
    build_graph: Callable[[ServiceContainer], Awaitable[Any]]
    respond: Respond
    question: Callable[[object], str] = read_question
    stream: StreamSteps | None = None
    live_updates: bool = True
    default_port: int = 8000
    app_kwargs: Mapping[str, Any] = field(default_factory=dict)
    app_extras: Callable[[ServiceContainer, Callable[[], Any]], Mapping[str, Any]] | None = None
    env_file: Path | None = None
    outcomes: Callable[[ServiceConfig], OutcomeJournal | None] | None = None


async def build_agent_app(
    service: AgentService,
    services: ServiceContainer,
    lifecycle: ServiceLifecycle,
    *,
    tracing: AgentTracing | None = None,
) -> Any:
    """Build the agent's HTTP application; the graph is prepared before readiness.

    Args:
        service: What the agent is: its name, graph builder and responders.
        services: The service container the graph and its governed calls use.
            It is also what ``service.app_extras`` receives.
        lifecycle: Readiness bookkeeping; the graph is built by a hook that
            runs after the container is validated and before the service
            reports ready.
        tracing: The running tracing adapter, or None for no agent tracing.
            When given, each ``/invoke`` request runs under it. The caller
            owns the adapter and closes it (``agent_tracing`` does so when its
            context exits); this function never does.

    Returns:
        The ASGI application. ``service.app_extras``, when set, has been called
        with ``services`` and a getter for the prepared graph; the getter
        returns the graph once ``lifecycle`` has passed readiness, which every
        invoke request implies (the inspection routes may see None). What it
        returns overrides ``service.app_kwargs``, including a journal supplied
        by ``service.outcomes``.
    """
    graph: Any = None

    async def prepare() -> None:
        nonlocal graph
        graph = await service.build_graph(services)

    lifecycle.prepare(prepare)

    async def run(context: RequestContext, given: Any) -> dict[str, Any]:
        question = service.question(given)
        if tracing is not None:
            return await tracing.run(graph, services, context, question, respond=service.respond)
        return await service.respond(graph, services, context, question)

    steps: Callable[[RequestContext, Any], AsyncIterator[dict[str, Any]]] | None = None
    if service.stream is not None:
        source = service.stream

        def bound(context: RequestContext, given: Any) -> AsyncIterator[dict[str, Any]]:
            return source(graph, services, context, given)

        steps = bound

    def prepared_graph() -> Any:
        return graph

    extras = dict(service.app_extras(services, prepared_graph)) if service.app_extras else {}

    return agent_app(
        services,
        run,
        application=service.application,
        lifecycle=lifecycle,
        stream=steps,
        live_updates=service.live_updates,
        **{**dict(service.app_kwargs), **extras},
    )


def _with_outcomes(service: AgentService, config: ServiceConfig) -> AgentService:
    """Resolve ``service.outcomes`` against ``config`` and merge it into ``app_kwargs``.

    Without a factory, ``service`` is returned unchanged. A factory that
    returns ``None`` (for example, because the deployment isn't local) also
    leaves ``app_kwargs`` unchanged; only a returned journal is merged in.
    """
    if service.outcomes is None:
        return service
    journal = service.outcomes(config)
    if journal is None:
        return service
    return dataclasses.replace(
        service, app_kwargs={**dict(service.app_kwargs), "outcomes": journal}
    )


async def _serve(service: AgentService, config: ServiceConfig, host: str, port: int) -> None:
    from ai_agent_lib_core.integrations.langgraph.tracing import agent_tracing

    service = _with_outcomes(service, config)
    dotenv_path = service.env_file if service.env_file is not None else Path(".env")
    async with agent_tracing(service.application, config, dotenv_path=dotenv_path) as tracing:
        services = ServiceContainer(config, model_trace=tracing)

        async def start() -> None:
            await services.start()
            await services.validate()

        lifecycle = ServiceLifecycle(start)
        try:
            app = await build_agent_app(service, services, lifecycle, tracing=tracing)
            await serve(app, lifecycle, host=host, port=port)
        finally:
            await services.aclose()


def run_agent(service: AgentService, argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets requests in flight finish first."""
    parser = argparse.ArgumentParser(
        prog=f"{service.application}-serve", description=run_agent.__doc__
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=service.default_port, help="port")
    arguments = parser.parse_args(argv)
    config: ServiceConfig | None = None
    try:
        config = (
            load_service_config(service.env_file)
            if service.env_file is not None
            else load_service_config()
        )
        configure_logging(service.application, details=shows_details(config))
        stop_telemetry = start_telemetry(service.application, config)
        try:
            asyncio.run(_serve(service, config, arguments.host, arguments.port))
        finally:
            stop_telemetry()
    except Exception as error:  # noqa: BLE001 - every failure is explained, then the exit code
        return report_error(f"{service.application}-serve", error, details=shows_details(config))
    return 0

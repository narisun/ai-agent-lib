"""The HTTP application of an agent: its routes composed into one Starlette app.

``agent_app`` is the library's own contract. ``agentcore_app`` in
:mod:`.agentcore` adds a hosting platform's aliases to the same routes; both
are built by :func:`compose_agent`, so the two never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentcraft2_core.contracts import RequestContext
from agentcraft2_core.contracts.inspection import TurnInspector
from agentcraft2_core.integrations.http.admission import InMemoryThreadAdmission, ThreadAdmission
from agentcraft2_core.integrations.http.health import health_routes
from agentcraft2_core.integrations.http.invoke import INVOKE_PATH, InvokeRoute, Run
from agentcraft2_core.integrations.http.lifecycle import ServiceLifecycle
from agentcraft2_core.integrations.http.outcomes import OutcomeJournal
from agentcraft2_core.integrations.http.prelude import (
    REQUEST_ID_HEADER,
    AgentServices,
    RequestPrelude,
)
from agentcraft2_core.integrations.http.stream import STREAM_PATH, Stream, StreamRoute
from agentcraft2_core.observability.diagnostics import Diagnostics

__all__ = ["AgentComposition", "agent_app", "compose_agent"]


@dataclass(frozen=True, slots=True)
class AgentComposition:
    """The parts of an agent's HTTP application before they become one app.

    Attributes:
        routes: The agent's own routes: invoke, and what the options added.
        health: The liveness and readiness routes.
        prelude: What every route does before it runs anything.
        invoke: The JSON route, whose handler an alias may serve again.
        admission: The thread guard the routes share.
    """

    routes: list[Route]
    health: list[Route]
    prelude: RequestPrelude
    invoke: InvokeRoute
    admission: ThreadAdmission

    def app(self, *more: Route, middleware: list[Any] | None = None) -> Starlette:
        """Return the Starlette application: the routes, ``more`` aliases, then health."""
        return Starlette(routes=[*self.routes, *more, *self.health], middleware=middleware)


def compose_agent(
    services: AgentServices,
    run: Run,
    *,
    application: str,
    lifecycle: ServiceLifecycle,
    max_body_bytes: int = 1_000_000,
    stream: Stream | None = None,
    diagnostics: Diagnostics | None = None,
    live_updates: bool = False,
    outcomes: OutcomeJournal | None = None,
    inspector: TurnInspector | None = None,
    admission: ThreadAdmission | None = None,
    session_header: str | None = None,
    request_id_headers: tuple[str, ...] = (REQUEST_ID_HEADER,),
) -> AgentComposition:
    """Build the routes of an agent without yet making them an application.

    The arguments are those of :func:`agent_app`, plus the two a hosting
    platform's contract adds to the prelude: ``session_header``, the header
    that names the session the platform routes to this process, and
    ``request_id_headers``, where the caller's request id may be sent.
    """
    if diagnostics is not None:
        run = _observed_run(run, diagnostics)
    admission = admission if admission is not None else InMemoryThreadAdmission()
    prelude = RequestPrelude(
        services,
        application=application,
        lifecycle=lifecycle,
        max_body_bytes=max_body_bytes,
        session_header=session_header,
        request_id_headers=request_id_headers,
    )
    invoke = InvokeRoute(
        prelude,
        run,
        application=application,
        admission=admission,
        live_updates=live_updates,
        outcomes=outcomes,
        inspector=inspector,
    )
    routes = [Route(INVOKE_PATH, invoke.handle, methods=["POST"])]
    if outcomes is not None:
        routes.append(Route("/requests/{request_id}", invoke.status, methods=["GET"]))
    if inspector is not None:
        routes.append(Route("/requests/{request_id}/inspection", invoke.inspect, methods=["GET"]))
    if stream is not None:
        streaming = StreamRoute(
            prelude, stream, application=application, admission=admission, outcomes=outcomes
        )
        routes.append(Route(STREAM_PATH, streaming.handle, methods=["POST"]))
    return AgentComposition(routes, health_routes(lifecycle), prelude, invoke, admission)


def agent_app(
    services: AgentServices,
    run: Run,
    *,
    application: str,
    lifecycle: ServiceLifecycle,
    max_body_bytes: int = 1_000_000,
    stream: Stream | None = None,
    diagnostics: Diagnostics | None = None,
    live_updates: bool = False,
    outcomes: OutcomeJournal | None = None,
    inspector: TurnInspector | None = None,
    admission: ThreadAdmission | None = None,
) -> Starlette:
    """Build the HTTP application of an agent.

    ``POST /invoke`` takes ``{"input": ..., "thread_id": "..."}`` and a bearer
    token, and answers ``{"output": ..., "thread_id": ..., "request_id": ...}``.
    A refused token is 401, a policy denial 403, and a failure of the run is
    reported by a code, never by the error's own text. ``GET /healthz`` and
    ``GET /readyz`` need no token.

    Args:
        services: A started service container.
        run: Runs the agent for one request.
        application: The agent's name: its token audience and its name in
            policy, the registry and the audit log.
        lifecycle: The service's lifecycle. Requests are refused once it stops.
        max_body_bytes: The largest request body accepted.
        stream: Runs the agent and yields each step. With it, ``POST
            /invoke/stream`` takes the same body and answers with server-sent
            events, so a chat front end can show progress.
        diagnostics: Optional recorder shared with the container. Adds metadata
            summaries to /invoke without changing its response. Streaming has
            no accepted-answer envelope and is not summarized by this option.
        live_updates: Allow /invoke clients to request text/event-stream. Configured
            model output checks finish before text is delivered; application
            validation can still revise the draft.
            The result event carries the ordinary envelope plus aggregate usage.
            JSON clients and /invoke/stream keep their existing behavior.
        outcomes: Optional durable journal. Adds owner-scoped GET /requests/{id}
            for reconciliation; reading status never re-executes a request.
        inspector: Optional authorized diagnostic reader. Adds read-only
            GET /requests/{id}/inspection?thread_id=... without changing invocation.
        admission: Optional exclusive thread coordinator. Defaults to an in-memory
            guard shared by this app's invocation routes. Overlap returns 409
            thread_busy before execution or request reservation. Multiple instances
            sharing checkpoints must inject a shared coordinator. Stream ownership
            lasts through graph cancellation and cleanup. A journal also deduplicates
            legacy stream request IDs, but that route has no recoverable final answer.
    """
    return compose_agent(
        services,
        run,
        application=application,
        lifecycle=lifecycle,
        max_body_bytes=max_body_bytes,
        stream=stream,
        diagnostics=diagnostics,
        live_updates=live_updates,
        outcomes=outcomes,
        inspector=inspector,
        admission=admission,
    ).app()


def _observed_run(run: Run, diagnostics: Diagnostics) -> Run:
    async def observed(context: RequestContext, given: Any) -> Any:
        async with diagnostics.operation(context):
            output = await run(context, given)
            # An unserializable return is not an accepted HTTP answer. Validate
            # with the same encoder as the envelope before writing completion.
            JSONResponse(output)
            return output

    return observed

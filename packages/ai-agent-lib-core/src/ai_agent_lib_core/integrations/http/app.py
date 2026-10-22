"""The HTTP application of an agent: its routes composed into one Starlette app."""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from ai_agent_lib_core.contracts import RequestContext
from ai_agent_lib_core.contracts.inspection import TurnInspector
from ai_agent_lib_core.integrations.http.admission import InMemoryThreadAdmission, ThreadAdmission
from ai_agent_lib_core.integrations.http.health import health_routes
from ai_agent_lib_core.integrations.http.invoke import INVOKE_PATH, InvokeRoute, Run
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle
from ai_agent_lib_core.integrations.http.outcomes import OutcomeJournal
from ai_agent_lib_core.integrations.http.prelude import AgentServices, RequestPrelude
from ai_agent_lib_core.integrations.http.stream import STREAM_PATH, Stream, StreamRoute
from ai_agent_lib_core.observability.diagnostics import Diagnostics

__all__ = ["agent_app"]


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
    if diagnostics is not None:
        run = _observed_run(run, diagnostics)
    admission = admission if admission is not None else InMemoryThreadAdmission()
    prelude = RequestPrelude(
        services, application=application, lifecycle=lifecycle, max_body_bytes=max_body_bytes
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
    return Starlette(routes=[*routes, *health_routes(lifecycle)])


def _observed_run(run: Run, diagnostics: Diagnostics) -> Run:
    async def observed(context: RequestContext, given: Any) -> Any:
        async with diagnostics.operation(context):
            output = await run(context, given)
            # An unserializable return is not an accepted HTTP answer. Validate
            # with the same encoder as the envelope before writing completion.
            JSONResponse(output)
            return output

    return observed

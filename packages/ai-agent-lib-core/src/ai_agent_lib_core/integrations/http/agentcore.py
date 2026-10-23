"""The HTTP contract of Amazon Bedrock AgentCore Runtime, served beside the library's own.

The runtime invokes an agent with ``POST /invocations`` and asks after it with
``GET /ping``; it names the session it routes to this process in a header, and
answers ``HealthyBusy`` keep that session alive while work is in progress.
:func:`agentcore_app` adds those aliases to the application :func:`agent_app`
builds, so one image serves both contracts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opentelemetry import baggage, context
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_agent_lib_core.contracts.inspection import TurnInspector
from ai_agent_lib_core.integrations.http.admission import SupportsActivity, ThreadAdmission
from ai_agent_lib_core.integrations.http.app import compose_agent
from ai_agent_lib_core.integrations.http.invoke import Run
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle
from ai_agent_lib_core.integrations.http.outcomes import OutcomeJournal
from ai_agent_lib_core.integrations.http.prelude import REQUEST_ID_HEADER, AgentServices
from ai_agent_lib_core.integrations.http.stream import Stream
from ai_agent_lib_core.observability.diagnostics import Diagnostics

__all__ = [
    "INVOCATIONS_PATH",
    "PING_PATH",
    "REQUEST_ID_HEADER_AGENTCORE",
    "SESSION_BAGGAGE_KEY",
    "SESSION_HEADER",
    "SessionBaggageMiddleware",
    "agentcore_app",
]

INVOCATIONS_PATH = "/invocations"
"""Where the runtime posts an invocation; served by the same handler as ``/invoke``."""
PING_PATH = "/ping"
"""Where the runtime asks whether the agent is healthy, and whether it is busy."""
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
"""The header that names the runtime session, which is the conversation's thread."""
REQUEST_ID_HEADER_AGENTCORE = "x-amzn-bedrock-agentcore-runtime-request-id"
"""The header that carries the runtime's id for one invocation."""
SESSION_BAGGAGE_KEY = "session.id"
"""The OpenTelemetry baggage entry the runtime's observability reads the session from."""

_HEALTHY = "Healthy"
_BUSY = "HealthyBusy"


class SessionBaggageMiddleware:
    """Put the runtime's session id into OpenTelemetry baggage for the request's duration.

    The runtime's observability correlates spans by ``session.id``; with the
    entry in baggage, every span the request opens can carry it. A request
    without the header passes through untouched.
    """

    def __init__(self, app: ASGIApp, *, header: str = SESSION_HEADER) -> None:
        self._app = app
        self._header = header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request inside a context that carries the session id."""
        session = _header(scope, self._header) if scope["type"] == "http" else None
        if session is None:
            await self._app(scope, receive, send)
            return
        token = context.attach(baggage.set_baggage(SESSION_BAGGAGE_KEY, session))
        try:
            await self._app(scope, receive, send)
        finally:
            context.detach(token)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            text = value.decode("latin-1")
            return text if text else None
    return None


def _ping(
    lifecycle: ServiceLifecycle, admission: ThreadAdmission
) -> Callable[[Request], Awaitable[Response]]:
    async def ping(request: Request) -> Response:  # noqa: ARG001 - a route takes the request
        if not lifecycle.ready:
            return JSONResponse({"status": lifecycle.state.value}, status_code=503)
        busy = isinstance(admission, SupportsActivity) and admission.active_threads() > 0
        return JSONResponse({"status": _BUSY if busy else _HEALTHY})

    return ping


def agentcore_app(
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
    """Build the HTTP application of an agent for AgentCore Runtime.

    It is :func:`agent_app` with the runtime's contract added, and takes the
    same arguments. ``POST /invocations`` is served by the ``/invoke`` handler,
    so the same body, the same bearer, the same replies and live server-sent
    events on ``Accept: text/event-stream`` apply there. ``GET /ping`` answers
    503 with the lifecycle state until the service is ready, then
    ``{"status": "Healthy"}``, or ``{"status": "HealthyBusy"}`` while the
    thread guard holds a thread, which keeps the session alive during a long
    turn. It never sets ``time_of_last_update``.

    The runtime's session header, ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``,
    is the conversation's thread when the body names none, and a body that
    names a different thread is refused; the runtime's request id header is
    read when ``x-request-id`` is absent. The session id is put into
    OpenTelemetry baggage as ``session.id`` for the request's duration.

    Every route of :func:`agent_app` stays, so one image serves both contracts.
    """
    composed = compose_agent(
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
        session_header=SESSION_HEADER,
        request_id_headers=(REQUEST_ID_HEADER, REQUEST_ID_HEADER_AGENTCORE),
    )
    return composed.app(
        Route(INVOCATIONS_PATH, composed.invoke.handle, methods=["POST"]),
        Route(PING_PATH, _ping(lifecycle, composed.admission), methods=["GET"]),
        middleware=[Middleware(SessionBaggageMiddleware)],
    )

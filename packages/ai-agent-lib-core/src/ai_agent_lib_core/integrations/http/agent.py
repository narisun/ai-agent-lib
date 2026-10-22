"""An HTTP entry point for an agent, and health routes for any service.

The entry point is deliberately small. It turns one HTTP request into one
governed run: it reads the bearer token, has the identity provider decide who
the caller is, and hands the caller's context and the request's input to a
function the agent's author wrote. That function runs the graph exactly as it
would anywhere else.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from anyio import CancelScope
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ai_agent_lib_core.contracts import (
    AgentLibError,
    BudgetExceeded,
    ExecutionPaused,
    IdGenerator,
    PolicyDenied,
    RequestContext,
    TransientError,
    ValidationFailed,
)
from ai_agent_lib_core.contracts.inspection import TurnInspector
from ai_agent_lib_core.integrations.http.admission import InMemoryThreadAdmission, ThreadAdmission
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle
from ai_agent_lib_core.integrations.http.live import live_response
from ai_agent_lib_core.integrations.http.outcomes import OutcomeJournal
from ai_agent_lib_core.integrations.http.streaming import ClosingStreamingResponse
from ai_agent_lib_core.integrations.spans import content_free_span
from ai_agent_lib_core.observability.diagnostics import Diagnostics
from ai_agent_lib_core.observability.live import live_run
from ai_agent_lib_core.observability.trace_links import capture_trace
from ai_agent_lib_core.pipeline.context import bind_request_context

__all__ = [
    "HEALTH_PATH",
    "INVOKE_PATH",
    "READY_PATH",
    "STREAM_PATH",
    "AgentServices",
    "Run",
    "Stream",
    "SupportsCustomRoutes",
    "add_health_routes",
    "agent_app",
    "health_routes",
]

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"
INVOKE_PATH = "/invoke"
STREAM_PATH = "/invoke/stream"

REQUEST_ID_HEADER = "x-request-id"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BEARER = re.compile(r"^[Bb]earer +(?P<token>[^\s]+)$")
_RETRY_AFTER_SECONDS = "1"
_SERVER_ERROR = 500
_LOG = logging.getLogger(__name__)
_INSTRUMENTATION = "ai_agent_lib_core"

Run = Callable[[RequestContext, Any], Awaitable[Any]]
"""Runs the agent once.

It is given the caller's request context and the ``input`` of the request, and
returns the output, which must be something JSON can hold.
"""


Stream = Callable[[RequestContext, Any], AsyncIterator[Any]]
"""Runs the agent once and yields what it has done so far, step by step.

Each item must be something JSON can hold, such as the update of one graph
node. Every model and tool call inside is governed as in :data:`Run`.
"""


class AgentServices(Protocol):
    """What the entry point needs from the service container."""

    @property
    def ids(self) -> IdGenerator:
        """The source of new identifiers."""
        ...

    async def authenticate(
        self,
        credential: str | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:
        """Verify a caller's credential and return the context for their request."""
        ...


class SupportsCustomRoutes(Protocol):
    """A server that takes extra HTTP routes, as the MCP SDK's server does."""

    def custom_route(self, path: str, methods: list[str]) -> Callable[[Any], Any]:
        """Return a decorator that adds a route outside the server's own protocol."""
        ...


class _TooLargeError(Exception):
    """The request body is larger than the entry point accepts."""


def _error(status: int, error: str, *, request_id: str | None = None, **more: str) -> JSONResponse:
    body: dict[str, str] = {"error": error, **more}
    headers: dict[str, str] = {}
    if request_id is not None:
        body["request_id"] = request_id
        headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(body, status_code=status, headers=headers)


def _unavailable() -> JSONResponse:
    response = _error(503, "unavailable")
    response.headers["retry-after"] = _RETRY_AFTER_SECONDS
    return response


async def _health(request: Request) -> Response:  # noqa: ARG001 - a route takes the request
    """Answer that the process is up. Says nothing about its dependencies."""
    return JSONResponse({"status": "ok"})


def _readiness(lifecycle: ServiceLifecycle) -> Callable[[Request], Awaitable[Response]]:
    async def ready(request: Request) -> Response:  # noqa: ARG001 - a route takes the request
        state = lifecycle.state.value
        return JSONResponse({"status": state}, status_code=200 if lifecycle.ready else 503)

    return ready


def health_routes(lifecycle: ServiceLifecycle) -> list[Route]:
    """Return the liveness and readiness routes of a service.

    Neither asks for a token: a load balancer has none. Neither says anything
    about the service beyond one word.
    """
    return [
        Route(HEALTH_PATH, _health, methods=["GET"]),
        Route(READY_PATH, _readiness(lifecycle), methods=["GET"]),
    ]


def add_health_routes(server: SupportsCustomRoutes, lifecycle: ServiceLifecycle) -> None:
    """Add the liveness and readiness routes to an MCP server.

    The MCP SDK serves these routes without the token check that guards the
    server's own endpoint.
    """
    server.custom_route(HEALTH_PATH, methods=["GET"])(_health)
    server.custom_route(READY_PATH, methods=["GET"])(_readiness(lifecycle))


async def _read_json(request: Request, limit: int) -> object:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _TooLargeError
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _TooLargeError
    return json.loads(body)


def _status_of(error: AgentLibError) -> tuple[int, str]:
    if isinstance(error, PolicyDenied):
        return 403, "denied"
    if isinstance(error, ValidationFailed):
        return 400, "invalid"
    if isinstance(error, BudgetExceeded):
        return 429, "budget_exceeded"
    if isinstance(error, ExecutionPaused):
        return 409, "paused"
    if isinstance(error, TransientError):
        return 503, "unavailable"
    return 500, "internal"


def _level_of(error: AgentLibError) -> int:
    """Refusals are the caller's problem; anything else is the service's."""
    if isinstance(error, PolicyDenied | ValidationFailed | BudgetExceeded | ExecutionPaused):
        return logging.WARNING
    return logging.ERROR


def _failure(error: AgentLibError, request_id: str) -> JSONResponse:
    """Describe a failed run to the caller: a code, never the error's own text."""
    status, name = _status_of(error)
    more = {"reason": error.reason_code} if isinstance(error, PolicyDenied) else {}
    response = _error(status, name, request_id=request_id, **more)
    if status == 503:
        response.headers["retry-after"] = _RETRY_AFTER_SECONDS
    return response


@dataclass(frozen=True, slots=True)
class _Asked:
    """What a request asks for: the input of the run, and the conversation it belongs to."""

    given: object
    thread_id: str


class _ThreadResponse(Response):
    """Hold admission in the ASGI task until execution and stream cleanup finish."""

    def __init__(
        self,
        admission: ThreadAdmission,
        context: RequestContext,
        respond: Callable[[], Awaitable[Response]],
    ) -> None:
        super().__init__()
        self.admission, self.context, self.respond = admission, context, respond
        self.observe: Callable[[Response], None] = lambda _response: None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with self.admission.hold(self.context) as acquired:
            response = (
                await self.respond()
                if acquired
                else _error(409, "thread_busy", request_id=self.context.request_id)
            )
            self.observe(response)
            await response(scope, receive, send)


class _Invoke:
    """The route that runs the agent for one request."""

    def __init__(
        self,
        services: AgentServices,
        run: Run,
        application: str,
        lifecycle: ServiceLifecycle,
        max_body_bytes: int,
        route: str = INVOKE_PATH,
        live_updates: bool = False,
        outcomes: OutcomeJournal | None = None,
        inspector: TurnInspector | None = None,
        admission: ThreadAdmission | None = None,
    ) -> None:
        self._services = services
        self._route = route
        self._run = run
        self._application = application
        self._lifecycle = lifecycle
        self._max_body_bytes = max_body_bytes
        self._live_updates = live_updates
        self._outcomes = outcomes
        self._inspector = inspector
        self._admission = admission if admission is not None else InMemoryThreadAdmission()

    async def handle(self, request: Request) -> Response:
        """Run the agent for one request, describe the outcome and log one line about it."""
        started = time.perf_counter()
        response = await self._respond(request)
        if isinstance(response, _ThreadResponse):
            response.observe = lambda reply: self._report(reply, started)
        else:
            self._report(response, started)
        return response

    def _report(self, response: Response, started: float) -> None:
        _LOG.log(
            logging.ERROR if response.status_code >= _SERVER_ERROR else logging.INFO,
            "request",
            extra={
                "route": self._route,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "request_id": response.headers.get(REQUEST_ID_HEADER),
            },
        )

    async def _respond(self, request: Request) -> Response:
        """Read the request, find out who is asking, then run the agent for them."""
        if not self._lifecycle.accepting:
            return _unavailable()
        asked = await self._read(request)
        if isinstance(asked, Response):
            return asked
        context = await self._caller(request, asked.thread_id)
        if isinstance(context, Response):
            return context
        return _ThreadResponse(
            self._admission,
            context,
            lambda: self._admitted_answer(
                context, asked.given, live="text/event-stream" in request.headers.get("accept", "")
            ),
        )

    async def _admitted_answer(
        self, context: RequestContext, given: object, *, live: bool
    ) -> Response:
        """Reserve the request only after acquiring exclusive thread execution."""
        if self._outcomes is not None and not await self._outcomes.begin(context):
            return _error(503, "outcome_unknown", request_id=context.request_id)
        if self._live_updates and live:
            return live_response(context, lambda: self._recorded_answer(context, given))
        return await self._recorded_answer(context, given)

    async def status(self, request: Request) -> Response:
        """Read an authoritative outcome without running the graph or any tools."""
        thread_id = request.query_params.get("thread_id", "")
        request_id = request.path_params["request_id"]
        if not _SAFE_ID.fullmatch(thread_id) or not _SAFE_ID.fullmatch(request_id):
            return _error(400, "invalid")
        context = await self._caller(request, thread_id)
        if isinstance(context, Response):
            return context
        body = await self._outcomes.read(context, request_id) if self._outcomes else None
        return JSONResponse(
            body if body is not None else {"state": "unknown"},
            headers={"cache-control": "no-store"},
        )

    async def _recorded_answer(self, context: RequestContext, given: object) -> Response:
        started = time.perf_counter()
        with capture_trace(context) as trace:
            response = await self._answer(context, given)
        if isinstance(response, StreamingResponse):
            return response
        body = json.loads(bytes(response.body))
        body.update(thread_id=context.thread_id, request_id=context.request_id)
        if "error" in body and trace.link is not None:
            body["trace"] = trace.link
        live = live_run(context)
        if live is not None:
            body.update(usage=live.usage().model_dump(), model_calls=live.model_calls)
        body["elapsed_ms"] = (time.perf_counter() - started) * 1000
        if self._outcomes is not None and (
            response.status_code == 200
            or body.get("error")
            in {
                "invalid",
                "denied",
                "unauthorized",
                "budget_exceeded",
                "paused",
            }
        ):
            await self._outcomes.finish(context, body)
        _LOG.info(
            "invocation finished",
            extra={
                "request_id": context.request_id,
                "status": response.status_code,
                "duration_ms": round(body["elapsed_ms"]),
            },
        )
        if self._outcomes is None and live is None and trace.link is None:
            return response
        headers = {key: value for key, value in response.headers.items() if key != "content-length"}
        return JSONResponse(body, status_code=response.status_code, headers=headers)

    async def inspect(self, request: Request) -> Response:
        """Read diagnostics for the authenticated owner; never run or resume the graph."""
        thread_id = request.query_params.get("thread_id", "")
        request_id = request.path_params["request_id"]
        if not _SAFE_ID.fullmatch(thread_id) or not _SAFE_ID.fullmatch(request_id):
            return _error(400, "invalid")
        context = await self._caller(request, thread_id)
        if isinstance(context, Response):
            return context
        if self._inspector is None:
            return _error(404, "not_found")
        try:
            with bind_request_context(context):
                result = await self._inspector.read(context, request_id)
        except PolicyDenied:
            return _error(404, "not_found")
        except Exception:  # noqa: BLE001 - never return raw diagnostic exceptions
            # The reply says only "unavailable"; the log says what the inspector raised.
            _LOG.exception(
                "the inspection could not be read",
                extra={"request_id": request_id, "route": "inspection"},
            )
            return _error(503, "unavailable")
        return JSONResponse(
            result.model_dump(mode="json", by_alias=True), headers={"cache-control": "no-store"}
        )

    async def _read(self, request: Request) -> _Asked | Response:
        """Return what was asked, or the reply to a request that cannot be read."""
        try:
            payload = await _read_json(request, self._max_body_bytes)
        except _TooLargeError:
            return _error(413, "too_large")
        except (ValueError, ClientDisconnect):
            return _error(400, "invalid", detail="the body must be a JSON object")
        if not isinstance(payload, dict) or "input" not in payload:
            return _error(400, "invalid", detail="the body must be a JSON object with 'input'")
        thread_id = payload["thread_id"] if "thread_id" in payload else self._new_thread()
        if not isinstance(thread_id, str) or not _SAFE_ID.match(thread_id):
            return _error(400, "invalid", detail="'thread_id' is not a usable identifier")
        return _Asked(payload["input"], thread_id)

    async def _caller(self, request: Request, thread_id: str) -> RequestContext | Response:
        """Return the request context of the caller, or the reply to one who is refused."""
        header = request.headers.get("authorization")
        bearer = _BEARER.match(header) if header is not None else None
        if header is not None and bearer is None:
            return self._unauthorized("malformed_authorization")
        sent = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = sent if _SAFE_ID.match(sent) else None
        try:
            return await self._services.authenticate(
                bearer["token"] if bearer is not None else None,
                application=self._application,
                thread_id=thread_id,
                request_id=request_id,
            )
        except PolicyDenied as refusal:
            _LOG.info(
                "the caller was refused at sign-in",
                exc_info=refusal,
                extra={"request_id": request_id, "reason_code": refusal.reason_code},
            )
            return self._unauthorized(refusal.reason_code)
        except AgentLibError as error:
            _LOG.error("the caller could not be signed in", exc_info=error)
            return _failure(error, request_id or self._services.ids.new_id())

    async def _answer(self, context: RequestContext, given: object) -> Response:
        """Run the agent for one caller and describe the outcome."""
        attributes = {
            "agentlib.application": self._application,
            "agentlib.request_id": context.request_id,
        }
        try:
            # Inside the run every log line and every governed call belongs to this request.
            with (
                bind_request_context(context),
                content_free_span(_INSTRUMENTATION, "agent.invoke", attributes=attributes),
            ):
                output = await self._run(context, given)
        except AgentLibError as error:
            # What was expected and what was found are logged: the library's
            # errors never put caller content in those.
            _LOG.log(
                _level_of(error),
                "the run ended with %s",
                type(error).__name__,
                exc_info=error,
                extra={"request_id": context.request_id},
            )
            return _failure(error, context.request_id)
        except Exception:  # noqa: BLE001 - a bug must become a reply and a log line
            # Its type, where it was raised and the line of the agent's own code
            # are logged; its text only where logging is set up with details.
            _LOG.exception("the run failed unexpectedly", extra={"request_id": context.request_id})
            return _error(_SERVER_ERROR, "internal", request_id=context.request_id)
        return JSONResponse(
            {"output": output, "thread_id": context.thread_id, "request_id": context.request_id},
            headers={REQUEST_ID_HEADER: context.request_id},
        )

    def _new_thread(self) -> str:
        return self._services.ids.new_id()

    @staticmethod
    def _unauthorized(reason: str) -> JSONResponse:
        response = _error(401, "unauthorized", reason=reason)
        response.headers["www-authenticate"] = 'Bearer error="invalid_token"'
        return response


class _StreamRoute(_Invoke):
    """The route that runs the agent for one request and sends each step as it happens.

    The reply is a stream of server-sent events: ``update`` for each item the
    agent yields, then ``end``; or ``error`` with a code, never the error's text.
    """

    def __init__(
        self,
        services: AgentServices,
        stream: Stream,
        application: str,
        lifecycle: ServiceLifecycle,
        max_body_bytes: int,
        admission: ThreadAdmission,
        outcomes: OutcomeJournal | None,
    ) -> None:
        super().__init__(
            services,
            _unused_run,
            application,
            lifecycle,
            max_body_bytes,
            route=STREAM_PATH,
            admission=admission,
            outcomes=outcomes,
        )
        self._stream = stream

    async def _answer(self, context: RequestContext, given: object) -> Response:
        return ClosingStreamingResponse(
            self._events(context, given),
            media_type="text/event-stream",
            headers={REQUEST_ID_HEADER: context.request_id, "cache-control": "no-cache"},
        )

    async def _events(self, context: RequestContext, given: object) -> AsyncIterator[str]:
        attributes = {
            "agentlib.application": self._application,
            "agentlib.request_id": context.request_id,
        }
        ending: dict[str, object] = {
            "thread_id": context.thread_id,
            "request_id": context.request_id,
        }
        try:
            with (
                bind_request_context(context),
                content_free_span(_INSTRUMENTATION, "agent.stream", attributes=attributes),
            ):
                iterator = self._stream(context, given)
                try:
                    async for item in iterator:
                        yield _event("update", item)
                finally:
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        with CancelScope(shield=True):
                            await close()
        except AgentLibError as error:
            _LOG.log(
                _level_of(error),
                "the run ended with %s",
                type(error).__name__,
                exc_info=error,
                extra={"request_id": context.request_id},
            )
            _, name = _status_of(error)
            reason = {"reason": error.reason_code} if isinstance(error, PolicyDenied) else {}
            yield _event("error", {"error": name, **reason, **ending})
            return
        except Exception:  # noqa: BLE001 - a bug must become an event and a log line
            _LOG.exception("the run failed unexpectedly", extra={"request_id": context.request_id})
            yield _event("error", {"error": "internal", **ending})
            return
        yield _event("end", ending)


async def _unused_run(context: RequestContext, given: Any) -> Any:  # noqa: ARG001  # pragma: no cover
    raise AssertionError("the streaming route never runs the agent all at once")


def _event(name: str, data: object) -> str:
    """One server-sent event. JSON holds no raw line breaks, so one data line suffices."""
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"


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
    invoke = _Invoke(
        services,
        run,
        application,
        lifecycle,
        max_body_bytes,
        live_updates=live_updates,
        outcomes=outcomes,
        inspector=inspector,
        admission=admission,
    )
    routes = [Route(INVOKE_PATH, invoke.handle, methods=["POST"])]
    if outcomes is not None:
        routes.append(Route("/requests/{request_id}", invoke.status, methods=["GET"]))
    if inspector is not None:
        routes.append(Route("/requests/{request_id}/inspection", invoke.inspect, methods=["GET"]))
    if stream is not None:
        streaming = _StreamRoute(
            services, stream, application, lifecycle, max_body_bytes, admission, outcomes
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

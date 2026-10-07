"""An HTTP entry point for an agent, and health routes for any service.

The entry point is deliberately small. It turns one HTTP request into one
governed run: it reads the bearer token, has the identity provider decide who
the caller is, and hands the caller's context and the request's input to a
function the agent's author wrote. That function runs the graph exactly as it
would anywhere else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

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
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle

__all__ = [
    "HEALTH_PATH",
    "INVOKE_PATH",
    "READY_PATH",
    "AgentServices",
    "Run",
    "SupportsCustomRoutes",
    "add_health_routes",
    "agent_app",
    "health_routes",
]

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"
INVOKE_PATH = "/invoke"

REQUEST_ID_HEADER = "x-request-id"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BEARER = re.compile(r"^[Bb]earer +(?P<token>[^\s]+)$")
_RETRY_AFTER_SECONDS = "1"

Run = Callable[[RequestContext, Any], Awaitable[Any]]
"""Runs the agent once.

It is given the caller's request context and the ``input`` of the request, and
returns the output, which must be something JSON can hold.
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


def _failure(error: AgentLibError, request_id: str) -> JSONResponse:
    """Describe a failed run to the caller: a code, never the error's own text."""
    status, name = _status_of(error)
    more = {"reason": error.reason_code} if isinstance(error, PolicyDenied) else {}
    response = _error(status, name, request_id=request_id, **more)
    if status == 503:
        response.headers["retry-after"] = _RETRY_AFTER_SECONDS
    return response


class _Invoke:
    """The route that runs the agent for one request."""

    def __init__(
        self,
        services: AgentServices,
        run: Run,
        application: str,
        lifecycle: ServiceLifecycle,
        max_body_bytes: int,
    ) -> None:
        self._services = services
        self._run = run
        self._application = application
        self._lifecycle = lifecycle
        self._max_body_bytes = max_body_bytes

    async def handle(self, request: Request) -> Response:
        """Run the agent for one request and describe the outcome."""
        if not self._lifecycle.accepting:
            return _unavailable()
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

        header = request.headers.get("authorization")
        bearer = _BEARER.match(header) if header is not None else None
        if header is not None and bearer is None:
            return self._unauthorized("malformed_authorization")
        asked = request.headers.get(REQUEST_ID_HEADER, "")
        try:
            context = await self._services.authenticate(
                bearer["token"] if bearer is not None else None,
                application=self._application,
                thread_id=thread_id,
                request_id=asked if _SAFE_ID.match(asked) else None,
            )
        except PolicyDenied as refusal:
            return self._unauthorized(refusal.reason_code)
        except AgentLibError as error:
            return _failure(error, asked if _SAFE_ID.match(asked) else self._services.ids.new_id())

        try:
            output = await self._run(context, payload["input"])
        except AgentLibError as error:
            return _failure(error, context.request_id)
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


def agent_app(
    services: AgentServices,
    run: Run,
    *,
    application: str,
    lifecycle: ServiceLifecycle,
    max_body_bytes: int = 1_000_000,
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
    """
    invoke = _Invoke(services, run, application, lifecycle, max_body_bytes)
    return Starlette(
        routes=[Route(INVOKE_PATH, invoke.handle, methods=["POST"]), *health_routes(lifecycle)]
    )

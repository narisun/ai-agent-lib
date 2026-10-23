"""What every route of an agent does before it runs anything.

The request is read and its identifiers are checked, the caller is signed in,
and the conversation's thread is held while the answer is produced. A request
that is refused on the way gets a reply with a code, never with the error's
own text. The JSON route and the streaming route share one prelude by
composition; neither inherits from the other.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from agentcraft2_core.contracts import (
    AgentCraft2Error,
    BudgetExceeded,
    ExecutionPaused,
    IdGenerator,
    PolicyDenied,
    PresentedCredentials,
    RequestContext,
    TransientError,
    ValidationFailed,
)
from agentcraft2_core.integrations.http.admission import ThreadAdmission
from agentcraft2_core.integrations.http.lifecycle import ServiceLifecycle

__all__ = [
    "REQUEST_ID_HEADER",
    "SERVER_ERROR",
    "Admitted",
    "AgentServices",
    "Asked",
    "Lookup",
    "Reply",
    "RequestPrelude",
    "ThreadResponse",
    "answer_admitted",
    "error_reply",
    "error_response",
    "failure",
    "level_of",
    "status_of",
    "unavailable",
]

REQUEST_ID_HEADER = "x-request-id"
SERVER_ERROR = 500
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BEARER = re.compile(r"^[Bb]earer +(?P<token>[^\s]+)$")
_RETRY_AFTER_SECONDS = "1"
_LOG = logging.getLogger(__name__)


class AgentServices(Protocol):
    """What the entry point needs from the service container."""

    @property
    def ids(self) -> IdGenerator:
        """The source of new identifiers."""
        ...

    async def authenticate(
        self,
        credential: str | PresentedCredentials | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:
        """Verify what a caller presented and return the context for their request.

        The entry point presents everything it was sent, as
        :class:`PresentedCredentials`; a bare token is accepted for callers
        that have only that.
        """
        ...


# ------------------------------------------------------------------ replies


@dataclass(frozen=True, slots=True)
class Reply:
    """A JSON reply before it is serialised: its status, its body and its headers.

    A route builds the body once, adds to it what it knows, and serialises it
    once; nothing parses its own output to append a field.
    """

    status: int
    body: dict[str, object]
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> JSONResponse:
        """Serialise the reply."""
        return JSONResponse(self.body, status_code=self.status, headers=self.headers)


def error_reply(status: int, error: str, *, request_id: str | None = None, **more: str) -> Reply:
    """A refusal or a failure: a code and, when one is known, the request's id."""
    body: dict[str, object] = {"error": error, **more}
    headers: dict[str, str] = {}
    if request_id is not None:
        body["request_id"] = request_id
        headers[REQUEST_ID_HEADER] = request_id
    return Reply(status, body, headers)


def error_response(
    status: int, error: str, *, request_id: str | None = None, **more: str
) -> JSONResponse:
    """The serialised form of :func:`error_reply`."""
    return error_reply(status, error, request_id=request_id, **more).response()


def unavailable() -> JSONResponse:
    """The reply of a service that is not accepting requests."""
    response = error_response(503, "unavailable")
    response.headers["retry-after"] = _RETRY_AFTER_SECONDS
    return response


def status_of(error: AgentCraft2Error) -> tuple[int, str]:
    """The HTTP status and the code that describe a failed run to the caller."""
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


def level_of(error: AgentCraft2Error) -> int:
    """Refusals are the caller's problem; anything else is the service's."""
    if isinstance(error, PolicyDenied | ValidationFailed | BudgetExceeded | ExecutionPaused):
        return logging.WARNING
    return logging.ERROR


def failure(error: AgentCraft2Error, request_id: str) -> Reply:
    """Describe a failed run to the caller: a code, never the error's own text."""
    status, name = status_of(error)
    more = {"reason": error.reason_code} if isinstance(error, PolicyDenied) else {}
    reply = error_reply(status, name, request_id=request_id, **more)
    if status == 503:
        reply.headers["retry-after"] = _RETRY_AFTER_SECONDS
    return reply


def _unauthorized(reason: str) -> JSONResponse:
    response = error_response(401, "unauthorized", reason=reason)
    response.headers["www-authenticate"] = 'Bearer error="invalid_token"'
    return response


# ------------------------------------------------------------------ the prelude


class _TooLargeError(Exception):
    """The request body is larger than the entry point accepts."""


@dataclass(frozen=True, slots=True)
class Asked:
    """What a request asks for: the input of the run, and the conversation it belongs to."""

    given: object
    thread_id: str


@dataclass(frozen=True, slots=True)
class Admitted:
    """A request that may run: who asks, what they gave, and whether they take events."""

    context: RequestContext
    given: object
    accepts_events: bool


@dataclass(frozen=True, slots=True)
class Lookup:
    """A read of one earlier request by the caller who owns its thread."""

    context: RequestContext
    request_id: str


class RequestPrelude:
    """Read a request, check its identifiers and sign the caller in.

    Nothing here runs the agent. Every reply it returns is a refusal: the body
    could not be read, an identifier is not usable, the credential is malformed
    or the identity provider said no.

    Args:
        services: The container, or anything that signs callers in like it.
        application: The agent's name.
        lifecycle: Requests are refused once the service stops accepting them.
        max_body_bytes: The largest request body accepted.
        session_header: A header the hosting platform sets to name the
            session, lower-case. When the body names no ``thread_id`` the
            session is the thread; a body that names a different thread is
            refused. ``None`` when no platform does this.
        request_id_headers: The headers that may carry the caller's request
            id, lower-case, in order of preference.
    """

    def __init__(
        self,
        services: AgentServices,
        *,
        application: str,
        lifecycle: ServiceLifecycle,
        max_body_bytes: int,
        session_header: str | None = None,
        request_id_headers: tuple[str, ...] = (REQUEST_ID_HEADER,),
    ) -> None:
        self._services = services
        self._application = application
        self._lifecycle = lifecycle
        self._max_body_bytes = max_body_bytes
        self._session_header = session_header
        self._request_id_headers = request_id_headers

    async def admit(self, request: Request) -> Admitted | Response:
        """Return what may run, or the reply to a request refused before anything runs."""
        if not self._lifecycle.accepting:
            return unavailable()
        asked = await self.read(request)
        if isinstance(asked, Response):
            return asked
        context = await self.caller(request, asked.thread_id)
        if isinstance(context, Response):
            return context
        accepts_events = "text/event-stream" in request.headers.get("accept", "")
        return Admitted(context, asked.given, accepts_events)

    async def lookup(self, request: Request) -> Lookup | Response:
        """Return the owner's read of one request, or the reply to a malformed or refused one.

        The thread comes from the query string and the request's id from the
        path; both must be usable identifiers before anyone is asked.
        """
        thread_id = request.query_params.get("thread_id", "")
        request_id: str = request.path_params["request_id"]
        if not _SAFE_ID.fullmatch(thread_id) or not _SAFE_ID.fullmatch(request_id):
            return error_response(400, "invalid")
        context = await self.caller(request, thread_id)
        if isinstance(context, Response):
            return context
        return Lookup(context, request_id)

    async def read(self, request: Request) -> Asked | Response:
        """Return what was asked, or the reply to a request that cannot be read."""
        try:
            payload = await _read_json(request, self._max_body_bytes)
        except _TooLargeError:
            return error_response(413, "too_large")
        except (ValueError, ClientDisconnect):
            return error_response(400, "invalid", detail="the body must be a JSON object")
        if not isinstance(payload, dict) or "input" not in payload:
            return error_response(
                400, "invalid", detail="the body must be a JSON object with 'input'"
            )
        session = self._session(request)
        if isinstance(session, Response):
            return session
        if "thread_id" in payload:
            thread_id = payload["thread_id"]
        elif session is not None:
            thread_id = session
        else:
            thread_id = self._services.ids.new_id()
        if not isinstance(thread_id, str) or not _SAFE_ID.fullmatch(thread_id):
            return error_response(400, "invalid", detail="'thread_id' is not a usable identifier")
        if session is not None and thread_id != session:
            return error_response(
                400, "invalid", detail="'thread_id' disagrees with the runtime session"
            )
        return Asked(payload["input"], thread_id)

    def _session(self, request: Request) -> str | Response | None:
        """The platform's session id, if the header is set; the refusal if it is not usable."""
        if self._session_header is None:
            return None
        session = request.headers.get(self._session_header)
        if session is None:
            return None
        if not _SAFE_ID.fullmatch(session):
            return error_response(
                400, "invalid", detail="the session header is not a usable identifier"
            )
        return session

    def _request_id(self, request: Request) -> str | None:
        """The caller's request id, from the first accepted header that carries a usable one."""
        for name in self._request_id_headers:
            sent = request.headers.get(name, "")
            if _SAFE_ID.fullmatch(sent):
                return sent
        return None

    async def caller(self, request: Request, thread_id: str) -> RequestContext | Response:
        """Return the request context of the caller, or the reply to one who is refused.

        Everything the request presented reaches the container: the bearer
        token, if any, and the headers, so that a verifier which trusts the
        hosting platform's perimeter can read what the platform set.
        """
        header = request.headers.get("authorization")
        bearer = _BEARER.match(header) if header is not None else None
        if header is not None and bearer is None:
            return _unauthorized("malformed_authorization")
        request_id = self._request_id(request)
        presented = PresentedCredentials(
            bearer=bearer["token"] if bearer is not None else None,
            headers=dict(request.headers),
        )
        try:
            return await self._services.authenticate(
                presented,
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
            return _unauthorized(refusal.reason_code)
        except AgentCraft2Error as error:
            _LOG.error("the caller could not be signed in", exc_info=error)
            return failure(error, request_id or self._services.ids.new_id()).response()


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


# ------------------------------------------------------------------ the thread


class ThreadResponse(Response):
    """Hold admission in the ASGI task until execution and stream cleanup finish."""

    def __init__(
        self,
        admission: ThreadAdmission,
        context: RequestContext,
        respond: Callable[[], Awaitable[Response]],
        observe: Callable[[Response], None],
    ) -> None:
        super().__init__()
        self._admission = admission
        self._context = context
        self._respond = respond
        self._observe = observe

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer while holding the thread, or say that the thread is busy."""
        async with self._admission.hold(self._context) as acquired:
            response = (
                await self._respond()
                if acquired
                else error_response(409, "thread_busy", request_id=self._context.request_id)
            )
            self._observe(response)
            await response(scope, receive, send)


async def answer_admitted(
    request: Request,
    *,
    path: str,
    prelude: RequestPrelude,
    admission: ThreadAdmission,
    answer: Callable[[Admitted], Awaitable[Response]],
) -> Response:
    """Admit the caller, hold their thread while ``answer`` runs, and log one line about it.

    The line says which route answered, with what status and how long it took;
    it never says what was asked.
    """
    started = time.perf_counter()
    admitted = await prelude.admit(request)
    if isinstance(admitted, Response):
        _report(path, admitted, started)
        return admitted
    return ThreadResponse(
        admission,
        admitted.context,
        lambda: answer(admitted),
        lambda response: _report(path, response, started),
    )


def _report(path: str, response: Response, started: float) -> None:
    _LOG.log(
        logging.ERROR if response.status_code >= SERVER_ERROR else logging.INFO,
        "request",
        extra={
            "route": path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "request_id": response.headers.get(REQUEST_ID_HEADER),
        },
    )

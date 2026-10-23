"""The JSON routes of an agent: run it once, read an outcome, read a turn's diagnostics.

``POST /invoke`` turns one HTTP request into one governed run and answers with
an envelope. ``GET /requests/{id}`` reads what became of an earlier request and
``GET /requests/{id}/inspection`` reads its diagnostics; neither runs anything.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agentcraft2_core.contracts import AgentCraft2Error, PolicyDenied, RequestContext
from agentcraft2_core.contracts.inspection import TurnInspector
from agentcraft2_core.integrations.http.admission import ThreadAdmission
from agentcraft2_core.integrations.http.live import live_response
from agentcraft2_core.integrations.http.outcomes import OutcomeJournal
from agentcraft2_core.integrations.http.prelude import (
    REQUEST_ID_HEADER,
    SERVER_ERROR,
    Admitted,
    Reply,
    RequestPrelude,
    answer_admitted,
    error_reply,
    error_response,
    failure,
    level_of,
)
from agentcraft2_core.integrations.spans import content_free_span
from agentcraft2_core.observability.live import live_run
from agentcraft2_core.observability.trace_links import capture_trace
from agentcraft2_core.pipeline.context import bind_request_context

__all__ = ["INVOKE_PATH", "TERMINAL_ERRORS", "InvokeRoute", "Run"]

INVOKE_PATH = "/invoke"
_INSTRUMENTATION = "agentcraft2_core"
_LOG = logging.getLogger(__name__)

Run = Callable[[RequestContext, Any], Awaitable[Any]]
"""Runs the agent once.

It is given the caller's request context and the ``input`` of the request, and
returns the output, which must be something JSON can hold.
"""

TERMINAL_ERRORS = frozenset({"invalid", "denied", "budget_exceeded", "paused"})
"""The error codes the outcome journal records, besides an accepted answer.

These are refusals the caller must change something to get past, so the same
request would end the same way again. ``unavailable`` and ``internal`` are the
service's own failures and may not recur, so they are not recorded.
``unauthorized`` never reaches the journal: a caller is signed in before the
journal begins their request.
"""


class InvokeRoute:
    """The routes that run the agent for one request, and that read what became of one."""

    def __init__(
        self,
        prelude: RequestPrelude,
        run: Run,
        *,
        application: str,
        admission: ThreadAdmission,
        live_updates: bool = False,
        outcomes: OutcomeJournal | None = None,
        inspector: TurnInspector | None = None,
    ) -> None:
        self._prelude = prelude
        self._run = run
        self._application = application
        self._admission = admission
        self._live_updates = live_updates
        self._outcomes = outcomes
        self._inspector = inspector

    async def handle(self, request: Request) -> Response:
        """Run the agent for one request, describe the outcome and log one line about it."""
        return await answer_admitted(
            request,
            path=INVOKE_PATH,
            prelude=self._prelude,
            admission=self._admission,
            answer=self._admitted_answer,
        )

    async def status(self, request: Request) -> Response:
        """Read an authoritative outcome without running the graph or any tools."""
        lookup = await self._prelude.lookup(request)
        if isinstance(lookup, Response):
            return lookup
        body = (
            await self._outcomes.read(lookup.context, lookup.request_id)
            if self._outcomes is not None
            else None
        )
        return JSONResponse(
            body if body is not None else {"state": "unknown"},
            headers={"cache-control": "no-store"},
        )

    async def inspect(self, request: Request) -> Response:
        """Read diagnostics for the authenticated owner; never run or resume the graph."""
        lookup = await self._prelude.lookup(request)
        if isinstance(lookup, Response):
            return lookup
        if self._inspector is None:
            return error_response(404, "not_found")
        try:
            with bind_request_context(lookup.context):
                result = await self._inspector.read(lookup.context, lookup.request_id)
        except PolicyDenied:
            return error_response(404, "not_found")
        except Exception:  # noqa: BLE001 - never return raw diagnostic exceptions
            # The reply says only "unavailable"; the log says what the inspector raised.
            _LOG.exception(
                "the inspection could not be read",
                extra={"request_id": lookup.request_id, "route": "inspection"},
            )
            return error_response(503, "unavailable")
        return JSONResponse(
            result.model_dump(mode="json", by_alias=True), headers={"cache-control": "no-store"}
        )

    async def _admitted_answer(self, admitted: Admitted) -> Response:
        """Reserve the request only after acquiring exclusive thread execution."""
        context = admitted.context
        if self._outcomes is not None and not await self._outcomes.begin(context):
            return error_response(503, "outcome_unknown", request_id=context.request_id)
        if self._live_updates and admitted.accepts_events:
            return live_response(context, lambda: self._recorded_answer(context, admitted.given))
        return await self._recorded_answer(context, admitted.given)

    async def _recorded_answer(self, context: RequestContext, given: object) -> Response:
        """Run the agent, complete the envelope, journal a terminal outcome, serialise once."""
        started = time.perf_counter()
        with capture_trace(context) as trace:
            reply = await self._answer(context, given)
        envelope: dict[str, object] = {
            **reply.body,
            "thread_id": context.thread_id,
            "request_id": context.request_id,
        }
        if "error" in envelope and trace.link is not None:
            envelope["trace"] = trace.link
        live = live_run(context)
        if live is not None:
            envelope.update(usage=live.usage().model_dump(), model_calls=live.model_calls)
        elapsed_ms = (time.perf_counter() - started) * 1000
        envelope["elapsed_ms"] = elapsed_ms
        if self._outcomes is not None and (
            reply.status == 200 or envelope.get("error") in TERMINAL_ERRORS
        ):
            await self._outcomes.finish(context, envelope)
        _LOG.info(
            "invocation finished",
            extra={
                "request_id": context.request_id,
                "status": reply.status,
                "duration_ms": round(elapsed_ms),
            },
        )
        if self._outcomes is None and live is None and trace.link is None:
            # A plain deployment answers with the bare envelope the run produced.
            return reply.response()
        return JSONResponse(envelope, status_code=reply.status, headers=reply.headers)

    async def _answer(self, context: RequestContext, given: object) -> Reply:
        """Run the agent for one caller and describe the outcome."""
        attributes = {
            "agentcraft2.application": self._application,
            "agentcraft2.request_id": context.request_id,
        }
        try:
            # Inside the run every log line and every governed call belongs to this request.
            with (
                bind_request_context(context),
                content_free_span(_INSTRUMENTATION, "agent.invoke", attributes=attributes),
            ):
                output = await self._run(context, given)
        except AgentCraft2Error as error:
            # What was expected and what was found are logged: the library's
            # errors never put caller content in those.
            _LOG.log(
                level_of(error),
                "the run ended with %s",
                type(error).__name__,
                exc_info=error,
                extra={"request_id": context.request_id},
            )
            return failure(error, context.request_id)
        except Exception:  # noqa: BLE001 - a bug must become a reply and a log line
            # Its type, where it was raised and the line of the agent's own code
            # are logged; its text only where logging is set up with details.
            _LOG.exception("the run failed unexpectedly", extra={"request_id": context.request_id})
            return error_reply(SERVER_ERROR, "internal", request_id=context.request_id)
        return Reply(
            200,
            {"output": output, "thread_id": context.thread_id, "request_id": context.request_id},
            {REQUEST_ID_HEADER: context.request_id},
        )

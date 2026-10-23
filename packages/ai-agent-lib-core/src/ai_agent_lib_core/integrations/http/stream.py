"""The streaming route of an agent: each step of the run as a server-sent event.

``POST /invoke/stream`` takes the same body as ``/invoke`` and answers with
``update`` for each item the agent yields, then ``end``; or ``error`` with a
code, never the error's text.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from anyio import CancelScope
from starlette.requests import Request
from starlette.responses import Response

from ai_agent_lib_core.contracts import AgentCraft2Error, PolicyDenied, RequestContext
from ai_agent_lib_core.integrations.http.admission import ThreadAdmission
from ai_agent_lib_core.integrations.http.outcomes import OutcomeJournal
from ai_agent_lib_core.integrations.http.prelude import (
    REQUEST_ID_HEADER,
    Admitted,
    RequestPrelude,
    answer_admitted,
    error_response,
    level_of,
    status_of,
)
from ai_agent_lib_core.integrations.http.streaming import ClosingStreamingResponse
from ai_agent_lib_core.integrations.spans import content_free_span
from ai_agent_lib_core.pipeline.context import bind_request_context

__all__ = ["STREAM_PATH", "Stream", "StreamRoute"]

STREAM_PATH = "/invoke/stream"
_INSTRUMENTATION = "ai_agent_lib_core"
_LOG = logging.getLogger(__name__)

Stream = Callable[[RequestContext, Any], AsyncIterator[Any]]
"""Runs the agent once and yields what it has done so far, step by step.

Each item must be something JSON can hold, such as the update of one graph
node. Every model and tool call inside is governed as in :data:`Run`.
"""


class StreamRoute:
    """The route that runs the agent for one request and sends each step as it happens."""

    def __init__(
        self,
        prelude: RequestPrelude,
        stream: Stream,
        *,
        application: str,
        admission: ThreadAdmission,
        outcomes: OutcomeJournal | None = None,
    ) -> None:
        self._prelude = prelude
        self._stream = stream
        self._application = application
        self._admission = admission
        self._outcomes = outcomes

    async def handle(self, request: Request) -> Response:
        """Run the agent for one request, stream its steps and log one line about it."""
        return await answer_admitted(
            request,
            path=STREAM_PATH,
            prelude=self._prelude,
            admission=self._admission,
            answer=self._admitted_answer,
        )

    async def _admitted_answer(self, admitted: Admitted) -> Response:
        """Reserve the request only after acquiring exclusive thread execution."""
        context = admitted.context
        if self._outcomes is not None and not await self._outcomes.begin(context):
            return error_response(503, "outcome_unknown", request_id=context.request_id)
        return ClosingStreamingResponse(
            self._events(context, admitted.given),
            media_type="text/event-stream",
            headers={REQUEST_ID_HEADER: context.request_id, "cache-control": "no-cache"},
        )

    async def _events(self, context: RequestContext, given: object) -> AsyncIterator[str]:
        attributes = {
            "agentcraft2.application": self._application,
            "agentcraft2.request_id": context.request_id,
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
        except AgentCraft2Error as error:
            _LOG.log(
                level_of(error),
                "the run ended with %s",
                type(error).__name__,
                exc_info=error,
                extra={"request_id": context.request_id},
            )
            _, name = status_of(error)
            reason = {"reason": error.reason_code} if isinstance(error, PolicyDenied) else {}
            yield _event("error", {"error": name, **reason, **ending})
            return
        except Exception:  # noqa: BLE001 - a bug must become an event and a log line
            _LOG.exception("the run failed unexpectedly", extra={"request_id": context.request_id})
            yield _event("error", {"error": "internal", **ending})
            return
        yield _event("end", ending)


def _event(name: str, data: object) -> str:
    """One server-sent event. JSON holds no raw line breaks, so one data line suffices."""
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"

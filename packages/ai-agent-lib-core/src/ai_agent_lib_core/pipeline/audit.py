"""Record governed call outcomes, including failures and attempted cancellation evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Generic, Protocol, TypeVar

from ai_agent_lib_core.contracts import (
    AuditOutcome,
    AuditRecord,
    AuditSink,
    AuditValue,
    BudgetExceeded,
    Clock,
    ExecutionPaused,
    IdGenerator,
    IntegrityError,
    PolicyDenied,
    RequestContext,
    Telemetry,
)
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["AuditInterceptor", "AuditableCall", "record_refused_sign_in"]

_UNKNOWN = "unknown"
_REFUSALS = (PolicyDenied, BudgetExceeded, ExecutionPaused)


class AuditableCall(Protocol):
    """What the audit stage needs from a request."""

    @property
    def context(self) -> RequestContext | None:
        """The request context, if one was supplied."""
        ...

    @property
    def event(self) -> str:
        """The event name to record, for example ``model.call``."""
        ...

    def audit_attributes(self) -> Mapping[str, AuditValue]:
        """Metadata about the call, including what the stages recorded."""
        ...

    def signal_attributes(self) -> Mapping[str, AuditValue]:
        """The few, low-cardinality facts that label telemetry."""
        ...

    @property
    def span_name(self) -> str:
        """The name of the span around the call."""
        ...

    def span_attributes(self) -> Mapping[str, AuditValue]:
        """The attributes the span around the call opens with."""
        ...


CallT = TypeVar("CallT", bound=AuditableCall)
ResponseT = TypeVar("ResponseT")


_USAGE = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
}


def _ended(
    outcome: AuditOutcome,
    extra: Mapping[str, AuditValue],
    refusal: BaseException | None = None,
) -> dict[str, AuditValue]:
    """What a span learns as its call ends: the outcome, the token usage, a denial's reason."""
    attributes: dict[str, AuditValue] = {"agentlib.outcome": outcome.value}
    attributes.update({_USAGE[key]: value for key, value in extra.items() if key in _USAGE})
    if isinstance(refusal, PolicyDenied):
        attributes["agentlib.reason_code"] = refusal.reason_code
    return attributes


class AuditInterceptor(Generic[CallT, ResponseT]):
    """Record each call's outcome and emit metadata-only telemetry.

    This is the outermost stage, so it sees denials and failures raised by any
    stage inside it. It fails closed: if the record cannot be written, the call
    fails with :class:`IntegrityError`, even if the call itself succeeded.
    This does not roll back an operation that already took effect. During
    cancellation, recording is best effort so an audit failure does not replace
    the caller's cancellation.

    Args:
        sink: Where records are stored.
        telemetry: Where operational signals go.
        clock: The clock port.
        ids: The identifier port.
        describe_response: Optional. Returns metadata about a successful
            response, such as token counts, to add to the record.
    """

    def __init__(
        self,
        sink: AuditSink,
        telemetry: Telemetry,
        clock: Clock,
        ids: IdGenerator,
        describe_response: Callable[[ResponseT], Mapping[str, AuditValue]] | None = None,
    ) -> None:
        self._sink = sink
        self._telemetry = telemetry
        self._clock = clock
        self._ids = ids
        self._describe_response = describe_response

    async def __call__(self, request: CallT, call_next: Handler[CallT, ResponseT]) -> ResponseT:
        """Run the call inside a span, and record how it ended."""
        with self._telemetry.span(request.span_name, request.span_attributes()) as note:
            started = self._clock.monotonic()
            try:
                response = await call_next(request)
            except asyncio.CancelledError as cancelled:
                note(_ended(AuditOutcome.FAILED, {}), cancelled)
                await self._record(
                    request, started, AuditOutcome.FAILED, cancelled, {}, best_effort=True
                )
                raise
            except _REFUSALS as refusal:
                note(_ended(AuditOutcome.DENIED, {}, refusal), refusal)
                await self._record(request, started, AuditOutcome.DENIED, refusal, {})
                raise
            except Exception as error:
                note(_ended(AuditOutcome.FAILED, {}), error)
                await self._record(request, started, AuditOutcome.FAILED, error, {})
                raise
            extra = self._describe_response(response) if self._describe_response else {}
            note(_ended(AuditOutcome.SUCCESS, extra))
            await self._record(request, started, AuditOutcome.SUCCESS, None, extra)
            return response

    async def _record(
        self,
        request: CallT,
        started: float,
        outcome: AuditOutcome,
        error: BaseException | None,
        extra: Mapping[str, AuditValue],
        *,
        best_effort: bool = False,
    ) -> None:
        seconds = max(0.0, self._clock.monotonic() - started)
        attributes: dict[str, AuditValue] = {
            **request.audit_attributes(),
            **extra,
            "duration_ms": round(seconds * 1000, 3),
        }
        if isinstance(error, PolicyDenied):
            attributes["reason_code"] = error.reason_code
        context = request.context
        if context is not None:
            # Who the caller is to the identity provider: a person or an application,
            # and the applications that presented the request for them.
            attributes["principal_kind"] = context.principal.kind.value
            if context.principal.delegation_chain:
                attributes["actors"] = ",".join(context.principal.delegation_chain)
        record = AuditRecord(
            record_id=self._ids.new_id(),
            timestamp=self._clock.now(),
            event=request.event,
            outcome=outcome,
            request_id=context.request_id if context else _UNKNOWN,
            thread_id=context.thread_id if context else _UNKNOWN,
            tenant=context.principal.tenant if context else _UNKNOWN,
            subject=context.principal.subject if context else _UNKNOWN,
            application=context.application if context else _UNKNOWN,
            attributes=attributes,
            error_type=type(error).__name__ if error is not None else None,
        )
        try:
            await self._sink.write(record)
        except Exception as audit_error:
            if best_effort:
                return
            failure = IntegrityError(f"the audit record for {request.event} could not be written")
            if error is not None:
                failure.add_note(f"The call itself had ended with {type(error).__name__}.")
            raise failure from audit_error
        signal = {"outcome": outcome.value, **request.signal_attributes()}
        self._telemetry.event(request.event, signal)
        self._telemetry.duration(request.event, seconds, signal)


SIGN_IN_EVENT = "request.authenticate"


async def record_refused_sign_in(
    sink: AuditSink,
    telemetry: Telemetry,
    clock: Clock,
    ids: IdGenerator,
    *,
    refusal: PolicyDenied,
    application: str,
    request_id: str,
    thread_id: str,
) -> None:
    """Write the audit record of a credential that was refused at the door.

    No caller is known at this point, so the record names none. It says which
    application refused, when and why, which is what makes a run of bad
    tokens visible.

    Raises:
        IntegrityError: If the record could not be written.
    """
    record = AuditRecord(
        record_id=ids.new_id(),
        timestamp=clock.now(),
        event=SIGN_IN_EVENT,
        outcome=AuditOutcome.DENIED,
        request_id=request_id,
        thread_id=thread_id,
        tenant=_UNKNOWN,
        subject=_UNKNOWN,
        application=application,
        attributes={"reason_code": refusal.reason_code},
        error_type=type(refusal).__name__,
    )
    try:
        await sink.write(record)
    except Exception as audit_error:
        raise IntegrityError(
            f"the audit record for {SIGN_IN_EVENT} could not be written"
        ) from audit_error
    telemetry.event(SIGN_IN_EVENT, {"outcome": AuditOutcome.DENIED.value})

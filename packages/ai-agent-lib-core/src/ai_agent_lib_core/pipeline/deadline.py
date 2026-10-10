"""The deadline stage: nothing is released as a success after the caller's deadline.

It sits just inside the audit stage, so it covers every check a call goes
through (budget, identity, policy, guardrails, structured output) as well as
the provider, and the audit stage still records the refusal. The resilience
stage bounds each attempt and its retries by the same deadline; this stage
bounds the whole call.

Cancellation only reaches a coroutine when it yields, so a step that blocks
the event loop can finish late. The clock is therefore read again before a
response is released, and a late response is refused rather than returned.
Blocking work belongs in an executor.
"""

from __future__ import annotations

import asyncio
from typing import Generic, Protocol, TypeVar

from ai_agent_lib_core.contracts import Clock, PolicyDenied, RequestContext
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["DeadlineInterceptor", "deadline_exceeded"]


class _HasContext(Protocol):
    @property
    def context(self) -> RequestContext | None: ...


CallT = TypeVar("CallT", bound=_HasContext)
ResponseT = TypeVar("ResponseT")


def deadline_exceeded(what: str) -> PolicyDenied:
    """The refusal for a call that ran out of the request's time; never retried."""
    return PolicyDenied(
        f"the {what} ran out of the request's time",
        reason_code="deadline_exceeded",
        expected="an answer before the request's deadline",
        fix="give the request a later deadline, or find what is slow",
    )


class DeadlineInterceptor(Generic[CallT, ResponseT]):
    """Bounds the whole governed call by the caller's deadline.

    Args:
        clock: The clock port; the deadline is read against it.
        what: Names the kind of call in the refusal, such as ``"model call"``.
    """

    def __init__(self, clock: Clock, what: str) -> None:
        self._clock = clock
        self._what = what

    async def __call__(self, request: CallT, call_next: Handler[CallT, ResponseT]) -> ResponseT:
        """Run the rest of the pipeline within the time the caller has left."""
        context = request.context
        if not isinstance(context, RequestContext) or context.deadline is None:
            return await call_next(request)  # the identity stage refuses a missing context
        deadline = context.deadline
        remaining = (deadline - self._clock.now()).total_seconds()
        if remaining <= 0:
            raise deadline_exceeded(self._what)
        timeout = asyncio.timeout(remaining)
        try:
            async with timeout:
                response = await call_next(request)
        except TimeoutError:
            if timeout.expired():
                raise deadline_exceeded(self._what) from None
            raise
        if self._clock.now() >= deadline:
            raise deadline_exceeded(self._what)
        return response

"""The resilience stage: a time limit on each attempt, and retries of transient failures.

It is the innermost stage, so policy, guardrails and audit run once around all
of a call's attempts, and the audit record says how many there were.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

from ai_agent_lib_core.contracts import (
    AgentLibError,
    CallLimits,
    Clock,
    RequestContext,
    TransientError,
)
from ai_agent_lib_core.pipeline.calls import Evidence
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["ResilienceInterceptor"]


class _Call(Protocol):
    @property
    def context(self) -> RequestContext | None: ...

    @property
    def evidence(self) -> Evidence: ...

    @property
    def span_name(self) -> str: ...


CallT = TypeVar("CallT", bound=_Call)
ResponseT = TypeVar("ResponseT")


class ResilienceInterceptor(Generic[CallT, ResponseT]):
    """Gives each attempt a time limit and tries a transient failure again.

    A failure is tried again only when its error says it may be
    (``retryable``), only while attempts remain and the request's deadline has
    not passed, and only when ``may_retry`` allows it for the call: a tool that
    changes something is never called twice.

    Args:
        limits: The time limit, the number of retries and the backoff.
        clock: The clock port, for the request's deadline.
        what: Names the kind of call in an error, such as ``"model call"``.
        variable: The setting a developer changes to raise the limits, for the error's fix.
        may_retry: Whether a call may be tried again at all. Every call by default.
        sleep: Waits between attempts. Injected so tests do not wait.
        jitter: Returns a number from 0 to 1 that spreads the waits. Injected for tests.
    """

    def __init__(
        self,
        limits: CallLimits,
        clock: Clock,
        *,
        what: str,
        variable: str = "the limits",
        may_retry: Callable[[CallT], bool] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._what = what
        self._variable = variable
        self._may_retry = may_retry
        self._sleep = sleep
        self._jitter = jitter

    async def __call__(self, request: CallT, call_next: Handler[CallT, ResponseT]) -> ResponseT:
        """Run the call, again after a transient failure while the limits allow."""
        allowed = self._may_retry is None or self._may_retry(request)
        attempts = 1 + (self._limits.retries if allowed else 0)
        for attempt in range(1, attempts + 1):
            try:
                response = await self._attempt(request, call_next)
            except AgentLibError as error:
                if not error.retryable or attempt == attempts or self._past_deadline(request):
                    request.evidence.add(attempts=attempt)
                    if error.retryable and attempt > 1:
                        error.add_note(f"tried {attempt} times, the last error is shown")
                    raise
                await self._sleep(self._wait(attempt))
                continue
            request.evidence.add(attempts=attempt)
            return response
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    async def _attempt(self, request: CallT, call_next: Handler[CallT, ResponseT]) -> ResponseT:
        seconds = self._limits.timeout_seconds
        if seconds is None:
            return await call_next(request)
        try:
            async with asyncio.timeout(seconds):
                return await call_next(request)
        except TimeoutError:
            raise TransientError(
                f"the {self._what} {request.span_name.split(' ', 1)[-1]!r} took longer than "
                f"its limit",
                expected=f"an answer within {seconds:g} seconds",
                actual=f"none after {seconds:g} seconds",
                fix=f"raise timeout_seconds in {self._variable}, or find what is slow",
            ) from None

    def _wait(self, attempt: int) -> float:
        base: float = self._limits.backoff_seconds * float(2 ** (attempt - 1))
        capped = min(base, self._limits.max_backoff_seconds)
        return float(capped * (0.5 + 0.5 * self._jitter()))

    def _past_deadline(self, request: CallT) -> bool:
        context = request.context
        return (
            context is not None
            and context.deadline is not None
            and self._clock.now() >= context.deadline
        )

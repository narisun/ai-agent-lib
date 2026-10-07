"""The identity stage: no verified caller, no call."""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar

from ai_agent_lib_core.contracts import Clock, PolicyDenied, RequestContext
from ai_agent_lib_core.pipeline.interceptor import Handler

__all__ = ["IdentityInterceptor", "require_identified"]


class _HasContext(Protocol):
    @property
    def context(self) -> RequestContext | None: ...


CallT = TypeVar("CallT", bound=_HasContext)
ResponseT = TypeVar("ResponseT")


def require_identified(context: RequestContext | None, clock: Clock) -> RequestContext:
    """Return ``context`` if it names a caller whose deadline has not passed.

    Raises:
        PolicyDenied: If there is no context or the deadline has passed.
    """
    if not isinstance(context, RequestContext):
        raise PolicyDenied(
            "the call has no request context, so the caller is unknown",
            reason_code="identity_missing",
        )
    if context.deadline is not None and clock.now() >= context.deadline:
        raise PolicyDenied("the request deadline has passed", reason_code="deadline_exceeded")
    return context


class IdentityInterceptor(Generic[CallT, ResponseT]):
    """Refuses a call that has no request context or is past its deadline.

    Authority comes only from the context passed beside the call. Nothing in
    the payload is consulted, so prompt content cannot supply an identity.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def __call__(self, request: CallT, call_next: Handler[CallT, ResponseT]) -> ResponseT:
        """Continue only for an identified caller whose deadline has not passed."""
        require_identified(request.context, self._clock)
        return await call_next(request)

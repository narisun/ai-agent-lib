"""The interceptor contract and how interceptors are chained."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, TypeVar

__all__ = ["Handler", "Interceptor", "compose"]

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")

Handler = Callable[[RequestT], Awaitable[ResponseT]]
"""Something that turns a request into a response."""


class Interceptor(Protocol[RequestT, ResponseT]):
    """One step around a call.

    An interceptor may inspect or refuse the request, call ``call_next`` to
    continue, and inspect the response on the way back. Refusing means raising;
    an interceptor never returns a made-up response in place of a denial.
    """

    async def __call__(
        self, request: RequestT, call_next: Handler[RequestT, ResponseT]
    ) -> ResponseT:
        """Handle ``request``, normally by awaiting ``call_next(request)``."""
        ...


def compose(
    interceptors: Sequence[Interceptor[RequestT, ResponseT]],
    terminal: Handler[RequestT, ResponseT],
) -> Handler[RequestT, ResponseT]:
    """Chain ``interceptors`` around ``terminal``; the first one is outermost."""
    handler = terminal
    for interceptor in reversed(interceptors):
        handler = _bind(interceptor, handler)
    return handler


def _bind(
    interceptor: Interceptor[RequestT, ResponseT], call_next: Handler[RequestT, ResponseT]
) -> Handler[RequestT, ResponseT]:
    async def handler(request: RequestT) -> ResponseT:
        return await interceptor(request, call_next)

    return handler

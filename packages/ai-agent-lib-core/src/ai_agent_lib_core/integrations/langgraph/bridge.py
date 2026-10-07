"""Running the async pipelines from synchronous code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

__all__ = ["LoopBridge"]

T = TypeVar("T")


class LoopBridge:
    """Runs async work on the container's event loop from another thread.

    The library is async first. Synchronous entry points such as ``invoke``
    are supported when they are called from a worker thread, which is how
    LangGraph runs synchronous nodes and tools inside an async run. Calling
    them on the event loop thread itself would block the loop forever, so that
    is refused with a clear error instead.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def run(self, make_awaitable: Callable[[], Awaitable[T]]) -> T:
        """Run the awaitable that ``make_awaitable`` returns on the loop and wait for it.

        The awaitable is created on the loop, so nothing is left un-awaited
        when the call is refused.

        Raises:
            RuntimeError: If called on the event loop thread, or the loop is closed.
        """
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop.is_closed():
            raise RuntimeError("the synchronous call cannot run: the event loop is closed")
        if running is self._loop:
            raise RuntimeError(
                "the synchronous call cannot run on the event loop thread; "
                "use the async method, for example ainvoke"
            )

        async def run_on_loop() -> T:
            return await make_awaitable()

        return asyncio.run_coroutine_threadsafe(run_on_loop(), self._loop).result()

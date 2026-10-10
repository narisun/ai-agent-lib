"""Keep an adapter's task-local resources isolated from other adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ai_agent_lib_core.contracts import SupportsAsyncClose


class AdapterLifetime:
    """Construct and close one adapter in its own task and cancellation scopes.

    A retained AnyIO task group may cancel its owner when a child fails. Giving
    each adapter an owner prevents that cancellation from interrupting another
    adapter's factory or cleanup. The container still orders construction and
    teardown, and receives every failure through ``start`` or ``aclose``.

    Args:
        create: Builds the adapter and releases partial resources if it fails.
        claimed: Adapter identities already owned by this container. Returning
            the same instance from two factories must not close it twice.
        on_exit: Notifies the container if a built adapter's task exits before
            normal teardown, so pending startup can be interrupted.
    """

    def __init__(
        self,
        create: Callable[[], Awaitable[object]],
        claimed: set[int],
        on_exit: Callable[[], None],
    ) -> None:
        self._ready: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._startup_cancelled = False
        self._task = asyncio.create_task(self._run(create, claimed, on_exit))

    async def _run(
        self,
        create: Callable[[], Awaitable[object]],
        claimed: set[int],
        on_exit: Callable[[], None],
    ) -> None:
        try:
            service = await create()
        except BaseException as error:  # noqa: BLE001 - delivered to the startup caller
            self._ready.set_exception(error)
            return
        owns = id(service) not in claimed
        claimed.add(id(service))
        self._ready.set_result(service)
        try:
            await self._stop.wait()
        finally:
            try:
                if owns and isinstance(service, SupportsAsyncClose):
                    await service.aclose()
            finally:
                if not self._stop.is_set():
                    on_exit()

    async def start(self) -> object:
        """Return the built adapter, cancelling its construction if startup is cancelled."""
        try:
            return await asyncio.shield(self._ready)
        except asyncio.CancelledError:
            self._startup_cancelled = True
            self._stop.set()
            # Construction can win the race with caller cancellation. In that
            # case request normal teardown; cancelling a built owner's task
            # would misreport a successful close as another failure.
            if not self._ready.done():
                self._task.cancel()
            # The container will await this task during teardown. Retrieve the
            # startup future's eventual cancellation/error even if nobody waits.
            self._ready.add_done_callback(lambda ready: ready.exception())
            raise

    async def aclose(self) -> None:
        """Tell the owning task to close, or surface its earlier background failure."""
        self._stop.set()
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if self._startup_cancelled and not self._ready.done():
                # Cancellation won before the task executed its first line:
                # nothing was created and no cleanup failed.
                self._ready.set_exception(asyncio.CancelledError())
                return
            raise

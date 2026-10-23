"""Thread admission is distinct from deduplicating individual request IDs."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from threading import Lock
from typing import Protocol, runtime_checkable

from agentcraft2_core.contracts import RequestContext

__all__ = ["InMemoryThreadAdmission", "SupportsActivity", "ThreadAdmission"]


class ThreadAdmission(Protocol):
    """Own exclusive execution for a scoped thread through response cleanup.

    Yield False without executing when the thread is busy. Implementations must
    acquire atomically, release on every exit (including cancellation), and never
    expire ownership while the previous graph can still write. Shared deployments
    inject coordination backed by their shared store; a local guard covers only
    the single process using that object. Acquisition failures must fail closed.
    """

    def hold(self, context: RequestContext) -> AbstractAsyncContextManager[bool]:
        """Hold tenant/subject/application/thread ownership in the calling task."""
        ...


@runtime_checkable
class SupportsActivity(Protocol):
    """Optional: an admission guard that can say how many threads it holds right now.

    A hosting platform that asks whether the service is busy, as AgentCore's
    ``GET /ping`` does, reads this. A guard that cannot count, such as one
    backed by a shared store, leaves it out and the service reports itself idle.
    """

    def active_threads(self) -> int:
        """Return how many threads this guard holds in this process at this moment."""
        ...


class InMemoryThreadAdmission:
    """Reject overlapping turns; retain only currently active thread keys.

    Share this instance across apps that use the same checkpoints in one process.
    The short mutex protects the set across event loops; no lock is held over IO.
    """

    def __init__(self) -> None:
        self._active: set[tuple[str, str, str, str]] = set()
        self._mutex = Lock()

    def active_threads(self) -> int:
        """Return how many threads are held right now."""
        with self._mutex:
            return len(self._active)

    @asynccontextmanager
    async def hold(self, context: RequestContext) -> AsyncIterator[bool]:
        """Acquire without waiting and remove the key synchronously on every exit."""
        key = (
            context.principal.tenant,
            context.principal.subject,
            context.application,
            context.thread_id,
        )
        with self._mutex:
            acquired = key not in self._active
            if acquired:
                self._active.add(key)
        try:
            yield acquired
        finally:
            if acquired:
                with self._mutex:
                    self._active.remove(key)

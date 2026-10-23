"""Track HTTP readiness and admission state; the serving layer owns sockets and tasks."""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable

__all__ = ["ServiceLifecycle", "ServiceState"]


class ServiceState(enum.StrEnum):
    """Where a service is between starting and stopping."""

    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"


class ServiceLifecycle:
    """Tracks one service from startup to shutdown.

    A service is *ready* after startup validation and all preparation callbacks
    succeed. A ready service first *drains* on shutdown: readiness becomes false
    while late arrivals are still accepted. It then *stops* accepting new work;
    the serving layer finishes active requests and closes the listener.
    Stopping during startup never opens admission to application requests.

    Args:
        validate: Async startup callback. Use ``services.validate`` for an
            already-started container, or a callback that starts and validates it.
    """

    def __init__(self, validate: Callable[[], Awaitable[None]]) -> None:
        self._validate = validate
        self._state = ServiceState.STARTING
        self._preparations: list[Callable[[], Awaitable[None]]] = []
        self._starting = False
        self._prepared = False

    def prepare(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register async preparation to run after listening, before readiness.

        Graph construction and MCP discovery belong here. Register callbacks
        while building the application, before ``serve`` starts the lifecycle.
        They run in registration order after the startup callback succeeds.
        """
        if self._starting or self._state is not ServiceState.STARTING:
            raise RuntimeError("register preparation before the lifecycle starts")
        self._preparations.append(callback)

    @property
    def state(self) -> ServiceState:
        """Where the service is now."""
        return self._state

    @property
    def ready(self) -> bool:
        """Whether a load balancer should send this service requests."""
        return self._state is ServiceState.READY

    @property
    def accepting(self) -> bool:
        """Whether a request that arrives now is taken on."""
        return self._prepared and self._state in (ServiceState.READY, ServiceState.DRAINING)

    async def start(self) -> None:
        """Run startup and preparation callbacks, then become ready.

        Callback failures and cancellation propagate without making a starting
        service ready. A shutdown request during startup also prevents readiness.

        Raises:
            RuntimeError: If another ``start`` call is already running.
        """
        if self._starting:
            raise RuntimeError("a service lifecycle can start only once")
        self._starting = True
        try:
            await self._validate()
            while self._preparations:
                if self._state is not ServiceState.STARTING:
                    return
                await self._preparations[0]()
                self._preparations.pop(0)
        finally:
            self._starting = False
        if self._state is ServiceState.STARTING:
            self._prepared = True
            self._state = ServiceState.READY

    def begin_drain(self) -> None:
        """Report not ready; keep accepting late arrivals only if startup completed."""
        if self._state in (ServiceState.STARTING, ServiceState.READY):
            self._state = ServiceState.DRAINING

    def stop(self) -> None:
        """Mark the service as refusing new work; socket shutdown is the server's job."""
        self._state = ServiceState.STOPPING

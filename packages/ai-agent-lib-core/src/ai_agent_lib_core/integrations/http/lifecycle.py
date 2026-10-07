"""Whether a service is ready for work, and whether it is stopping."""

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

    A service is *ready* once its startup validation has passed. When it is
    asked to stop it first *drains*: it reports that it is not ready, so a load
    balancer stops sending it requests, and it still accepts the ones that
    arrive. Then it *stops*: it accepts nothing new and finishes what it has.

    Args:
        validate: The startup check, normally ``services.validate``.
    """

    def __init__(self, validate: Callable[[], Awaitable[None]]) -> None:
        self._validate = validate
        self._state = ServiceState.STARTING

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
        return self._state in (ServiceState.READY, ServiceState.DRAINING)

    async def start(self) -> None:
        """Run the startup check and become ready.

        A service that was asked to stop while it was starting stays stopped.

        Raises:
            ConfigurationError: If the check fails. The service stays not ready.
        """
        await self._validate()
        if self._state is ServiceState.STARTING:
            self._state = ServiceState.READY

    def begin_drain(self) -> None:
        """Report not ready, and keep accepting what still arrives."""
        if self._state in (ServiceState.STARTING, ServiceState.READY):
            self._state = ServiceState.DRAINING

    def stop(self) -> None:
        """Accept nothing new."""
        self._state = ServiceState.STOPPING

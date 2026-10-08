"""Serving an HTTP application until the service is asked to stop.

On Fargate a task is stopped with SIGTERM and killed some seconds later. In
between, a service has to leave the load balancer, finish the requests it has
and release what it holds. :func:`serve` does the first two and then returns,
so the caller's ``async with ServiceContainer`` block does the third.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import signal
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import uvicorn

from ai_agent_lib_core.contracts import ConfigurationError, describe
from ai_agent_lib_core.integrations.http.lifecycle import ServiceLifecycle

__all__ = ["serve"]

_POLL_SECONDS = 0.01
_LOG = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """A server that leaves signal handling to :func:`serve`.

    The stock server handles a signal, shuts down and then raises the signal
    again, which ends the process before the caller can close what it holds.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class _Stop:
    """Remembers that the service was asked to stop, and how."""

    def __init__(self, external: asyncio.Event | None) -> None:
        self.requested = external if external is not None else asyncio.Event()
        self.at_once = asyncio.Event()

    def gracefully(self) -> None:
        self.requested.set()

    def now(self) -> None:
        self.at_once.set()
        self.requested.set()


@contextlib.contextmanager
def _signals(stop: _Stop) -> Iterator[None]:
    """Turn SIGTERM into a graceful stop and SIGINT into an immediate one."""
    loop = asyncio.get_running_loop()
    handlers: dict[signal.Signals, Callable[[], None]] = {
        signal.SIGTERM: stop.gracefully,
        signal.SIGINT: stop.now,
    }
    installed: list[signal.Signals] = []
    previous: dict[signal.Signals, Any] = {}
    for number, handler in handlers.items():
        try:
            before = signal.getsignal(number)
            loop.add_signal_handler(number, handler)
            installed.append(number)
            previous[number] = before
        except (NotImplementedError, RuntimeError, ValueError):
            # No loop support (Windows) or not the main thread: fall back, or go without.
            with contextlib.suppress(ValueError):
                previous[number] = signal.signal(
                    number, lambda *_, h=handler: loop.call_soon_threadsafe(h)
                )
    try:
        yield
    finally:
        for number in installed:
            loop.remove_signal_handler(number)
        for number, old in previous.items():
            # Whatever handled the signal before this service does so again.
            signal.signal(number, old)


def _open(family: int, kind: int, protocol: int, address: Any, *, reuse: bool) -> socket.socket:
    listener = socket.socket(family, kind, protocol)
    try:
        if reuse:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(address)
    except BaseException:
        listener.close()
        raise
    return listener


def _bind(host: str, port: int, uds: str | None) -> socket.socket:
    """Open the listening socket here, so a failure is an error and not an exit."""
    try:
        if uds is not None:
            if Path(uds).is_socket():
                Path(uds).unlink()  # left behind by a process that was killed
            return _open(socket.AF_UNIX, socket.SOCK_STREAM, 0, uds, reuse=False)
        family, kind, protocol, _, address = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )[0]
        return _open(family, kind, protocol, address, reuse=True)
    except OSError as exc:
        where = uds or f"{host}:{port}"
        raise ConfigurationError(
            f"the service could not listen on {where} ({describe(exc)})"
        ) from exc


async def _listening(server: uvicorn.Server, serving: asyncio.Future[None]) -> None:
    while not server.started:
        if serving.done():
            serving.result()
            raise ConfigurationError("the service stopped before it was listening")
        await asyncio.sleep(_POLL_SECONDS)


async def _either(*events: asyncio.Event, serving: asyncio.Future[None]) -> None:
    """Wait until one of the events is set or the server ends by itself."""
    waiters: list[asyncio.Future[Any]] = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait([*waiters, serving], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


async def _started(
    lifecycle: ServiceLifecycle, stopping: _Stop, serving: asyncio.Future[None]
) -> bool:
    """Run the startup check until it ends, unless a stop or the server's end comes first.

    A check that hangs, say on a dependency that does not answer, must not
    keep a stop request waiting: the check is cancelled and the service stops.

    Returns:
        Whether the check passed. ``False`` when it was cancelled.

    Raises:
        ConfigurationError: If the check failed.
    """
    starting: asyncio.Future[Any] = asyncio.ensure_future(lifecycle.start())
    stopped: asyncio.Future[Any] = asyncio.ensure_future(stopping.requested.wait())
    racing: list[asyncio.Future[Any]] = [starting, stopped, serving]
    try:
        await asyncio.wait(racing, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
    if starting.done():
        starting.result()  # raises what the check raised
        return True
    starting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await starting
    _LOG.info("stopped before the startup check finished")
    return False


async def serve(
    app: Any,
    lifecycle: ServiceLifecycle,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    uds: str | None = None,
    drain_seconds: float = 5.0,
    grace_seconds: float = 20.0,
    stop: asyncio.Event | None = None,
) -> None:
    """Serve ``app`` until the service is asked to stop, then stop it in order.

    The service listens first and runs its startup check second, so the
    liveness route answers while readiness still says no. On SIGTERM it
    reports not ready for ``drain_seconds`` while it still takes requests,
    then stops listening and gives the requests it has ``grace_seconds`` to
    finish. SIGINT skips the wait. Keep the two times together below the time
    the platform allows a task to stop.

    Args:
        app: An ASGI application, for example from :func:`agent_app` or an MCP
            server's ``streamable_http_app()``.
        lifecycle: The service's lifecycle. It must not have been started.
        host: The address to listen on.
        port: The port to listen on.
        uds: A Unix socket path to listen on instead of an address.
        drain_seconds: How long to report not ready before closing the listener.
        grace_seconds: How long requests in flight get to finish.
        stop: Setting this stops the service as SIGTERM does.

    Raises:
        ConfigurationError: If the service cannot listen, or its startup check fails.
    """
    listener = _bind(host, port, uds)
    config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
        server_header=False,
        ws="none",
        timeout_graceful_shutdown=max(1, math.ceil(grace_seconds)),
    )
    server = _Server(config)
    stopping = _Stop(stop)
    serving: asyncio.Future[None] = asyncio.ensure_future(server.serve(sockets=[listener]))
    try:
        with _signals(stopping):
            await _listening(server, serving)
            _LOG.info("listening", extra={"address": uds or f"{host}:{port}"})
            if not await _started(lifecycle, stopping, serving):
                # Asked to stop, or the server ended, while the startup check ran.
                return
            _LOG.info("ready")
            await _either(stopping.requested, serving=serving)
            lifecycle.begin_drain()
            _LOG.info(
                "draining",
                extra={"drain_seconds": drain_seconds, "grace_seconds": grace_seconds},
            )
            if drain_seconds > 0 and not stopping.at_once.is_set() and not serving.done():
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(drain_seconds):
                        await _either(stopping.at_once, serving=serving)
    finally:
        lifecycle.stop()
        server.should_exit = True
        try:
            await serving
        finally:
            listener.close()
            if uds is not None:
                Path(uds).unlink(missing_ok=True)
            _LOG.info("stopped")

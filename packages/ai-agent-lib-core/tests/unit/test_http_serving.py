"""Serving until told to stop: listen, check, drain, finish what is in flight, return.

The server is real and listens on a Unix socket in a private directory, so
nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_agent_lib_core.contracts import ConfigurationError, Principal, RequestContext
from ai_agent_lib_core.integrations.http import (
    ServiceLifecycle,
    ServiceState,
    add_health_routes,
    agent_app,
    serve,
)
from ai_agent_lib_core.testing import SequentialIds

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs Unix sockets")


class Services:
    ids = SequentialIds()

    async def authenticate(
        self,
        credential: str | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:
        return RequestContext(
            principal=Principal(subject=credential or "dev", tenant="t-1"),
            application=application,
            request_id=request_id or "r-1",
            thread_id=thread_id,
        )


async def _nothing_to_check() -> None:
    return None


class SlowAgent:
    """A run that waits to be released, then records that it finished."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished: list[str] = []

    async def __call__(self, context: RequestContext, given: Any) -> Any:
        if given == "slow":
            self.started.set()
            await self.release.wait()
        self.finished.append(str(given))  # what a checkpoint written at the end stands for
        return {"done": given}


@pytest.fixture
def socket_path() -> Iterator[str]:
    # A short path: a socket's path has a small length limit.
    directory = tempfile.mkdtemp(prefix="eap-http-")
    yield str(Path(directory) / "s")
    shutil.rmtree(directory, ignore_errors=True)


class Running:
    def __init__(self, socket_path: str, *, valid: bool = True, **settings: Any) -> None:
        self.valid = valid
        self.validated = asyncio.Event()
        self.agent = SlowAgent()
        self.lifecycle = ServiceLifecycle(self._validate)
        self.stop = asyncio.Event()
        self.socket_path = socket_path
        app = agent_app(
            Services(), self.agent, application="accounts-agent", lifecycle=self.lifecycle
        )
        settings.setdefault("drain_seconds", 0)
        self.task = asyncio.ensure_future(
            serve(app, self.lifecycle, uds=socket_path, stop=self.stop, **settings)
        )
        self.gate: asyncio.Event | None = None

    async def _validate(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.validated.set()
        if not self.valid:
            raise ConfigurationError("startup validation failed: the audit stream is not active")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self.socket_path),
            base_url="http://agent.test",
            timeout=10,
        )

    async def until(self, state: ServiceState) -> None:
        async with asyncio.timeout(10):
            while self.lifecycle.state is not state:
                if self.task.done():
                    self.task.result()
                await asyncio.sleep(0.01)


@pytest.fixture
async def running(socket_path: str) -> AsyncIterator[Running]:
    service = Running(socket_path)
    await service.until(ServiceState.READY)
    yield service
    service.stop.set()
    service.agent.release.set()
    with contextlib.suppress(Exception):
        await service.task


async def test_a_request_in_flight_finishes_before_the_service_returns(running: Running) -> None:
    async with running.client() as client:
        slow = asyncio.ensure_future(client.post("/invoke", json={"input": "slow"}))
        await running.agent.started.wait()

        running.stop.set()
        await running.until(ServiceState.STOPPING)
        await asyncio.sleep(0.3)
        assert not running.task.done(), "the service returned with a request still running"
        assert running.agent.finished == []

        running.agent.release.set()
        reply = await slow
        assert (reply.status_code, reply.json()["output"]) == (200, {"done": "slow"})
        await asyncio.wait_for(running.task, 10)
    assert running.agent.finished == ["slow"]


async def test_new_work_is_refused_once_the_service_stops(running: Running) -> None:
    async with running.client() as client:
        assert (await client.post("/invoke", json={"input": "one"})).status_code == 200
    running.stop.set()
    await asyncio.wait_for(running.task, 10)
    async with running.client() as client:
        with pytest.raises(httpx.TransportError):
            await client.post("/invoke", json={"input": "two"})
    assert running.agent.finished == ["one"]


async def test_draining_reports_not_ready_and_still_takes_requests(socket_path: str) -> None:
    service = Running(socket_path, drain_seconds=30)
    await service.until(ServiceState.READY)
    async with service.client() as client:
        assert (await client.get("/readyz")).status_code == 200
        service.stop.set()
        await service.until(ServiceState.DRAINING)
        draining = await client.get("/readyz")
        assert (draining.status_code, draining.json()) == (503, {"status": "draining"})
        assert (await client.post("/invoke", json={"input": "late"})).status_code == 200
        assert (await client.get("/healthz")).status_code == 200
    # A second signal, as from a keyboard, ends the wait.
    signal.raise_signal(signal.SIGINT)
    await asyncio.wait_for(service.task, 10)
    assert service.lifecycle.state is ServiceState.STOPPING


async def test_sigterm_stops_the_service_and_leaves_the_process_running(
    socket_path: str,
) -> None:
    handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    service = Running(socket_path)
    await service.until(ServiceState.READY)
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(service.task, 10)
    assert service.lifecycle.state is ServiceState.STOPPING
    # Whatever handled the signals before the service does so again.
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == handlers
    assert not Path(socket_path).exists()


async def test_liveness_answers_while_the_startup_check_is_still_running(
    socket_path: str,
) -> None:
    service = Running(socket_path)
    service.gate = asyncio.Event()
    async with service.client() as client:
        async with asyncio.timeout(10):
            while not Path(socket_path).exists():
                await asyncio.sleep(0.01)
        assert (await client.get("/healthz")).status_code == 200
        starting = await client.get("/readyz")
        assert (starting.status_code, starting.json()) == (503, {"status": "starting"})
        assert (await client.post("/invoke", json={"input": "early"})).status_code == 503
        service.gate.set()
        await service.until(ServiceState.READY)
        assert (await client.get("/readyz")).status_code == 200
    service.stop.set()
    await asyncio.wait_for(service.task, 10)


async def test_r11_a_stop_does_not_wait_for_a_startup_check_that_hangs(
    socket_path: str,
) -> None:
    service = Running(socket_path, grace_seconds=1)
    service.gate = asyncio.Event()  # the check never finishes on its own
    async with asyncio.timeout(10):
        while not Path(socket_path).exists():
            await asyncio.sleep(0.01)
    service.stop.set()
    await asyncio.wait_for(service.task, 5)
    assert not service.validated.is_set()
    assert service.lifecycle.state is ServiceState.STOPPING


async def test_a_failed_startup_check_stops_the_service(socket_path: str) -> None:
    service = Running(socket_path, valid=False)
    with pytest.raises(ConfigurationError, match="the audit stream is not active"):
        await asyncio.wait_for(service.task, 10)
    assert service.lifecycle.state is ServiceState.STOPPING
    async with service.client() as client:
        with pytest.raises(httpx.TransportError):
            await client.get("/healthz")


async def test_an_address_that_cannot_be_used_is_a_configuration_error(tmp_path: Path) -> None:
    lifecycle = ServiceLifecycle(_nothing_to_check)
    missing = str(tmp_path / "no-such-directory" / "socket")
    app = agent_app(Services(), SlowAgent(), application="a", lifecycle=lifecycle)
    with pytest.raises(ConfigurationError, match="could not listen"):
        await asyncio.wait_for(serve(app, lifecycle, uds=missing), 10)
    assert not lifecycle.accepting


async def test_a_request_that_outlasts_the_grace_period_is_cut_off(socket_path: str) -> None:
    service = Running(socket_path, grace_seconds=1)
    await service.until(ServiceState.READY)
    async with service.client() as client:
        slow = asyncio.ensure_future(client.post("/invoke", json={"input": "slow"}))
        await service.agent.started.wait()
        service.stop.set()
        await asyncio.wait_for(service.task, 10)
        # Either an error reply or a closed connection, never the answer.
        with contextlib.suppress(httpx.TransportError):
            assert (await slow).status_code == 500
    assert service.agent.finished == []


class McpLikeServer:
    """Takes custom routes the way the MCP SDK's server does."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}

    def custom_route(self, path: str, methods: list[str]) -> Any:
        assert methods == ["GET"]

        def register(handler: Any) -> Any:
            self.routes[path] = handler
            return handler

        return register


async def test_an_mcp_server_gets_the_same_health_routes() -> None:
    lifecycle = ServiceLifecycle(_nothing_to_check)
    server = McpLikeServer()
    add_health_routes(server, lifecycle)
    assert sorted(server.routes) == ["/healthz", "/readyz"]
    assert (await server.routes["/readyz"](None)).status_code == 503
    await lifecycle.start()
    assert (await server.routes["/readyz"](None)).status_code == 200
    assert (await server.routes["/healthz"](None)).status_code == 200


async def test_a_socket_left_by_a_killed_process_is_replaced(socket_path: str) -> None:
    import socket

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(socket_path)
    stale.close()
    service = Running(socket_path)
    await service.until(ServiceState.READY)
    async with service.client() as client:
        assert (await client.get("/healthz")).status_code == 200
    service.stop.set()
    await asyncio.wait_for(service.task, 10)

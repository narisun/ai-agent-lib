"""Startup cancellation is portable and leaves no constructing container behind."""

import asyncio

import pytest

from ai_agent_lib_core.contracts import ServiceConfig
from ai_agent_lib_core.di import BuildContext, ServiceContainer
from ai_agent_lib_core.integrations.http import ServiceLifecycle
from ai_agent_lib_core.integrations.http.serving import _started, _Stop
from ai_agent_lib_core.testing import Fakes


@pytest.mark.parametrize("exit_reason", ["caller", "stop", "server"])
async def test_leaving_startup_cancels_construction_and_awaits_cleanup(exit_reason: str) -> None:
    entered, released = asyncio.Event(), asyncio.Event()
    providers = Fakes().providers()

    async def build(context: BuildContext) -> object:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            released.set()
        return object()

    providers.register("audit", "fake", build, replace=True)
    services = ServiceContainer(ServiceConfig.for_testing(), providers)
    lifecycle = ServiceLifecycle(services.start)
    stopping = _Stop(None)
    serving: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    starting = asyncio.create_task(_started(lifecycle, stopping, serving))
    async with asyncio.timeout(2):
        await entered.wait()
        if exit_reason == "caller":
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await starting
        else:
            if exit_reason == "stop":
                stopping.gracefully()
            else:
                serving.set_result(None)
            assert not await starting
        assert released.is_set()
        await services.aclose()
    assert not lifecycle.accepting
    serving.cancel()

"""Generated services take ``--profile agentcore``: the runtime's routes, host and port."""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.testing import toolbox_for_tests
from ai_agent_lib_core import Principal, ServiceContainer
from ai_agent_lib_core.integrations.http import PROFILES, SESSION_HEADER, ServiceLifecycle
from ai_agent_lib_core.testing import FakeChatModelProvider, FakeIdentityVerifier, Fakes

SESSION = "runtime-session-0123456789abcdef0123456789abcdef"
ANY_ADDRESS = "0.0.0.0"  # noqa: S104 - the platform reaches the container this way


async def test_the_generated_agent_serves_the_runtime_contract_in_the_agentcore_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolbox = toolbox_for_tests()
    assert main(["init", "demo", "--dir", str(tmp_path)], toolbox) == 0
    workspace = tmp_path / "demo"
    assert main(["new", "agent", "core-agent", "--workspace", str(workspace)], toolbox) == 0
    assert main(["new", "mcp", "core-mcp", "--workspace", str(workspace)], toolbox) == 0
    monkeypatch.syspath_prepend(str(workspace / "agents" / "core-agent" / "src"))
    monkeypatch.syspath_prepend(str(workspace / "mcp-servers" / "core-mcp" / "src"))
    service = importlib.import_module("core_agent.service")
    mcp_main = importlib.import_module("core_mcp.__main__")

    # The profiles say where each kind of service listens for the runtime: an agent
    # takes them from run_agent, an MCP server declares its own.
    assert PROFILES["agentcore"] == (ANY_ADDRESS, 8080)
    assert mcp_main.PROFILES["agentcore"] == (ANY_ADDRESS, 8000)
    assert PROFILES["standard"][0] == mcp_main.PROFILES["standard"][0] == "127.0.0.1"
    assert service.SERVICE.agentcore is False

    kit = Fakes(
        model=FakeChatModelProvider(["Hello Ada"]),
        identity=FakeIdentityVerifier({"test-token": Principal(subject="ada", tenant="t-1")}),
    )
    async with ServiceContainer(kit.config(), kit.providers()) as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await service.build_app(services, lifecycle, agentcore=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            starting = await client.get("/ping")
            assert (starting.status_code, starting.json()) == (503, {"status": "starting"})
            await lifecycle.start()
            assert (await client.get("/ping")).json() == {"status": "Healthy"}
            reply = await client.post(
                "/invocations",
                headers={"authorization": "Bearer test-token", SESSION_HEADER: SESSION},
                json={"input": {"question": "Greet Ada"}},
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["thread_id"] == SESSION
            assert reply.json()["output"]["answer"] == "Hello Ada"
            # The library's own route is still there.
            plain = await client.post(
                "/invoke",
                headers={"authorization": "Bearer test-token"},
                json={"input": {"question": "Greet Ada"}, "thread_id": "one"},
            )
            assert plain.status_code == 200, plain.text

        # The standard profile serves no runtime routes.
        standard = await service.build_app(services, ServiceLifecycle(services.validate))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=standard), base_url="http://agent.test"
        ) as client:
            assert (await client.get("/ping")).status_code == 404

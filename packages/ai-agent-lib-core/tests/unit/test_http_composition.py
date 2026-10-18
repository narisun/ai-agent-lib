"""The shared composition behind every generated agent service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import ai_agent_lib_core.integrations.http.composition as composition
from ai_agent_lib_core.config import PREFIX, Key, variable_for
from ai_agent_lib_core.contracts import Principal, RequestContext, ServiceConfig, ValidationFailed
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.http import (
    AgentService,
    ServiceLifecycle,
    build_agent_app,
    read_question,
    run_agent,
)
from ai_agent_lib_core.testing import Fakes
from ai_agent_lib_core.testing.fakes import FakeIdentityVerifier


def test_read_question_takes_a_non_empty_question() -> None:
    assert read_question({"question": "What is the balance?"}) == "What is the balance?"


@pytest.mark.parametrize("given", [None, {}, {"question": ""}, {"question": 3}, "text"])
def test_read_question_refuses_anything_else(given: object) -> None:
    with pytest.raises(ValidationFailed):
        read_question(given)


def _authenticated_container() -> ServiceContainer:
    """A started-but-not-yet-validated container that accepts ``"token"``."""
    identity = FakeIdentityVerifier({"token": Principal(subject="ann", tenant="t-1")})
    return Fakes(identity=identity).container()


class _Running:
    """Wires an ``AgentService`` the way ``run_agent`` would, for driving in tests."""

    def __init__(self, service: AgentService) -> None:
        self.services = _authenticated_container()

        async def start() -> None:
            await self.services.start()
            await self.services.validate()

        self.lifecycle = ServiceLifecycle(start)
        self._service = service
        self.app: Any = None

    async def build(self) -> None:
        self.app = await build_agent_app(self._service, self.services, self.lifecycle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://agent.test",
        )


async def _build_graph(services: ServiceContainer) -> str:
    return "the-graph"


async def _respond(
    graph: Any, services: ServiceContainer, context: RequestContext, question: str
) -> dict[str, Any]:
    return {"answer": question.upper()}


async def test_an_agent_service_serves_invoke_and_health() -> None:
    service = AgentService(application="probe", build_graph=_build_graph, respond=_respond)
    running = _Running(service)
    await running.build()

    async with running.client() as client:
        assert (await client.get("/healthz")).status_code == 200
        starting = await client.get("/readyz")
        assert starting.status_code == 503

        await running.lifecycle.start()
        assert (await client.get("/readyz")).status_code == 200

        reply = await client.post(
            "/invoke",
            json={"input": {"question": "hi"}, "thread_id": "t-1"},
            headers={"authorization": "Bearer token"},
        )
        assert reply.status_code == 200
        assert reply.json()["output"] == {"answer": "HI"}

        bad = await client.post(
            "/invoke",
            json={"input": {}, "thread_id": "t-2"},
            headers={"authorization": "Bearer token"},
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid"


async def test_build_graph_runs_before_readiness() -> None:
    ran: list[str] = []

    async def build_graph(services: ServiceContainer) -> str:
        ran.append("built")
        return "the-graph"

    service = AgentService(application="probe", build_graph=build_graph, respond=_respond)
    running = _Running(service)
    await running.build()

    async with running.client() as client:
        assert ran == []
        assert (await client.get("/readyz")).status_code == 503

        await running.lifecycle.start()

        assert ran == ["built"]
        assert (await client.get("/readyz")).status_code == 200


# --------------------------------------------------------------- run_agent's own .env file


def test_run_agent_resolves_configuration_from_the_services_own_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env_file`` is the service's own file: run_agent reads it, not ``.env``."""
    env_file = tmp_path / "service.env"
    env_file.write_text(f"{variable_for(Key.DEPLOYMENT_ENV)}=dev\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # a stray ".env" here must not be what is read

    captured: dict[str, Any] = {}

    async def fake_serve(
        service: AgentService, config: ServiceConfig, host: str, port: int
    ) -> None:
        captured["config"] = config

    monkeypatch.setattr(composition, "_serve", fake_serve)
    service = AgentService(
        application="probe", build_graph=_build_graph, respond=_respond, env_file=env_file
    )

    assert run_agent(service, []) == 0
    assert captured["config"].deployment_env.value == "dev"


def test_run_agent_without_an_env_file_reads_the_working_directorys_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``env_file`` the behavior is unchanged: only the process environment."""
    monkeypatch.chdir(tmp_path)  # no ".env" here either
    captured: dict[str, Any] = {}

    async def fake_serve(
        service: AgentService, config: ServiceConfig, host: str, port: int
    ) -> None:
        captured["config"] = config

    monkeypatch.setattr(composition, "_serve", fake_serve)
    service = AgentService(application="probe", build_graph=_build_graph, respond=_respond)

    assert run_agent(service, []) == 0
    assert captured["config"].deployment_env.value == "local"


def test_run_agent_reports_a_configuration_error_from_a_bad_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A problem in the service's own file is reported, not a crash or a silent default."""
    env_file = tmp_path / "service.env"
    env_file.write_text(f"{PREFIX}NONSENSE=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    service = AgentService(
        application="probe", build_graph=_build_graph, respond=_respond, env_file=env_file
    )

    exit_code = run_agent(service, [])

    assert exit_code == 1

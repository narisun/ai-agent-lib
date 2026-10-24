"""The shared composition behind every generated agent service."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

import agentcraft2_core.integrations.http.composition as composition
from agentcraft2_core.config import PREFIX, Key, variable_for
from agentcraft2_core.contracts import (
    DeploymentEnv,
    Principal,
    RequestContext,
    ServiceConfig,
    ValidationFailed,
)
from agentcraft2_core.di import ServiceContainer
from agentcraft2_core.integrations.http import (
    AgentService,
    OutcomeJournal,
    ServiceLifecycle,
    build_agent_app,
    read_question,
    run_agent,
)
from agentcraft2_core.integrations.http.admission import InMemoryThreadAdmission
from agentcraft2_core.integrations.http.composition import _with_outcomes
from agentcraft2_core.integrations.langgraph import tracing as tracing_module
from agentcraft2_core.testing import Fakes
from agentcraft2_core.testing.fakes import FakeIdentityVerifier


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


# --------------------------------------------------------------- config-aware outcome journal


class _FakeJournal:
    """A stand-in journal: identity, not behavior, is what these tests check."""

    async def begin(self, context: RequestContext) -> bool:
        return True

    async def finish(self, context: RequestContext, envelope: dict[str, object]) -> None:
        return None

    async def read(self, context: RequestContext, request_id: str) -> dict[str, object] | None:
        return None


def test_with_outcomes_merges_a_journal_the_factory_returns() -> None:
    journal = _FakeJournal()
    service = AgentService(
        application="probe",
        build_graph=_build_graph,
        respond=_respond,
        outcomes=lambda config: journal,
    )

    merged = _with_outcomes(service, ServiceConfig())

    assert merged.app_kwargs["outcomes"] is journal


def test_with_outcomes_leaves_app_kwargs_alone_when_the_factory_returns_none() -> None:
    service = AgentService(
        application="probe",
        build_graph=_build_graph,
        respond=_respond,
        app_kwargs={"diagnostics": "on"},
        outcomes=lambda config: None,
    )

    merged = _with_outcomes(service, ServiceConfig())

    assert merged is service
    assert "outcomes" not in merged.app_kwargs


def test_with_outcomes_leaves_the_service_alone_without_a_factory() -> None:
    service = AgentService(
        application="probe",
        build_graph=_build_graph,
        respond=_respond,
        app_kwargs={"diagnostics": "on"},
    )

    merged = _with_outcomes(service, ServiceConfig())

    assert merged is service


def test_with_outcomes_passes_the_resolved_configuration_to_the_factory() -> None:
    seen: list[ServiceConfig] = []

    def factory(config: ServiceConfig) -> OutcomeJournal | None:
        seen.append(config)
        return None

    service = AgentService(
        application="probe", build_graph=_build_graph, respond=_respond, outcomes=factory
    )
    config = ServiceConfig(deployment_env=DeploymentEnv.PROD)

    _with_outcomes(service, config)

    assert seen == [config]


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


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_run_agent_logs_a_deprecation_for_each_legacy_spelling_in_its_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A service read through its logs is told when its file uses a retired name.

    The warning is read from standard output, where ``configure_logging`` writes
    its JSON lines, so the test pins that it is emitted after logging is set up:
    a record sent earlier would not arrive there as a structured line.
    """
    from agentcraft2_core.config import deprecations

    deprecations._LOGGED.clear()
    env_file = tmp_path / "service.env"
    env_file.write_text("EAP_DEPLOYMENT_ENV=dev\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def fake_serve(
        service: AgentService, config: ServiceConfig, host: str, port: int
    ) -> None:
        return None

    monkeypatch.setattr(composition, "_serve", fake_serve)
    service = AgentService(
        application="probe", build_graph=_build_graph, respond=_respond, env_file=env_file
    )

    assert run_agent(service, []) == 0
    deprecations._LOGGED.clear()

    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    told = [line for line in lines if "EAP_DEPLOYMENT_ENV" in line["message"]]
    assert len(told) == 1
    assert told[0]["level"] == "WARNING"
    assert told[0]["service"] == "probe"
    assert told[0]["message"] == "EAP_DEPLOYMENT_ENV is deprecated; use AC2_DEPLOYMENT_ENV instead"


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


# --------------------------------------------------------------- app_extras: composition access


class _CountingJournal(_FakeJournal):
    """A journal that counts the requests it reserved: which instance served is observable."""

    def __init__(self) -> None:
        self.begun = 0

    async def begin(self, context: RequestContext) -> bool:
        self.begun += 1
        return True


class _CountingAdmission(InMemoryThreadAdmission):
    """An admission guard that counts the turns it held."""

    def __init__(self) -> None:
        super().__init__()
        self.held = 0

    @asynccontextmanager
    async def hold(self, context: RequestContext) -> AsyncIterator[bool]:
        self.held += 1
        async with super().hold(context) as acquired:
            yield acquired


async def _invoke(running: _Running, question: str = "hi") -> httpx.Response:
    """Ready the service, then send it one authorized question."""
    await running.lifecycle.start()
    async with running.client() as client:
        return await client.post(
            "/invoke",
            json={"input": {"question": question}, "thread_id": "t-1"},
            headers={"authorization": "Bearer token"},
        )


async def test_app_extras_receive_the_container_and_the_prepared_graph() -> None:
    graph = object()
    responded_with: list[object] = []
    calls: list[tuple[ServiceContainer, Callable[[], Any]]] = []

    async def build_graph(services: ServiceContainer) -> object:
        return graph

    async def respond(
        given: Any, services: ServiceContainer, context: RequestContext, question: str
    ) -> dict[str, Any]:
        responded_with.append(given)
        return {"answer": question}

    def extras(services: ServiceContainer, prepared: Callable[[], Any]) -> dict[str, Any]:
        calls.append((services, prepared))
        return {}

    service = AgentService(
        application="probe", build_graph=build_graph, respond=respond, app_extras=extras
    )
    running = _Running(service)
    await running.build()

    assert len(calls) == 1  # composed once, with the live container
    services, prepared = calls[0]
    assert services is running.services
    assert prepared() is None  # nothing is prepared until readiness passes

    reply = await _invoke(running)

    assert reply.status_code == 200
    assert responded_with == [graph]
    assert prepared() is graph  # the very object respond was given
    assert len(calls) == 1  # requests do not compose again


async def test_app_extras_win_over_app_kwargs_on_a_conflicting_key() -> None:
    from_kwargs, from_extras = _CountingJournal(), _CountingJournal()

    service = AgentService(
        application="probe",
        build_graph=_build_graph,
        respond=_respond,
        app_kwargs={"outcomes": from_kwargs},
        app_extras=lambda services, graph: {"outcomes": from_extras},
    )
    running = _Running(service)
    await running.build()

    reply = await _invoke(running)

    assert reply.status_code == 200
    assert from_extras.begun == 1
    assert from_kwargs.begun == 0


async def test_app_extras_add_to_app_kwargs_without_replacing_other_keys() -> None:
    journal, admission = _CountingJournal(), _CountingAdmission()

    service = AgentService(
        application="probe",
        build_graph=_build_graph,
        respond=_respond,
        app_kwargs={"outcomes": journal},
        app_extras=lambda services, graph: {"admission": admission},
    )
    running = _Running(service)
    await running.build()

    reply = await _invoke(running)

    assert reply.status_code == 200
    assert journal.begun == 1  # the service's own app_kwargs survive the merge
    assert admission.held == 1  # and the hook's addition is applied


async def test_without_app_extras_app_kwargs_apply_as_before() -> None:
    journal = _CountingJournal()
    service = AgentService(
        application="probe",
        build_graph=_build_graph,
        respond=_respond,
        app_kwargs={"outcomes": journal},
    )
    running = _Running(service)
    await running.build()

    reply = await _invoke(running)

    assert reply.status_code == 200
    assert journal.begun == 1


# --------------------------------------------------------------- _serve: one tracing instance


def test_run_agent_gives_one_tracing_adapter_to_the_container_and_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter is the model-trace sink and the request tracer: the same instance, once."""
    adapter = object()
    opened: list[tuple[str, ServiceConfig, Path | None]] = []
    container_args: list[dict[str, Any]] = []
    app_args: list[dict[str, Any]] = []
    served: list[Any] = []

    @asynccontextmanager
    async def fake_agent_tracing(
        application: str, config: ServiceConfig, *, dotenv_path: Path | None = None
    ) -> AsyncIterator[object]:
        opened.append((application, config, dotenv_path))
        yield adapter

    class FakeContainer:
        def __init__(self, config: ServiceConfig, **kwargs: Any) -> None:
            container_args.append({"config": config, **kwargs})

        async def aclose(self) -> None:
            return None

    async def fake_build_agent_app(
        service: AgentService, services: Any, lifecycle: Any, **kwargs: Any
    ) -> str:
        app_args.append({"services": services, **kwargs})
        return "the-app"

    async def fake_serve(app: Any, lifecycle: Any, **kwargs: Any) -> None:
        served.append(app)

    monkeypatch.setattr(tracing_module, "agent_tracing", fake_agent_tracing)
    monkeypatch.setattr(composition, "ServiceContainer", FakeContainer)
    monkeypatch.setattr(composition, "build_agent_app", fake_build_agent_app)
    monkeypatch.setattr(composition, "serve", fake_serve)
    env_file = tmp_path / "service.env"
    env_file.write_text("", encoding="utf-8")
    service = AgentService(
        application="probe", build_graph=_build_graph, respond=_respond, env_file=env_file
    )

    assert run_agent(service, []) == 0

    assert [(name, path) for name, _, path in opened] == [("probe", env_file)]
    assert len(container_args) == 1
    assert len(app_args) == 1
    assert container_args[0]["model_trace"] is adapter
    assert app_args[0]["tracing"] is adapter
    assert served == ["the-app"]


# ------------------------------------------------------------------ --profile


def test_the_agentcore_profile_listens_where_the_runtime_reaches_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--profile agentcore`` means 0.0.0.0:8080 and the runtime's routes; flags still win."""
    captured: list[tuple[bool, str, int]] = []

    async def fake_serve(
        service: AgentService, config: ServiceConfig, host: str, port: int
    ) -> None:
        captured.append((service.agentcore, host, port))

    monkeypatch.setattr(composition, "_serve", fake_serve)
    env_file = tmp_path / "service.env"
    env_file.write_text("AC2_PROFILE=agentcore\nAC2_DEPLOYMENT_ENV=dev\n", encoding="utf-8")
    service = AgentService(
        application="probe",
        build_graph=_build_graph,
        respond=_respond,
        env_file=env_file,
        default_port=8123,
    )

    assert run_agent(service, []) == 0
    assert run_agent(service, ["--profile", "agentcore"]) == 0
    assert run_agent(service, ["--profile", "agentcore", "--port", "9000"]) == 0
    assert run_agent(service, ["--host", "10.0.0.5"]) == 0

    assert captured == [
        (False, "127.0.0.1", 8123),
        (True, "0.0.0.0", 8080),  # noqa: S104 - the platform reaches the container this way
        (True, "0.0.0.0", 9000),  # noqa: S104
        (False, "10.0.0.5", 8123),
    ]
    assert service.agentcore is False  # the caller's service is left as it was


def test_listening_for_agentcore_without_the_agentcore_profile_serves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A service that lost its profile does not listen on every address with local defaults."""
    served: list[int] = []

    async def fake_serve(
        service: AgentService, config: ServiceConfig, host: str, port: int
    ) -> None:
        served.append(port)

    monkeypatch.setattr(composition, "_serve", fake_serve)
    env_file = tmp_path / "service.env"
    env_file.write_text("", encoding="utf-8")
    service = AgentService(
        application="probe", build_graph=_build_graph, respond=_respond, env_file=env_file
    )

    assert run_agent(service, ["--profile", "agentcore"]) == 1

    assert served == []
    assert "AC2_PROFILE=agentcore" in capsys.readouterr().err


async def test_an_agentcore_service_serves_the_runtime_contract_beside_its_own() -> None:
    service = AgentService(
        application="probe", build_graph=_build_graph, respond=_respond, agentcore=True
    )
    running = _Running(service)
    await running.build()

    async with running.client() as client:
        assert (await client.get("/ping")).status_code == 503
        await running.lifecycle.start()
        assert (await client.get("/ping")).json() == {"status": "Healthy"}
        reply = await client.post(
            "/invocations",
            json={"input": {"question": "hi"}},
            headers={
                "authorization": "Bearer token",
                "x-amzn-bedrock-agentcore-runtime-session-id": "s" * 33,
            },
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["output"] == {"answer": "HI"}
        assert (await client.get("/healthz")).status_code == 200

    # The standard composition has none of the runtime's routes.
    plain = _Running(AgentService(application="probe", build_graph=_build_graph, respond=_respond))
    await plain.build()
    async with plain.client() as client:
        assert (await client.get("/ping")).status_code == 404

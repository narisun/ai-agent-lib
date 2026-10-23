"""AgentCore Runtime's contract beside the library's own: /invocations, /ping and the session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from opentelemetry import baggage
from pydantic import SecretStr

from ai_agent_lib_core.adapters import NullTelemetry
from ai_agent_lib_core.contracts import (
    PolicyDenied,
    PresentedCredentials,
    Principal,
    RequestContext,
    RequestVerifier,
    Verified,
)
from ai_agent_lib_core.di.authentication import RequestAuthenticator
from ai_agent_lib_core.integrations.http import (
    INVOCATIONS_PATH,
    PING_PATH,
    REQUEST_ID_HEADER_AGENTCORE,
    SESSION_HEADER,
    InMemoryThreadAdmission,
    ServiceLifecycle,
    SupportsActivity,
    agent_app,
    agentcore_app,
)
from ai_agent_lib_core.integrations.http.prelude import _SAFE_ID
from ai_agent_lib_core.testing import FakeRegistry, FrozenClock, InMemoryAuditSink, SequentialIds

APPLICATION = "accounts-agent"
SESSION = "6f3b2c1d-9a8e-4f70-b1c2-d3e4f5a6b7c8-agentcore-session"  # 33 characters or more
USER_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"
WORKLOAD_TOKEN_HEADER = "x-amz-bedrock-agentcore-identity-wat"


class Services:
    """Stands in for the container: a known token, and a record of what was presented."""

    def __init__(self) -> None:
        self.ids = SequentialIds()
        self.presented: list[PresentedCredentials] = []

    async def authenticate(
        self,
        credential: str | PresentedCredentials | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:
        assert isinstance(credential, PresentedCredentials)
        self.presented.append(credential)
        if credential.bearer != "ann-token":
            raise PolicyDenied("the caller is not known", reason_code="invalid_token")
        return RequestContext(
            principal=Principal(subject="ann", tenant="t-1"),
            application=application,
            request_id=request_id or self.ids.new_id(),
            thread_id=thread_id,
        )


class Agent:
    """A run that echoes what it saw, including the session in baggage, and can be held."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, Any]] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def __call__(self, context: RequestContext, given: Any) -> Any:
        self.seen.append((context.principal.subject, context.thread_id, given))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return {
            "thread": context.thread_id,
            "request": context.request_id,
            "session_baggage": baggage.get_baggage("session.id"),
        }


async def _stream(context: RequestContext, given: Any) -> AsyncIterator[dict[str, Any]]:
    yield {"node": "agent", "text": f"{given} for {context.thread_id}"}


async def _ready() -> None:
    return None


class Service:
    def __init__(self, *, agentcore: bool = True, admission: Any = None) -> None:
        self.services = Services()
        self.agent = Agent()
        self.lifecycle = ServiceLifecycle(_ready)
        build = agentcore_app if agentcore else agent_app
        app = build(
            self.services,
            self.agent,
            application=APPLICATION,
            lifecycle=self.lifecycle,
            stream=_stream,
            live_updates=True,
            admission=admission,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://agent.test",
        )

    async def post(
        self,
        path: str = INVOCATIONS_PATH,
        body: Any = None,
        *,
        token: str | None = "ann-token",  # noqa: S107 - a key of the test's own table
        **headers: str,
    ) -> httpx.Response:
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        return await self.client.post(
            path, json={"input": "hi"} if body is None else body, headers=headers
        )


@pytest.fixture
async def service() -> Service:
    started = Service()
    await started.lifecycle.start()
    return started


def _events(body: str) -> list[tuple[str, Any]]:
    frames = [part for part in body.split("\n\n") if part.startswith("event:")]
    found = []
    for frame in frames:
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        found.append((lines["event"], json.loads(lines["data"])))
    return found


# ------------------------------------------------------------ the aliases


async def test_invocations_is_the_invoke_route_under_the_runtime_name(service: Service) -> None:
    reply = await service.post(body={"input": {"q": 1}, "thread_id": "th-1"})
    assert reply.status_code == 200
    assert reply.json()["output"]["thread"] == "th-1"
    assert reply.json()["thread_id"] == "th-1"
    assert service.agent.seen == [("ann", "th-1", {"q": 1})]
    # The library's own routes are still served by the same application.
    plain = await service.post("/invoke", body={"input": 2, "thread_id": "th-1"})
    assert plain.status_code == 200
    assert (await service.client.get("/healthz")).status_code == 200
    assert (await service.client.get("/readyz")).status_code == 200
    stream = await service.post("/invoke/stream", body={"input": 3, "thread_id": "th-1"})
    assert stream.status_code == 200


async def test_invocations_streams_live_events_when_asked_for_them(service: Service) -> None:
    reply = await service.post(
        body={"input": "go", "thread_id": "th-2"}, accept="text/event-stream"
    )
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/event-stream")
    events = _events(reply.text)
    assert events[-1][0] == "result"
    assert events[-1][1]["output"]["thread"] == "th-2"


async def test_invocations_refuses_an_unknown_token_like_invoke_does(service: Service) -> None:
    reply = await service.post(token="who")
    assert reply.status_code == 401
    assert reply.json() == {"error": "unauthorized", "reason": "invalid_token"}


# ------------------------------------------------------------ the session


async def test_safe_id_accepts_the_platforms_long_session_ids_and_uuids() -> None:
    assert len(SESSION) >= 33
    assert _SAFE_ID.fullmatch(SESSION)
    assert _SAFE_ID.fullmatch("6f3b2c1d-9a8e-4f70-b1c2-d3e4f5a6b7c8")
    assert _SAFE_ID.fullmatch("a" * 128)
    assert not _SAFE_ID.fullmatch("a" * 129)
    assert not _SAFE_ID.fullmatch("has space")


async def test_the_session_header_is_the_thread_when_the_body_names_none(
    service: Service,
) -> None:
    reply = await service.post(**{SESSION_HEADER: SESSION})
    assert reply.status_code == 200
    assert reply.json()["thread_id"] == SESSION
    assert service.agent.seen == [("ann", SESSION, "hi")]
    # The same body named with the same thread agrees with the header.
    agreed = await service.post(
        body={"input": 1, "thread_id": SESSION}, **{SESSION_HEADER: SESSION}
    )
    assert agreed.status_code == 200


async def test_a_body_thread_that_disagrees_with_the_session_is_refused(
    service: Service,
) -> None:
    reply = await service.post(body={"input": 1, "thread_id": "other"}, **{SESSION_HEADER: SESSION})
    assert reply.status_code == 400
    assert reply.json() == {
        "error": "invalid",
        "detail": "'thread_id' disagrees with the runtime session",
    }
    assert service.agent.seen == []


async def test_a_session_header_that_is_not_an_identifier_is_refused(service: Service) -> None:
    reply = await service.post(**{SESSION_HEADER: "not an id"})
    assert reply.status_code == 400
    assert reply.json() == {
        "error": "invalid",
        "detail": "the session header is not a usable identifier",
    }
    assert service.agent.seen == []


async def test_without_the_header_a_body_without_a_thread_starts_a_new_one(
    service: Service,
) -> None:
    first, second = (await service.post()).json(), (await service.post()).json()
    assert first["thread_id"] != second["thread_id"]


async def test_the_runtime_request_id_is_read_when_x_request_id_is_absent(
    service: Service,
) -> None:
    reply = await service.post(**{REQUEST_ID_HEADER_AGENTCORE: "runtime-req-1"})
    assert reply.json()["request_id"] == "runtime-req-1"
    assert reply.headers["x-request-id"] == "runtime-req-1"
    preferred = await service.post(
        **{REQUEST_ID_HEADER_AGENTCORE: "runtime-req-2", "x-request-id": "caller-req-2"}
    )
    assert preferred.json()["request_id"] == "caller-req-2"


async def test_the_library_app_ignores_the_runtime_headers() -> None:
    service = Service(agentcore=False)
    await service.lifecycle.start()
    reply = await service.post(
        "/invoke", **{SESSION_HEADER: SESSION, REQUEST_ID_HEADER_AGENTCORE: "runtime-req"}
    )
    assert reply.status_code == 200
    assert reply.json()["thread_id"] != SESSION
    assert reply.json()["request_id"] != "runtime-req"
    assert (await service.client.get(PING_PATH)).status_code == 404
    assert (await service.post(INVOCATIONS_PATH)).status_code == 404


async def test_the_session_is_in_baggage_while_the_run_executes(service: Service) -> None:
    reply = await service.post(**{SESSION_HEADER: SESSION})
    assert reply.json()["output"]["session_baggage"] == SESSION
    # Without the header there is nothing in baggage, and nothing leaks between requests.
    assert (await service.post()).json()["output"]["session_baggage"] is None
    assert baggage.get_baggage("session.id") is None


# ------------------------------------------------------------ /ping


async def test_ping_is_503_with_the_state_until_the_service_is_ready() -> None:
    service = Service()
    reply = await service.client.get(PING_PATH)
    assert reply.status_code == 503
    assert reply.json() == {"status": "starting"}
    await service.lifecycle.start()
    assert (await service.client.get(PING_PATH)).json() == {"status": "Healthy"}
    service.lifecycle.begin_drain()
    draining = await service.client.get(PING_PATH)
    assert (draining.status_code, draining.json()) == (503, {"status": "draining"})
    service.lifecycle.stop()
    assert (await service.client.get(PING_PATH)).json() == {"status": "stopping"}


async def test_ping_reports_busy_while_a_thread_is_held(service: Service) -> None:
    service.agent.release = asyncio.Event()
    task = asyncio.create_task(service.post(body={"input": "slow", "thread_id": "th-9"}))
    try:
        await asyncio.wait_for(service.agent.entered.wait(), 3)
        busy = await service.client.get(PING_PATH)
        assert busy.status_code == 200
        assert busy.json() == {"status": "HealthyBusy"}
        assert "time_of_last_update" not in busy.text
    finally:
        service.agent.release.set()
        assert (await asyncio.wait_for(task, 3)).status_code == 200
    assert (await service.client.get(PING_PATH)).json() == {"status": "Healthy"}


async def test_an_admission_that_cannot_count_reports_healthy() -> None:
    class Uncounted:
        def hold(self, context: RequestContext) -> Any:
            return InMemoryThreadAdmission().hold(context)

    assert not isinstance(Uncounted(), SupportsActivity)
    assert isinstance(InMemoryThreadAdmission(), SupportsActivity)
    service = Service(admission=Uncounted())
    await service.lifecycle.start()
    service.agent.release = asyncio.Event()
    task = asyncio.create_task(service.post(body={"input": "slow", "thread_id": "th-9"}))
    try:
        await asyncio.wait_for(service.agent.entered.wait(), 3)
        assert (await service.client.get(PING_PATH)).json() == {"status": "Healthy"}
    finally:
        service.agent.release.set()
        await asyncio.wait_for(task, 3)


def test_the_in_memory_admission_counts_held_threads() -> None:
    admission = InMemoryThreadAdmission()
    context = RequestContext(
        principal=Principal(subject="ann", tenant="t-1"),
        application=APPLICATION,
        request_id="r-1",
        thread_id="th-1",
    )

    async def hold_and_count() -> tuple[int, int]:
        async with admission.hold(context) as acquired:
            assert acquired
            held = admission.active_threads()
        return held, admission.active_threads()

    assert asyncio.run(hold_and_count()) == (1, 0)


# ------------------------------------------------------------ credentials reach the verifier


@dataclass
class PerimeterIdentity:
    """A verifier that trusts the runtime: the caller is whoever the user-id header names."""

    seen: list[PresentedCredentials] = field(default_factory=list)
    trusts_perimeter: bool = True
    required_headers: tuple[str, ...] = (USER_ID_HEADER,)

    async def verify(self, credential: str | None) -> Principal:
        raise AssertionError("the entry point must present the headers, not the token alone")

    async def verify_request(self, credentials: PresentedCredentials) -> Verified:
        self.seen.append(credentials)
        user = credentials.header(USER_ID_HEADER)
        if user is None:
            raise PolicyDenied("the perimeter set no user", reason_code="perimeter_user_missing")
        workload = credentials.header(WORKLOAD_TOKEN_HEADER)
        return Verified(
            Principal(subject=user, tenant="t-1", authenticated_by="agentcore-runtime"),
            SecretStr(workload) if workload else None,
        )


class VerifyingServices:
    """A container stand-in whose sign-in goes through the real RequestAuthenticator."""

    def __init__(self, identity: PerimeterIdentity) -> None:
        self.ids = SequentialIds()
        self.contexts: list[RequestContext] = []
        self._authenticator = RequestAuthenticator(
            identity=identity,
            registry=FakeRegistry(),
            audit=InMemoryAuditSink(),
            telemetry=NullTelemetry(),
            clock=FrozenClock(),
            ids=self.ids,
        )

    async def authenticate(
        self,
        credential: str | PresentedCredentials | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:
        context = await self._authenticator.authenticate(
            credential, application=application, thread_id=thread_id, request_id=request_id
        )
        self.contexts.append(context)
        return context


async def test_the_headers_reach_a_verifier_that_trusts_the_perimeter() -> None:
    identity = PerimeterIdentity()
    assert isinstance(identity, RequestVerifier)
    services = VerifyingServices(identity)
    agent = Agent()
    lifecycle = ServiceLifecycle(_ready)
    await lifecycle.start()
    app = agentcore_app(services, agent, application=APPLICATION, lifecycle=lifecycle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as client:
        reply = await client.post(
            INVOCATIONS_PATH,
            json={"input": "hi"},
            headers={
                SESSION_HEADER: SESSION,
                USER_ID_HEADER: "user-42",
                WORKLOAD_TOKEN_HEADER: "wat-token",
            },
        )
        assert reply.status_code == 200, reply.text
        refused = await client.post(INVOCATIONS_PATH, json={"input": "hi"})
        assert refused.status_code == 401
        assert refused.json() == {"error": "unauthorized", "reason": "perimeter_user_missing"}
        malformed = await client.post(
            INVOCATIONS_PATH, json={"input": "hi"}, headers={"authorization": "Token x"}
        )
        assert malformed.status_code == 401
        assert malformed.json()["reason"] == "malformed_authorization"

    (presented, _) = identity.seen
    assert presented.bearer is None
    assert presented.header(SESSION_HEADER) == SESSION
    assert presented.header("X-Amzn-Bedrock-AgentCore-Runtime-User-Id") == "user-42"
    (context,) = services.contexts
    assert context.principal.subject == "user-42"
    assert context.principal.authenticated_by == "agentcore-runtime"
    assert context.thread_id == SESSION
    assert context.credential is not None
    assert context.credential.get_secret_value() == "wat-token"
    assert agent.seen == [("user-42", SESSION, "hi")]

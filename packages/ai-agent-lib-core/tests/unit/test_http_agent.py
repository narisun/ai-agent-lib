"""The agent's HTTP entry point: a token decides who calls, a code says what failed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ai_agent_lib_core.contracts import (
    AgentLibError,
    BudgetExceeded,
    ConfigurationError,
    ExecutionPaused,
    IntegrityError,
    PolicyDenied,
    Principal,
    RequestContext,
    TransientError,
    ValidationFailed,
)
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app
from ai_agent_lib_core.testing import SequentialIds

APPLICATION = "accounts-agent"
TOKENS = {"ann-token": "ann", "bo-token": "bo"}


class Services:
    """Stands in for the container: a table of tokens and what was asked of it."""

    def __init__(self) -> None:
        self.ids = SequentialIds()
        self.fail_with: AgentLibError | None = None
        self.asked: list[dict[str, Any]] = []

    async def authenticate(
        self,
        credential: str | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:
        self.asked.append(
            {"credential": credential, "application": application, "request_id": request_id}
        )
        if self.fail_with is not None:
            raise self.fail_with
        if credential not in TOKENS:
            reason = "missing_credential" if credential is None else "invalid_token"
            raise PolicyDenied("the caller is not known", reason_code=reason)
        return RequestContext(
            principal=Principal(subject=TOKENS[credential], tenant="t-1"),
            application=application,
            request_id=request_id or self.ids.new_id(),
            thread_id=thread_id,
        )


class Agent:
    """A run function that echoes, or fails as told."""

    def __init__(self) -> None:
        self.fail_with: BaseException | None = None
        self.seen: list[tuple[str, str, Any]] = []

    async def __call__(self, context: RequestContext, given: Any) -> Any:
        self.seen.append((context.principal.subject, context.thread_id, given))
        if self.fail_with is not None:
            raise self.fail_with
        return {"answer": f"hello {context.principal.subject}", "asked": given}


class Service:
    def __init__(self, *, max_body_bytes: int = 1_000_000) -> None:
        self.services = Services()
        self.agent = Agent()
        self.lifecycle = ServiceLifecycle(self._validate)
        self.valid = True
        app = agent_app(
            self.services,
            self.agent,
            application=APPLICATION,
            lifecycle=self.lifecycle,
            max_body_bytes=max_body_bytes,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://agent.test",
        )

    async def _validate(self) -> None:
        if not self.valid:
            raise ConfigurationError("startup validation failed")

    async def invoke(
        self,
        body: Any = None,
        *,
        token: str | None = "ann-token",  # noqa: S107 - a key of the test's own table
        **headers: str,
    ) -> httpx.Response:
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        payload = {"input": "hi"} if body is None else body
        return await self.client.post("/invoke", json=payload, headers=headers)


@pytest.fixture
async def service() -> Service:
    started = Service()
    await started.lifecycle.start()
    return started


async def test_a_request_with_a_valid_token_runs_the_agent_for_that_caller(
    service: Service,
) -> None:
    reply = await service.invoke({"input": {"question": "balance?"}, "thread_id": "th-7"})
    assert reply.status_code == 200
    assert reply.json() == {
        "output": {"answer": "hello ann", "asked": {"question": "balance?"}},
        "thread_id": "th-7",
        "request_id": "id-1",
    }
    assert reply.headers["x-request-id"] == "id-1"
    assert service.agent.seen == [("ann", "th-7", {"question": "balance?"})]
    assert service.services.asked[0]["application"] == APPLICATION


async def test_a_request_without_a_thread_starts_a_new_one(service: Service) -> None:
    first = (await service.invoke()).json()
    second = (await service.invoke()).json()
    assert first["thread_id"] != second["thread_id"]


async def test_the_callers_request_id_is_kept_when_it_is_safe(service: Service) -> None:
    kept = await service.invoke(**{"x-request-id": "req-2024.10:7"})
    assert kept.json()["request_id"] == "req-2024.10:7"
    replaced = await service.invoke(**{"x-request-id": "bad id\twith spaces"})
    assert replaced.json()["request_id"] != "bad id\twith spaces"


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "missing_credential"),
        ({"authorization": "Bearer not-a-token"}, "invalid_token"),
        ({"authorization": "Basic YW5uOnB3"}, "malformed_authorization"),
        ({"authorization": "Bearer two tokens"}, "malformed_authorization"),
    ],
)
async def test_a_refused_token_is_401_and_the_agent_does_not_run(
    service: Service, headers: dict[str, str], reason: str
) -> None:
    reply = await service.client.post("/invoke", json={"input": "hi"}, headers=headers)
    assert reply.status_code == 401
    assert reply.json() == {"error": "unauthorized", "reason": reason}
    assert reply.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert service.agent.seen == []


async def test_the_token_scheme_is_matched_whatever_its_case(service: Service) -> None:
    reply = await service.client.post(
        "/invoke", json={"input": "hi"}, headers={"authorization": "bearer bo-token"}
    )
    assert reply.json()["output"]["answer"] == "hello bo"


@pytest.mark.parametrize(
    ("failure", "status", "body"),
    [
        (
            PolicyDenied("the policy said no to a-secret-name", reason_code="not_permitted"),
            403,
            {"error": "denied", "reason": "not_permitted"},
        ),
        (ValidationFailed("parameter a-secret-name is wrong"), 400, {"error": "invalid"}),
        (BudgetExceeded("a-secret-name ran out"), 429, {"error": "budget_exceeded"}),
        (ExecutionPaused("a-secret-name waits"), 409, {"error": "paused"}),
        (TransientError("a-secret-name timed out"), 503, {"error": "unavailable"}),
        (IntegrityError("a-secret-name was not recorded"), 500, {"error": "internal"}),
        (ConfigurationError("a-secret-name is not set"), 500, {"error": "internal"}),
    ],
)
async def test_a_failed_run_is_reported_by_a_code_and_never_by_its_text(
    service: Service, failure: AgentLibError, status: int, body: dict[str, str]
) -> None:
    service.agent.fail_with = failure
    reply = await service.invoke(**{"x-request-id": "r-9"})
    assert reply.status_code == status
    assert reply.json() == {**body, "request_id": "r-9"}
    assert "a-secret-name" not in reply.text
    assert (reply.headers.get("retry-after") == "1") == (status == 503)


async def test_an_unexpected_error_is_a_plain_500(service: Service) -> None:
    service.agent.fail_with = RuntimeError("a bug that names a-secret-name")
    reply = await service.invoke()
    assert reply.status_code == 500
    assert "a-secret-name" not in reply.text


async def test_a_sign_in_that_cannot_be_recorded_fails_the_request(service: Service) -> None:
    service.services.fail_with = IntegrityError("the audit record was not accepted")
    reply = await service.invoke()
    assert reply.status_code == 500
    assert reply.json()["error"] == "internal"
    assert service.agent.seen == []


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[1, 2]",
        b'{"thread_id": "t"}',
        b'{"input": 1, "thread_id": 7}',
        b'{"input": 1, "thread_id": "has spaces"}',
        b"\xff\xfe",
    ],
)
async def test_a_malformed_request_is_400_before_anyone_is_asked(
    service: Service, content: bytes
) -> None:
    reply = await service.client.post(
        "/invoke", content=content, headers={"authorization": "Bearer ann-token"}
    )
    assert reply.status_code == 400
    assert reply.json()["error"] == "invalid"
    assert service.services.asked == []


async def test_a_body_over_the_limit_is_413() -> None:
    small = Service(max_body_bytes=64)
    await small.lifecycle.start()
    reply = await small.invoke({"input": "x" * 100})
    assert reply.status_code == 413
    assert small.services.asked == []

    async def chunks() -> Any:
        for _ in range(10):
            yield b"x" * 10

    streamed = await small.client.post(
        "/invoke", content=chunks(), headers={"authorization": "Bearer ann-token"}
    )
    assert streamed.status_code == 413


async def test_only_post_invokes(service: Service) -> None:
    assert (await service.client.get("/invoke")).status_code == 405
    assert (await service.client.get("/elsewhere")).status_code == 404


async def test_health_needs_no_token_and_readiness_follows_the_lifecycle() -> None:
    service = Service()
    service.valid = False
    assert (await service.client.get("/healthz")).json() == {"status": "ok"}
    starting = await service.client.get("/readyz")
    assert (starting.status_code, starting.json()) == (503, {"status": "starting"})
    assert (await service.invoke()).status_code == 503

    with pytest.raises(ConfigurationError):
        await service.lifecycle.start()
    assert (await service.client.get("/readyz")).status_code == 503

    service.valid = True
    await service.lifecycle.start()
    ready = await service.client.get("/readyz")
    assert (ready.status_code, ready.json()) == (200, {"status": "ready"})

    service.lifecycle.begin_drain()
    draining = await service.client.get("/readyz")
    assert (draining.status_code, draining.json()) == (503, {"status": "draining"})
    assert (await service.invoke()).status_code == 200  # still taken on while draining

    service.lifecycle.stop()
    stopped = await service.invoke()
    assert (stopped.status_code, stopped.headers["retry-after"]) == (503, "1")


async def test_drain_before_startup_never_accepts_requests() -> None:
    service = Service()
    service.lifecycle.begin_drain()
    assert not service.lifecycle.accepting
    assert (await service.invoke()).status_code == 503
    await service.lifecycle.start()
    assert not service.lifecycle.accepting
    assert (await service.client.get("/readyz")).json() == {"status": "draining"}
    assert (await service.client.get("/healthz")).status_code == 200


async def test_a_service_told_to_stop_while_starting_never_becomes_ready() -> None:
    lifecycle = ServiceLifecycle(Service()._validate)
    lifecycle.begin_drain()
    await lifecycle.start()
    assert lifecycle.state.value == "draining"
    lifecycle.stop()
    lifecycle.begin_drain()
    assert lifecycle.state.value == "stopping"
    assert not lifecycle.accepting


# ------------------------------------------------------------------ streaming


async def _steps(context: RequestContext, given: Any) -> Any:
    yield {"node": "agent", "text": "thinking"}
    if given == "deny":
        raise PolicyDenied("no rule allows this", reason_code="no_rule")
    if given == "bug":
        raise RuntimeError("the question was private")
    yield {"node": "agent", "text": "done"}


async def _never(context: RequestContext, given: Any) -> Any:
    return {"ok": True}


def _events(body: str) -> list[tuple[str, Any]]:
    import json as _json

    found = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        found.append((lines["event"], _json.loads(lines["data"])))
    return found


async def test_a_streamed_run_sends_each_step_then_the_end() -> None:
    lifecycle = ServiceLifecycle(_no_check)
    await lifecycle.start()
    app = agent_app(
        _StreamServices(), _never, application="accounts-agent", lifecycle=lifecycle, stream=_steps
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
        headers = {"authorization": "Bearer good", "x-request-id": "r-5"}
        ok = await http.post(
            "/invoke/stream", json={"input": "go", "thread_id": "t-1"}, headers=headers
        )
        denied = await http.post("/invoke/stream", json={"input": "deny"}, headers=headers)
        bug = await http.post("/invoke/stream", json={"input": "bug"}, headers=headers)
        refused = await http.post("/invoke/stream", json={"input": "go"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/event-stream")
    assert _events(ok.text) == [
        ("update", {"node": "agent", "text": "thinking"}),
        ("update", {"node": "agent", "text": "done"}),
        ("end", {"thread_id": "t-1", "request_id": "r-5"}),
    ]
    assert _events(denied.text)[-1][1]["error"] == "denied"
    assert _events(denied.text)[-1][1]["reason"] == "no_rule"
    assert _events(bug.text)[-1][1]["error"] == "internal"
    assert "private" not in bug.text
    assert refused.status_code == 401


async def test_without_a_stream_function_there_is_no_streaming_route() -> None:
    lifecycle = ServiceLifecycle(_no_check)
    await lifecycle.start()
    app = agent_app(_StreamServices(), _never, application="a", lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
        reply = await http.post("/invoke/stream", json={"input": "go"})
    assert reply.status_code == 404


async def _no_check() -> None:
    return None


class _StreamServices:
    ids = SequentialIds()

    async def authenticate(
        self, credential: str | None, *, application: str, thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:  # fmt: skip
        if credential != "good":
            raise PolicyDenied("unknown caller", reason_code="invalid_token")
        return RequestContext(
            principal=Principal(subject="ann", tenant="t-1"),
            application=application,
            request_id=request_id or "r-1",
            thread_id=thread_id,
        )

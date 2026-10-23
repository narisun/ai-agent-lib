"""An MCP server behind a perimeter: no bearer, but headers the runtime set, and a door for them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent_lib_core.contracts import (
    AgentEntry,
    ConfigurationError,
    DeploymentEnv,
    IdentityVerifier,
    PolicyDenied,
    PresentedCredentials,
    Principal,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
    Verified,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.mcp import HEALTH_PATHS, PerimeterDoor, perimeter_refused
from ai_agent_lib_core.testing import FakeIdentityVerifier, FakeRegistry, Fakes

USER_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"
WORKLOAD_TOKEN_HEADER = "x-amz-bedrock-agentcore-identity-wat"
PLATFORM_SESSION = "platform-session-0123456789abcdef0123456789abcdef"
SERVER = ServerEntry(
    id="accounts",
    owner="treasury",
    url="https://accounts.internal.example/mcp",
    audience="accounts-mcp",
    tools=(ToolEntry(name="ping", version="1"),),
)
AGENT = AgentEntry(id="accounts-agent", owner="treasury", version="1", mcp_servers=("accounts",))
JSON_RPC_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


@dataclass
class PerimeterIdentity:
    """A verifier that trusts the runtime: the caller is whoever the user-id header names."""

    seen: list[PresentedCredentials] = field(default_factory=list)
    trusts_perimeter: bool = True
    required_headers: tuple[str, ...] = (USER_ID_HEADER,)

    async def verify(self, credential: str | None) -> Principal:
        raise AssertionError("the server must present the headers, not the token alone")

    async def verify_request(self, credentials: PresentedCredentials) -> Verified:
        self.seen.append(credentials)
        user = credentials.header(USER_ID_HEADER)
        if user is None:
            raise PolicyDenied("the perimeter set no user", reason_code="perimeter_user_missing")
        workload = credentials.header(WORKLOAD_TOKEN_HEADER)
        return Verified(
            Principal(
                subject=user,
                tenant="t-1",
                authenticated_by="agentcore-runtime",
                # The runtime names the calling agent in a custom header, say.
                delegation_chain=tuple(filter(None, (credentials.header("x-caller-agent"),))),
            ),
            SecretStr(workload) if workload else None,
        )


def _fakes(identity: IdentityVerifier) -> Fakes:
    return Fakes(identity=identity, registry=FakeRegistry([AGENT], [SERVER]))


@asynccontextmanager
async def governed_server(
    services: ServiceContainer, ran: list[str], *, through_door: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    """A governed MCP server over stateless Streamable HTTP, driven in process."""
    server: MCPServer[Any] = MCPServer("accounts-mcp", **services.mcp_server_kwargs("accounts"))

    @server.tool(name="ping")
    def ping() -> str:
        ran.append("ping")
        return "pong"

    app = server.streamable_http_app(stateless_http=True, json_response=True)
    served = services.mcp_door(app) if through_door else app
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=served), base_url="http://127.0.0.1:8000"
        ) as client,
    ):
        yield client


def _call(name: str, **headers: str) -> dict[str, Any]:
    return {
        "json": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
        "headers": {**JSON_RPC_HEADERS, **headers},
    }


# ------------------------------------------------------------ the SDK contract


async def test_a_stateless_server_accepts_a_platform_set_session_header() -> None:
    ran: list[str] = []
    fakes = _fakes(FakeIdentityVerifier({"token-of-ann": Principal(subject="ann", tenant="t-1")}))
    async with fakes.container() as services, governed_server(services, ran) as client:
        reply = await client.post(
            "/mcp",
            **_call(
                "ping",
                **{"mcp-session-id": PLATFORM_SESSION, "authorization": "Bearer token-of-ann"},
            ),
        )
        assert reply.status_code == 200, reply.text
        result = reply.json()["result"]
        assert result["isError"] is False
        assert result["content"][0]["text"] == "pong"
    assert ran == ["ping"]


# ------------------------------------------------------------ the middleware


async def test_the_middleware_presents_the_headers_to_a_perimeter_trusting_verifier() -> None:
    identity = PerimeterIdentity()
    ran: list[str] = []
    fakes = _fakes(identity)
    async with fakes.container() as services, governed_server(services, ran) as client:
        reply = await client.post(
            "/mcp", **_call("ping", **{USER_ID_HEADER: "user-42", WORKLOAD_TOKEN_HEADER: "wat"})
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["result"]["isError"] is False
        refused = await client.post("/mcp", **_call("ping"))
        assert refused.status_code == 200
        assert refused.json()["result"]["isError"] is True
        assert "denied" in refused.json()["result"]["content"][0]["text"]
        assert "user" not in refused.json()["result"]["content"][0]["text"]
    assert ran == ["ping"]
    first, second = identity.seen
    assert first.bearer is None
    assert first.header("X-Amzn-Bedrock-AgentCore-Runtime-User-Id") == "user-42"
    assert second.header(USER_ID_HEADER) is None
    (policy_asked, *_) = fakes.policy.requests
    assert policy_asked.principal.subject == "user-42"
    assert policy_asked.principal.authenticated_by == "agentcore-runtime"
    accepted, refused_record = [r for r in fakes.audit.records if r.event == "tool.call"]
    assert accepted.subject == "user-42"
    assert refused_record.outcome is not accepted.outcome


# ------------------------------------------------------------ the door


class Echo:
    """An ASGI app that answers 200 and records what reached it."""

    def __init__(self) -> None:
        self.reached: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.fixture
def door() -> tuple[Echo, httpx.AsyncClient]:
    inner = Echo()
    wrapped = PerimeterDoor(inner, required_headers=("X-Amzn-Bedrock-AgentCore-Runtime-User-Id",))
    return inner, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://127.0.0.1:8000"
    )


async def test_the_door_lets_through_the_perimeters_headers_or_a_bearer(
    door: tuple[Echo, httpx.AsyncClient],
) -> None:
    inner, client = door
    by_perimeter = await client.post("/mcp", headers={USER_ID_HEADER: "user-42"})
    by_token = await client.post("/mcp", headers={"authorization": "Bearer t"})
    assert (by_perimeter.status_code, by_token.status_code) == (200, 200)
    assert inner.reached == ["/mcp", "/mcp"]


async def test_the_door_refuses_a_request_the_perimeter_did_not_vouch_for(
    door: tuple[Echo, httpx.AsyncClient],
) -> None:
    inner, client = door
    refused = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert refused.status_code == 401
    assert refused.headers["content-type"] == "application/json"
    assert refused.json() == perimeter_refused()
    assert refused.json() == {"error": "unauthorized", "reason": "perimeter_headers_missing"}
    empty = await client.post("/mcp", headers={USER_ID_HEADER: ""})
    assert empty.status_code == 401
    assert inner.reached == []


async def test_the_door_leaves_the_health_routes_open(
    door: tuple[Echo, httpx.AsyncClient],
) -> None:
    inner, client = door
    assert HEALTH_PATHS == ("/healthz", "/readyz", "/ping")
    for path in HEALTH_PATHS:
        assert (await client.get(path)).status_code == 200
    assert inner.reached == list(HEALTH_PATHS)


async def test_a_door_with_no_required_headers_looks_for_a_bearer_only() -> None:
    inner = Echo()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=PerimeterDoor(inner, required_headers=())),
        base_url="http://127.0.0.1:8000",
    )
    assert (await client.post("/mcp", headers={USER_ID_HEADER: "u"})).status_code == 401
    assert (await client.post("/mcp", headers={"authorization": "Bearer t"})).status_code == 200


# ------------------------------------------------------------ the container


async def test_the_container_puts_a_perimeter_trusting_identity_behind_the_door() -> None:
    identity = PerimeterIdentity()
    ran: list[str] = []
    config = ServiceConfig.for_testing(deployment_env=DeploymentEnv.PROD)
    async with _fakes(identity).container(config) as services:
        kwargs = services.mcp_server_kwargs("accounts")
        assert set(kwargs) == {"middleware"}, (
            "no token door: the perimeter authenticated the caller"
        )
        async with governed_server(services, ran, through_door=True) as client:
            refused = await client.post("/mcp", **_call("ping"))
            assert refused.status_code == 401
            assert refused.json() == perimeter_refused()
            accepted = await client.post(
                "/mcp",
                **_call("ping", **{USER_ID_HEADER: "user-42", "x-caller-agent": "accounts-agent"}),
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["result"]["isError"] is False, accepted.text
    assert ran == ["ping"]


async def test_any_other_identity_leaves_the_app_unchanged() -> None:
    fakes = _fakes(FakeIdentityVerifier())
    async with fakes.container() as services:
        app = object()
        assert services.mcp_door(app) is app


async def test_outside_local_an_identity_that_neither_checks_tokens_nor_trusts_a_perimeter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ServiceConfig.for_testing(deployment_env=DeploymentEnv.PROD)
    async with _fakes(FakeIdentityVerifier()).container(config) as services:
        with pytest.raises(ConfigurationError, match="does not trust a perimeter"):
            services.mcp_server_kwargs("accounts")
    distrustful = PerimeterIdentity(trusts_perimeter=False)
    async with _fakes(distrustful).container(config) as services:
        with pytest.raises(ConfigurationError, match="cannot check tokens at its door"):
            services.mcp_server_kwargs("accounts")
        app = object()
        assert services.mcp_door(app) is app

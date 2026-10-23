"""The HTTP MCP connector: one client for every call, and the server's answers mapped."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from agentcraft2_core import bind_request_context
from agentcraft2_core.adapters import StaticIdentityOptions, StaticIdentityVerifier
from agentcraft2_core.contracts import (
    AgentEntry,
    CallLimits,
    Classification,
    DeploymentEnv,
    Limits,
    NotStartedError,
    PolicyDenied,
    Principal,
    RequestContext,
    ServerEntry,
    ServiceConfig,
    SupportsAsyncClose,
    ToolEntry,
    TransientError,
)
from agentcraft2_core.di.mcp import McpBindings
from agentcraft2_core.integrations.mcp import mcp_result_text
from agentcraft2_core.integrations.mcp.client import HttpMcpConnector, McpConnector, _unwrap
from agentcraft2_core.pipeline import Operations
from agentcraft2_core.testing import FakeRegistry, Fakes, FrozenClock

SERVER = ServerEntry(
    id="accounts",
    owner="treasury",
    url="http://localhost:8001/mcp",
    audience="accounts-mcp",
    tools=(ToolEntry(name="accounts.lookup", version="1.0.0", read_only=True),),
)
AGENT = AgentEntry(
    id="accounts-agent",
    owner="treasury",
    version="0.1.0",
    mcp_servers=("accounts",),
    classification_ceiling=Classification.RESTRICTED,
)
CONTEXT = RequestContext(
    principal=Principal(subject="u-7", tenant="t-9", roles=frozenset({"analyst"})),
    application="accounts-agent",
    request_id="r-42",
    thread_id="th-1",
    classification_ceiling=Classification.RESTRICTED,
)


class _Door(httpx2.AsyncBaseTransport):
    """An MCP server behind an HTTP door that can be told to refuse, or to be away.

    Attributes:
        requests: The method and ``Authorization`` header of every request, in order.
        answer: What the door does instead of letting the server answer: an HTTP
            status to answer with, a transport error to raise, or ``None``.
        closed: How many times the transport was closed.
    """

    def __init__(self, app: Any) -> None:
        self._server = httpx2.ASGITransport(app=app)
        self.requests: list[tuple[str, str | None]] = []
        self.answer: int | type[httpx2.TransportError] | None = None
        self.closed = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((request.method, request.headers.get("authorization")))
        if isinstance(self.answer, int):
            return httpx2.Response(self.answer, text="go away, intruder")
        if self.answer is not None:
            raise self.answer("the door is shut", request=request)
        return await self._server.handle_async_request(request)

    async def aclose(self) -> None:
        self.closed += 1
        await self._server.aclose()


@asynccontextmanager
async def running_server(ran: list[str]) -> AsyncIterator[_Door]:
    """Run an MCP server over Streamable HTTP, in process, behind a door."""
    server: MCPServer[Any] = MCPServer("accounts-mcp")

    @server.tool(name="accounts.lookup", description="Accounts in one region.")
    def lookup(region: str, min_balance: float = 0) -> str:
        ran.append(region)
        return f"accounts in {region} above {min_balance}"

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    async with app.router.lifespan_context(app):
        yield _Door(app)


def connector_through(door: _Door) -> HttpMcpConnector:
    return HttpMcpConnector(timeout_seconds=5.0, transport=door)


async def call_lookup(connector: McpConnector, token: SecretStr | None, region: str) -> str:
    async with connector.connect(SERVER, token) as client:
        result = await client.call_tool("accounts.lookup", {"region": region})
    return mcp_result_text(result)


# ------------------------------------------------------------- the connector


async def test_the_token_is_set_per_call_and_never_kept_on_the_client() -> None:
    ran: list[str] = []
    async with running_server(ran) as door:
        connector = connector_through(door)
        assert await call_lookup(connector, SecretStr("token-of-ann"), "west") == (
            "accounts in west above 0"
        )
        first = len(door.requests)
        assert first > 0
        assert {auth for _, auth in door.requests} == {"Bearer token-of-ann"}

        # A later call for another caller presents that caller's token, and only it.
        await call_lookup(connector, SecretStr("token-of-bob"), "east")
        second = len(door.requests)
        assert {auth for _, auth in door.requests[first:second]} == {"Bearer token-of-bob"}

        # Without a token no header is sent: nothing from an earlier call stays behind.
        await call_lookup(connector, None, "north")
        assert {auth for _, auth in door.requests[second:]} == {None}
        await connector.aclose()
    assert ran == ["west", "east", "north"]


async def test_one_http_client_serves_every_call_and_aclose_closes_it() -> None:
    async with running_server([]) as door:
        connector = connector_through(door)
        assert isinstance(connector, SupportsAsyncClose)
        await call_lookup(connector, SecretStr("t"), "west")
        await call_lookup(connector, SecretStr("t"), "east")
        # Two calls went through the one transport, which was never closed between them.
        assert len(door.requests) >= 2
        assert door.closed == 0

        await connector.aclose()
        assert door.closed == 1
        await connector.aclose()
        assert door.closed == 1, "closing twice closes once"
        with pytest.raises(NotStartedError, match="HTTP connector for MCP server 'accounts'"):
            async with connector.connect(SERVER, None):
                pass  # pragma: no cover - the connector refuses before this line


def test_unwrap_opens_nested_groups_around_one_error_only() -> None:
    inner = httpx2.ConnectError("refused")
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    assert _unwrap(nested) is inner
    assert _unwrap(inner) is inner
    two = ExceptionGroup("two", [inner, ValueError("other")])
    assert _unwrap(two) is two, "a group of two faults is not reduced to one of them"


# ----------------------------------------------------- the agent's error map


class Agent:
    """An agent container whose MCP connector goes through the door."""

    def __init__(self, door: _Door) -> None:
        clock = FrozenClock()
        self.connector = connector_through(door)
        self.fakes = Fakes(
            clock=clock,
            identity=StaticIdentityVerifier(StaticIdentityOptions(), clock),
            registry=FakeRegistry([AGENT], [SERVER], revision="rev-a"),
            mcp_connector=self.connector,
        )
        self.services = self.fakes.container(
            ServiceConfig.for_testing(deployment_env=DeploymentEnv.LOCAL)
        )

    async def __aenter__(self) -> Agent:
        await self.services.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.services.aclose()
        await self.connector.aclose()


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_token_is_a_policy_denial_when_listing_and_when_calling(
    status: int,
) -> None:
    async with running_server([]) as door, Agent(door) as agent:
        door.answer = status
        with pytest.raises(PolicyDenied, match="did not accept this agent's own token") as listing:
            await agent.services.mcp_tools("accounts")
        assert listing.value.reason_code == "server_refused_token"
        assert listing.value.expected == "a token for the server's audience 'accounts-mcp'"
        assert "intruder" not in str(listing.value), "the server's body is not repeated"

        door.answer = None
        (tool,) = await agent.services.mcp_tools("accounts")
        door.answer = status
        with (
            bind_request_context(CONTEXT),
            pytest.raises(PolicyDenied, match="did not accept the caller's token") as calling,
        ):
            await tool.ainvoke({"region": "west"})
        assert calling.value.reason_code == "server_refused_token"
        assert calling.value.retryable is False


@pytest.mark.parametrize("fault", [httpx2.ConnectError, httpx2.ReadTimeout])
async def test_a_server_that_cannot_be_reached_is_a_transient_error(
    fault: type[httpx2.TransportError],
) -> None:
    async with running_server([]) as door, Agent(door) as agent:
        door.answer = fault
        with pytest.raises(TransientError, match="could not be reached") as listing:
            await agent.services.mcp_tools("accounts")
        assert "agentcraft2 run accounts" in (listing.value.fix or "")

        door.answer = None
        (tool,) = await agent.services.mcp_tools("accounts")
        door.answer = fault
        with (
            bind_request_context(CONTEXT),
            pytest.raises(TransientError, match="could not be reached") as calling,
        ):
            await tool.ainvoke({"region": "west"})
        assert calling.value.actual == fault.__name__
        assert calling.value.retryable is True


# ------------------------------------------------------- the composition root


class _NoBindings:
    """Stands in for the LangGraph bindings: checks it is handed a connector, loads nothing."""

    async def mcp_tools(self, server: ServerEntry, **kwargs: Any) -> list[Any]:
        assert server.id == "accounts"
        assert kwargs["connector"] is not None
        return []


class _InjectedConnector:
    """A caller-owned connector, which the bindings must not close."""

    def __init__(self) -> None:
        self.closed = 0

    def connect(
        self, server: ServerEntry, token: SecretStr | None
    ) -> AbstractAsyncContextManager[Any]:
        raise AssertionError("never connected in this test")

    def credential_meta(self, token: SecretStr | None) -> dict[str, str]:
        return {}

    async def aclose(self) -> None:
        self.closed += 1


def mcp_bindings(connector: McpConnector | None, timeout_seconds: float | None) -> McpBindings:
    fakes = Fakes(registry=FakeRegistry([AGENT], [SERVER]))
    return McpBindings(
        audit=fakes.audit,
        telemetry=fakes.telemetry,
        clock=fakes.clock,
        ids=fakes.ids,
        identity=fakes.identity,
        registry=fakes.registry,
        policy=fakes.policy,
        guardrails=fakes.guardrails,
        environment=DeploymentEnv.LOCAL,
        identity_provider="fake",
        operations=Operations(limits=Limits(tool=CallLimits(timeout_seconds=timeout_seconds))),
        connector=connector,
        bindings=cast(Any, _NoBindings),
    )


async def test_the_bindings_create_one_connector_with_the_configured_timeout_and_close_it() -> None:
    bindings = mcp_bindings(None, timeout_seconds=7.5)
    await bindings.mcp_tools("accounts")
    await bindings.mcp_tools("accounts")
    connector = bindings.mcp_connector()
    assert isinstance(connector, HttpMcpConnector)
    assert connector.timeout_seconds == 7.5, "the timeout is limits.tool.timeout_seconds"
    assert bindings.mcp_connector() is connector, "one connector for the life of the bindings"

    await bindings.aclose()
    with pytest.raises(NotStartedError):
        async with connector.connect(SERVER, None):
            pass  # pragma: no cover - the connector refuses before this line
    await bindings.aclose()  # closing again is harmless


async def test_no_timeout_in_the_limits_means_none_on_the_connector() -> None:
    connector = mcp_bindings(None, timeout_seconds=None).mcp_connector()
    assert isinstance(connector, HttpMcpConnector)
    assert connector.timeout_seconds is None
    await connector.aclose()


async def test_an_injected_connector_is_used_as_it_is_and_left_to_its_owner() -> None:
    injected = _InjectedConnector()
    bindings = mcp_bindings(injected, timeout_seconds=7.5)
    assert bindings.mcp_connector() is injected
    await bindings.aclose()
    assert injected.closed == 0

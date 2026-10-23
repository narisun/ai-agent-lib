"""The AgentCore MCP connector: the runtime's URL, the credential on each request, its errors."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent_lib_aws.mcp_agentcore import (
    SESSION_HEADER,
    AgentCoreMcpConnector,
    classify_agentcore_mcp_error,
)
from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_aws.testing import offline_sessions
from ai_agent_lib_core import bind_request_context
from ai_agent_lib_core.adapters import StaticIdentityOptions, StaticIdentityVerifier
from ai_agent_lib_core.contracts import (
    AgentCraft2Error,
    AgentEntry,
    Classification,
    ConfigurationError,
    DeploymentEnv,
    NotStartedError,
    PolicyDenied,
    Principal,
    RequestContext,
    ServerEntry,
    ServiceConfig,
    SupportsAsyncClose,
    ToolEntry,
    TransientError,
    UpstreamError,
    ValidationFailed,
)
from ai_agent_lib_core.integrations.mcp import mcp_result_text
from ai_agent_lib_core.integrations.mcp.client import SupportsErrorClassification
from ai_agent_lib_core.kit import ConnectorContext, ServiceProviders
from ai_agent_lib_core.testing import FakeRegistry, Fakes, FrozenClock

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/accounts_mcp-abc123"
HOST = "https://bedrock-agentcore.us-east-1.amazonaws.com"
PATH = (
    "/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012%3Aruntime%2F"
    "accounts_mcp-abc123/invocations"
)
TOOLS = (ToolEntry(name="accounts.lookup", version="1.0.0", read_only=True),)
BEARER = ServerEntry(
    id="accounts", owner="treasury", runtime_arn=ARN, audience="accounts-mcp", tools=TOOLS
)
SIGNED = ServerEntry(id="accounts", owner="treasury", runtime_arn=ARN, auth="sigv4", tools=TOOLS)
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
WHERE = "MCP server 'accounts', tool 'accounts.lookup'"


class _Platform(httpx2.AsyncBaseTransport):
    """The platform in front of an in-process MCP server: it can refuse, fail or hand out sessions.

    Attributes:
        requests: Every request the platform saw, in order.
        session_id: The ``Mcp-Session-Id`` to return on every response, if any.
        status: An HTTP status to answer every request with, instead of the server.
        jsonrpc_error: A JSON-RPC error to answer every ``tools/call`` with, over HTTP 200.
        closed: How many times the transport was closed.
    """

    def __init__(self, app: Any) -> None:
        self._server = httpx2.ASGITransport(app=app)
        self.requests: list[httpx2.Request] = []
        self.urls: list[str] = []
        self.session_id: str | None = None
        self.status: int | None = None
        self.jsonrpc_error: tuple[int, str] | None = None
        self.closed = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.urls.append(str(request.url))
        if self.status is not None:
            return httpx2.Response(self.status, json={"message": "go away, intruder"})
        body = json.loads(request.content) if request.content else {}
        if self.jsonrpc_error is not None and body.get("method") == "tools/call":
            code, message = self.jsonrpc_error
            response = httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": code, "message": message},
                },
            )
        else:
            # The platform serves the runtime at its invocation path and terminates TLS;
            # the server behind it listens on /mcp and knows only its own host.
            request.url = request.url.copy_with(raw_path=b"/mcp")
            request.headers["host"] = "localhost:8001"
            response = await self._server.handle_async_request(request)
        if self.session_id is not None:
            response.headers[SESSION_HEADER] = self.session_id
        return response

    async def aclose(self) -> None:
        self.closed += 1
        await self._server.aclose()


@asynccontextmanager
async def hosted_server(ran: list[str]) -> AsyncIterator[_Platform]:
    """Run an MCP server in process, behind the platform."""
    server: MCPServer[Any] = MCPServer("accounts-mcp")

    @server.tool(name="accounts.lookup", description="Accounts in one region.")
    def lookup(region: str, min_balance: float = 0) -> str:
        ran.append(region)
        return f"accounts in {region} above {min_balance}"

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    async with app.router.lifespan_context(app):
        yield _Platform(app)


def connector_through(
    platform: _Platform, sessions: AwsSessionFactory | None = None
) -> AgentCoreMcpConnector:
    return AgentCoreMcpConnector(
        sessions if sessions is not None else offline_sessions(),
        timeout_seconds=5.0,
        transport=platform,
    )


async def call_lookup(
    connector: AgentCoreMcpConnector, server: ServerEntry, token: str | None
) -> str:
    async with connector.connect(server, SecretStr(token) if token else None) as client:
        result = await client.call_tool("accounts.lookup", {"region": "west"})
    return mcp_result_text(result)


# ------------------------------------------------------------- the address


async def test_the_runtime_is_reached_at_the_url_derived_from_its_arn() -> None:
    ran: list[str] = []
    async with hosted_server(ran) as platform:
        connector = connector_through(platform)
        assert await call_lookup(connector, BEARER, "token-of-ann") == "accounts in west above 0"
        await connector.aclose()
    assert ran == ["west"]
    assert platform.urls, "the platform saw the requests"
    assert set(platform.urls) == {f"{HOST}{PATH}?qualifier=DEFAULT"}


# ---------------------------------------------------------- the credential


async def test_bearer_mode_presents_the_callers_token_and_signs_nothing() -> None:
    async with hosted_server([]) as platform:
        connector = connector_through(platform)
        await call_lookup(connector, BEARER, "token-of-ann")
        first = len(platform.requests)
        await call_lookup(connector, BEARER, None)
        await connector.aclose()
    for request in platform.requests[:first]:
        assert request.headers["authorization"] == "Bearer token-of-ann"
        assert "x-amz-date" not in request.headers
    for request in platform.requests[first:]:
        assert "authorization" not in request.headers, "no token, no header; nothing stays behind"


async def test_sigv4_mode_signs_every_request_for_the_runtimes_region() -> None:
    async with hosted_server([]) as platform:
        connector = connector_through(platform)
        await call_lookup(connector, SIGNED, "token-that-is-ignored")
        await connector.aclose()
    assert platform.requests
    for request in platform.requests:
        authorization = request.headers["authorization"]
        assert authorization.startswith("AWS4-HMAC-SHA256 Credential=testing/")
        assert "/us-east-1/bedrock-agentcore/aws4_request" in authorization, (
            "the region comes from the ARN, not from the session's own region"
        )
        assert "SignedHeaders=" in authorization
        assert "host;" in authorization
        assert "x-amz-date" in request.headers
        assert "Bearer" not in authorization


async def test_a_session_without_credentials_cannot_sign() -> None:
    class _NoCredentials:
        region_name = "eu-west-1"

        def get_credentials(self) -> None:
            return None

    sessions = AwsSessionFactory(session_factory=lambda **_: _NoCredentials())
    with pytest.raises(ConfigurationError, match="no credentials to sign with"):
        sessions.credentials()


# ------------------------------------------------------ the platform session


async def test_the_platform_session_is_kept_per_server_and_sent_on_later_calls() -> None:
    async with hosted_server([]) as platform:
        connector = connector_through(platform)
        assert connector.session_id("accounts") is None
        platform.session_id = "s-1"
        await call_lookup(connector, BEARER, "t")
        first = len(platform.requests)
        assert SESSION_HEADER not in platform.requests[0].headers, "nothing known yet"
        assert connector.session_id("accounts") == "s-1"

        platform.session_id = None
        await call_lookup(connector, BEARER, "t")
        for request in platform.requests[first:]:
            assert request.headers[SESSION_HEADER] == "s-1", "every request, initialize included"
        assert not any(r.method == "DELETE" for r in platform.requests), (
            "the SDK's session is not terminated: the platform's outlives it"
        )

        other = ServerEntry(id="payments", owner="treasury", runtime_arn=ARN, tools=TOOLS)
        second = len(platform.requests)
        await call_lookup(connector, other, "t")
        assert all(SESSION_HEADER not in r.headers for r in platform.requests[second:]), (
            "a session belongs to one registered server"
        )
        await connector.aclose()


# ------------------------------------------------------------- the lifecycle


async def test_one_http_client_serves_every_call_and_aclose_closes_it() -> None:
    async with hosted_server([]) as platform:
        connector = connector_through(platform)
        assert isinstance(connector, SupportsAsyncClose)
        assert isinstance(connector, SupportsErrorClassification)
        assert connector.timeout_seconds == 5.0
        assert connector.credential_meta(SecretStr("t")) == {}
        await call_lookup(connector, BEARER, "t")
        await call_lookup(connector, SIGNED, "t")
        assert platform.closed == 0
        await connector.aclose()
        assert platform.closed == 1
        await connector.aclose()
        assert platform.closed == 1
        with pytest.raises(NotStartedError, match="AgentCore connector for MCP server 'accounts'"):
            async with connector.connect(BEARER, None):
                pass  # pragma: no cover - the connector refuses before this line


def test_the_pack_registers_the_connector_for_the_agentcore_transport() -> None:
    sessions = offline_sessions()
    providers = register_aws_adapters(ServiceProviders(), sessions=sessions)
    factory = providers.mcp_connector_factory("agentcore")
    assert factory is not None
    assert providers.mcp_connector_factory("http") is None
    context = ConnectorContext(
        deployment_env=DeploymentEnv.LOCAL,
        tls_ca_bundle=None,
        external=ServiceConfig.for_testing().external,
        clock=FrozenClock(),
        timeout_seconds=12.0,
    )
    connector = factory(context)
    assert isinstance(connector, AgentCoreMcpConnector)
    assert connector.timeout_seconds == 12.0


# ------------------------------------------------------------- the error map


@pytest.mark.parametrize(
    ("code", "message", "kind", "fact"),
    [
        (-32001, "UnauthorizedException", PolicyDenied, "server_refused_token"),
        (-32002, "AccessDeniedException", PolicyDenied, "server_denied"),
        (-32003, "ThrottlingException", TransientError, "throttling"),
        (-32005, "Session operation in progress, please retry", TransientError, "in progress"),
        (-32005, "ConflictException", UpstreamError, "ConflictException"),
        (-32004, "ResourceNotFoundException", ConfigurationError, "runtime_arn"),
        (-32006, "ValidationException", ValidationFailed, "invalid"),
        (-32010, "RuntimeClientError", UpstreamError, "RuntimeClientError"),
        (-32603, "Internal error", UpstreamError, "InternalError"),
    ],
)
def test_each_platform_code_has_its_place_in_the_taxonomy(
    code: int, message: str, kind: type[AgentCraft2Error], fact: str
) -> None:
    error = MCPError(code=code, message=f"{message} (the caller sent hunter2)")
    mapped = classify_agentcore_mcp_error(error, where=WHERE)
    assert isinstance(mapped, kind)
    assert mapped.message.startswith(WHERE)
    assert "hunter2" not in str(mapped), "the platform's text is never in the message or facts"
    assert mapped.detail is not None
    assert "hunter2" in mapped.detail, "the platform's text is kept in detail only"
    facts = str(mapped) + (mapped.reason_code if isinstance(mapped, PolicyDenied) else "")
    assert fact in facts
    assert mapped.retryable is (kind is TransientError)


def test_other_errors_are_left_to_the_agent_side() -> None:
    assert (
        classify_agentcore_mcp_error(MCPError(code=-32601, message="no such method"), where=WHERE)
        is None
    )
    assert classify_agentcore_mcp_error(ValueError("not a platform error"), where=WHERE) is None
    assert classify_agentcore_mcp_error(TransientError("already mapped"), where=WHERE) is None
    request = httpx2.Request("POST", HOST)
    refused = httpx2.HTTPStatusError(
        "403", request=request, response=httpx2.Response(403, request=request)
    )
    assert classify_agentcore_mcp_error(refused, where=WHERE) is None, (
        "a refusal at the door keeps its status for the agent side's own mapping"
    )


class Agent:
    """An agent container whose MCP connector goes through the platform."""

    def __init__(self, platform: _Platform) -> None:
        clock = FrozenClock()
        self.connector = connector_through(platform)
        self.fakes = Fakes(
            clock=clock,
            identity=StaticIdentityVerifier(StaticIdentityOptions(), clock),
            registry=FakeRegistry([AGENT], [BEARER], revision="rev-a"),
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


async def test_the_platforms_answers_reach_the_agent_through_the_connector() -> None:
    async with hosted_server([]) as platform, Agent(platform) as agent:
        (tool,) = await agent.services.mcp_tools("accounts")
        assert tool.name == "accounts_lookup", "the model-safe spelling of the registered name"

        platform.jsonrpc_error = (-32001, "UnauthorizedException: bad token")
        with bind_request_context(CONTEXT), pytest.raises(PolicyDenied) as denied:
            await tool.ainvoke({"region": "west"})
        assert denied.value.reason_code == "server_refused_token"
        assert denied.value.message == f"{WHERE}: the runtime did not accept the caller's token"
        assert denied.value.__cause__ is None

        platform.jsonrpc_error = (-32003, "ThrottlingException")
        with bind_request_context(CONTEXT), pytest.raises(TransientError) as throttled:
            await tool.ainvoke({"region": "west"})
        assert isinstance(throttled.value.__cause__, MCPError)

        platform.jsonrpc_error = (-32004, "ResourceNotFoundException")
        with bind_request_context(CONTEXT), pytest.raises(ConfigurationError) as missing:
            await tool.ainvoke({"region": "west"})
        assert missing.value.fix == (
            "check runtime_arn and qualifier of the server in the tool registry"
        )

        platform.jsonrpc_error = None
        with bind_request_context(CONTEXT):
            assert "accounts in west" in await tool.ainvoke({"region": "west"})


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refusal_at_the_door_is_a_policy_denial_when_listing_and_when_calling(
    status: int,
) -> None:
    async with hosted_server([]) as platform, Agent(platform) as agent:
        platform.status = status
        with pytest.raises(PolicyDenied, match="did not accept this agent's own token") as listing:
            await agent.services.mcp_tools("accounts")
        assert listing.value.reason_code == "server_refused_token"
        assert "intruder" not in str(listing.value)

        platform.status = None
        (tool,) = await agent.services.mcp_tools("accounts")
        platform.status = status
        with (
            bind_request_context(CONTEXT),
            pytest.raises(PolicyDenied, match="did not accept the caller's token") as calling,
        ):
            await tool.ainvoke({"region": "west"})
        assert calling.value.reason_code == "server_refused_token"

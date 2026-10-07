"""The door of an MCP server: over HTTP, nothing is answered without a valid token."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent_lib_core import bind_request_context
from ai_agent_lib_core.adapters import (
    JwtIdentityOptions,
    JwtIdentityVerifier,
    StaticIdentityOptions,
    StaticIdentityVerifier,
    resolve_jwt_options,
)
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AuditOutcome,
    ConfigurationError,
    DeploymentEnv,
    EntryStatus,
    IntegrityError,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
)
from ai_agent_lib_core.integrations.mcp import DoorTokenVerifier
from ai_agent_lib_core.testing import FakeRegistry, Fakes, FrozenClock
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

API = "api://accounts-mcp"
AGENT_CLIENT = "22222222-2222-2222-2222-222222222222"
OTHER_CLIENT = "33333333-3333-3333-3333-333333333333"
SERVER = ServerEntry(
    id="accounts",
    owner="treasury",
    url="https://accounts.internal.example/mcp",
    audience=API,
    tools=(ToolEntry(name="ping", version="1"),),
)
AGENT = AgentEntry(
    id="accounts-agent",
    owner="treasury",
    version="1",
    mcp_servers=("accounts",),
    client_id=AGENT_CLIENT,
)


@pytest.fixture(scope="module")
def idp() -> FakeIdentityProvider:
    """One provider for the module: generating a signing key is the slow part."""
    return FakeIdentityProvider()


def identity(idp: FakeIdentityProvider) -> JwtIdentityVerifier:
    settings = resolve_jwt_options(JwtIdentityOptions.model_validate(idp.options(API)))
    return JwtIdentityVerifier(settings, FrozenClock(), transport=idp.transport)


def door(
    idp: FakeIdentityProvider,
    fakes: Fakes,
    *,
    agents: tuple[AgentEntry, ...] = (AGENT,),
    require_agent: bool = True,
) -> DoorTokenVerifier:
    return DoorTokenVerifier(
        identity=identity(idp),
        server=SERVER,
        application="accounts-mcp",
        agents=FakeRegistry(agents, [SERVER]).agents,
        require_agent=require_agent,
        audit=fakes.audit,
        telemetry=fakes.telemetry,
        clock=fakes.clock,
        ids=fakes.ids,
    )


# ------------------------------------------------------------------ the verifier


async def test_a_users_token_presented_by_a_registered_agent_opens_the_door(
    idp: FakeIdentityProvider,
) -> None:
    fakes = Fakes()
    token = idp.user_token(audience=API, subject="u-7", client_id=AGENT_CLIENT)

    access = await door(idp, fakes).verify_token(token)

    assert access is not None
    assert (access.client_id, access.subject) == (AGENT_CLIENT, "u-7")
    assert access.claims == {"iss": idp.issuer}
    assert fakes.audit.records == []


async def test_an_agents_own_token_opens_the_door_so_it_can_list_tools(
    idp: FakeIdentityProvider,
) -> None:
    # The same token is refused for a tool call: the door only asks whether it is valid.
    own = idp.app_token(audience=API, client_id=AGENT_CLIENT)
    assert await door(idp, Fakes()).verify_token(own) is not None
    with pytest.raises(PolicyDenied) as refused:
        await identity(idp).verify(own)
    assert refused.value.reason_code == "service_token_refused"


@pytest.mark.parametrize(
    ("agents", "client", "reason"),
    [
        ((AGENT,), OTHER_CLIENT, "agent_unregistered"),
        ((), AGENT_CLIENT, "agent_unregistered"),
        (
            (AgentEntry(id="a", owner="o", version="1", client_id=AGENT_CLIENT),),
            AGENT_CLIENT,
            "server_not_allowed",
        ),
        (
            (
                AgentEntry(
                    id="a",
                    owner="o",
                    version="1",
                    mcp_servers=("accounts",),
                    client_id=AGENT_CLIENT,
                    status=EntryStatus.DISABLED,
                ),
            ),
            AGENT_CLIENT,
            "agent_disabled",
        ),
    ],
)
async def test_a_valid_token_from_anything_but_a_listed_agent_is_refused_and_recorded(
    idp: FakeIdentityProvider, agents: tuple[AgentEntry, ...], client: str, reason: str
) -> None:
    fakes = Fakes()
    token = idp.user_token(audience=API, subject="u-7", client_id=client)

    assert await door(idp, fakes, agents=agents).verify_token(token) is None

    (record,) = fakes.audit.records
    assert (record.event, record.outcome) == ("request.authenticate", AuditOutcome.DENIED)
    assert record.attributes["reason_code"] == reason
    assert (record.subject, record.application) == ("unknown", "accounts-mcp")


async def test_a_token_that_names_no_application_is_refused(idp: FakeIdentityProvider) -> None:
    fakes = Fakes()
    token = idp.user_token(audience=API, subject="u-7", azp=None)
    assert await door(idp, fakes).verify_token(token) is None
    assert fakes.audit.records[0].attributes["reason_code"] == "agent_unidentified"


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda idp: "not-a-token", "credential_invalid"),
        (lambda idp: idp.user_token(audience="another-api", subject="u-7"), "credential_audience"),
        (
            lambda idp: idp.user_token(audience=API, subject="u-7", expires_in=-3600),
            "credential_expired",
        ),
    ],
)
async def test_a_token_that_is_not_valid_here_is_refused_and_never_written_down(
    idp: FakeIdentityProvider, make: Any, reason: str
) -> None:
    fakes = Fakes()
    token = make(idp)

    assert await door(idp, fakes).verify_token(token) is None

    (record,) = fakes.audit.records
    assert record.attributes["reason_code"] == reason
    assert token not in str(record.to_dict())
    assert ("request.authenticate", {"outcome": "denied"}) in fakes.telemetry.events


async def test_on_a_developers_machine_any_valid_token_opens_the_door(
    idp: FakeIdentityProvider,
) -> None:
    token = idp.user_token(audience=API, subject="u-7", client_id=OTHER_CLIENT)
    access = await door(idp, Fakes(), require_agent=False).verify_token(token)
    assert access is not None
    assert access.client_id == OTHER_CLIENT


async def test_a_refusal_that_cannot_be_recorded_fails_closed(idp: FakeIdentityProvider) -> None:
    fakes = Fakes()
    fakes.audit.fail_with = OSError("disk full")
    with pytest.raises(IntegrityError):
        await door(idp, fakes).verify_token("not-a-token")


# ----------------------------------------------------------------- the container


async def test_a_server_whose_callers_show_issued_tokens_checks_them_at_the_door(
    idp: FakeIdentityProvider,
) -> None:
    fakes = Fakes(identity=identity(idp), registry=FakeRegistry([AGENT], [SERVER]))
    async with fakes.container(ServiceConfig.for_testing(deployment_env=DeploymentEnv.PROD)) as s:
        kwargs = s.mcp_server_kwargs("accounts", application="accounts-mcp")
        server: MCPServer[Any] = MCPServer("accounts-mcp", **kwargs)
        # Deployed, a valid token must also come from a listed agent.
        stranger = idp.user_token(audience=API, subject="u-7", client_id=OTHER_CLIENT)
        listed = idp.user_token(audience=API, subject="u-7", client_id=AGENT_CLIENT)
        assert await kwargs["token_verifier"].verify_token(stranger) is None
        assert await kwargs["token_verifier"].verify_token(listed) is not None

    assert set(kwargs) == {"middleware", "auth", "token_verifier"}
    assert isinstance(kwargs["token_verifier"], DoorTokenVerifier)
    assert str(kwargs["auth"].issuer_url) == idp.issuer
    assert str(kwargs["auth"].resource_server_url) == SERVER.url
    assert server.settings.auth is kwargs["auth"]


async def test_the_development_identity_has_no_door_and_cannot_be_deployed() -> None:
    clock = FrozenClock()
    fakes = Fakes(
        clock=clock,
        identity=StaticIdentityVerifier(StaticIdentityOptions(), clock),
        registry=FakeRegistry([AGENT], [SERVER]),
    )
    async with fakes.container() as services:
        # A developer may call a tool with no token at all.
        assert set(services.mcp_server_kwargs("accounts")) == {"middleware"}
    async with fakes.container(ServiceConfig.for_testing(deployment_env=DeploymentEnv.DEV)) as s:
        with pytest.raises(ConfigurationError, match="cannot check tokens at its door"):
            s.mcp_server_kwargs("accounts")


# ---------------------------------------------------------------- the agent side


class _Recording(InProcessMcpConnector):
    """Remembers the token each connection was opened with."""

    def __init__(self, servers: dict[str, MCPServer[Any]], *, refuse: bool = False) -> None:
        super().__init__(servers)
        self.tokens: list[SecretStr | None] = []
        self._refuse = refuse

    @asynccontextmanager
    async def connect(self, server: ServerEntry, token: SecretStr | None) -> AsyncIterator[Client]:
        self.tokens.append(token)
        if self._refuse:
            request = httpx2.Request("POST", server.url)
            raise httpx2.HTTPStatusError(
                "401", request=request, response=httpx2.Response(401, request=request)
            )
        async with super().connect(server, token) as client:
            yield client


def _agent(connector: _Recording) -> Fakes:
    clock = FrozenClock()
    return Fakes(
        clock=clock,
        identity=StaticIdentityVerifier(StaticIdentityOptions(tenant="acme"), clock),
        registry=FakeRegistry(
            [AgentEntry(id="accounts-agent", owner="o", version="1", mcp_servers=("accounts",))],
            [SERVER],
        ),
        mcp_connector=connector,
    )


def _server() -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer("accounts-mcp")

    @server.tool(name="ping")
    def ping() -> str:
        return "pong"

    return server


async def test_an_agent_lists_tools_with_a_token_of_its_own() -> None:
    connector = _Recording({"accounts": _server()})
    fakes = _agent(connector)
    async with fakes.container() as services:
        (tool,) = await services.mcp_tools("accounts")

    (shown,) = connector.tokens
    assert shown is not None
    # It is the agent's own token for this server: it names no person.
    server_side = StaticIdentityVerifier(StaticIdentityOptions(audience=API), fakes.clock)
    principal = await server_side.verify(shown.get_secret_value())
    assert principal.kind is PrincipalKind.SERVICE
    assert tool.name == "ping"


async def test_a_server_that_refuses_the_token_is_a_denial_not_an_outage() -> None:
    connector = _Recording({"accounts": _server()}, refuse=True)
    async with _agent(connector).container() as services:
        with pytest.raises(PolicyDenied) as listing:
            await services.mcp_tools("accounts")
    assert listing.value.reason_code == "server_refused_token"
    assert not listing.value.retryable

    # The same holds for a call: retrying with the same token cannot help.
    accepting = _Recording({"accounts": _server()})
    fakes = _agent(accepting)
    async with fakes.container() as services:
        (tool,) = await services.mcp_tools("accounts")
        tool._connector = _Recording({"accounts": _server()}, refuse=True)
        context = RequestContext(
            principal=Principal(subject="u-7", tenant="acme"),
            application="accounts-agent",
            request_id="r-1",
            thread_id="th-1",
        )
        with bind_request_context(context), pytest.raises(PolicyDenied) as call:
            await tool.ainvoke({})
    assert call.value.reason_code == "server_refused_token"
    assert fakes.audit.records[-1].outcome is AuditOutcome.DENIED

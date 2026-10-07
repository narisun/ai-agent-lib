"""The whole identity path: sign-in token, exchange at the agent, verification at the MCP server.

A stand-in identity provider plays Microsoft Entra ID. Everything else is the
real code: the jwt verifier on both sides, the on-behalf-of exchange, the tool
registry, the rules policy and the MCP bindings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent_lib_core import bind_request_context
from ai_agent_lib_core.adapters import (
    ExchangingJwtIdentity,
    JwtIdentityOptions,
    JwtIdentityVerifier,
    OAuthTokenExchanger,
    RulesPolicyDecisionPoint,
    RulesPolicyOptions,
    resolve_jwt_options,
)
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AuditOutcome,
    Classification,
    DeploymentEnv,
    PolicyDenied,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
)
from ai_agent_lib_core.integrations.mcp import mcp_result_text
from ai_agent_lib_core.pipeline import frame_untrusted
from ai_agent_lib_core.testing import FakeRegistry, Fakes, FrozenClock, SequentialIds
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

AGENT_CLIENT = "22222222-2222-2222-2222-222222222222"
MCP_AUDIENCE = "api://33333333-3333-3333-3333-333333333333"
ROGUE_CLIENT = "99999999-9999-9999-9999-999999999999"

SERVER = ServerEntry(
    id="accounts",
    owner="treasury",
    url="https://accounts-mcp.internal.test/mcp",
    audience=MCP_AUDIENCE,
    tools=(ToolEntry(name="accounts.whoami", version="1", read_only=True),),
)
AGENT = AgentEntry(
    id="accounts-agent",
    owner="treasury",
    version="1",
    mcp_servers=("accounts",),
    classification_ceiling=Classification.RESTRICTED,
    client_id=AGENT_CLIENT,
)
RULES = {
    "schema": "agentlib.rules/v1",
    "rules": [
        {
            "id": "analysts-through-the-accounts-agent",
            "actions": ["tool.call"],
            "roles": ["analyst"],
            "agents": ["accounts-agent"],
            "kinds": ["user"],
        }
    ],
}


class Deployment:
    """An agent and an MCP server, both configured as they would be in production."""

    def __init__(self, tmp_path: Path, *, agents: tuple[AgentEntry, ...] = (AGENT,)) -> None:
        self.clock = FrozenClock()
        self.idp = FakeIdentityProvider(clock=self.clock)
        self.idp.register_client(AGENT_CLIENT, "agent-s3cret")
        self.idp.register_client(ROGUE_CLIENT, "rogue-s3cret")
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps(RULES), encoding="utf-8")
        self.registry = FakeRegistry(agents, [SERVER])
        self.server_fakes = Fakes(
            clock=self.clock,
            identity=JwtIdentityVerifier(
                self.settings(MCP_AUDIENCE), self.clock, transport=self.idp.transport
            ),
            registry=self.registry,
        )
        self.server_fakes.policy = RulesPolicyDecisionPoint(  # type: ignore[assignment]
            RulesPolicyOptions(path=rules), SequentialIds("decision")
        )
        self.agent_fakes = Fakes(
            clock=self.clock,
            identity=self.agent_identity(AGENT_CLIENT, "agent-s3cret"),
            registry=self.registry,
        )

    def settings(self, audience: str, **extra: Any) -> Any:
        return resolve_jwt_options(
            JwtIdentityOptions.model_validate(self.idp.options(audience, **extra))
        )

    def agent_identity(self, client_id: str, secret: str) -> ExchangingJwtIdentity:
        settings = self.settings(client_id, exchange={"client_id": client_id})
        assert settings.token_url is not None
        exchanger = OAuthTokenExchanger(
            kind="on_behalf_of",
            token_url=settings.token_url,
            client_id=client_id,
            client_secret=SecretStr(secret),
            scope=settings.exchange_scope,
            clock=self.clock,
            transport=self.idp.transport,
        )
        return ExchangingJwtIdentity(settings, self.clock, exchanger, transport=self.idp.transport)

    async def __aenter__(self) -> Deployment:
        # Deployed: the development identity could not even start here.
        deployed = ServiceConfig.for_testing(deployment_env=DeploymentEnv.PROD)
        self.server_services = self.server_fakes.container(deployed)
        await self.server_services.start()
        self.seen: list[Any] = []
        server: MCPServer[Any] = MCPServer(
            "accounts-mcp",
            **self.server_services.mcp_server_kwargs("accounts", application="accounts-mcp"),
        )

        @server.tool(name="accounts.whoami")
        def whoami() -> str:
            return "you are known"

        self.server = server
        self.agent_fakes.mcp_connector = InProcessMcpConnector({"accounts": server})
        self.agent_services = self.agent_fakes.container(
            ServiceConfig.for_testing(deployment_env=DeploymentEnv.LOCAL)
        )
        await self.agent_services.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.agent_services.aclose()
        await self.server_services.aclose()

    async def ask_as(
        self, subject: str, roles: list[str], *, mcp_roles: list[str] | None = None
    ) -> str:
        """A user signs in at the chat UI, which calls the agent, which calls the tool."""
        self.idp.assign_roles(subject, MCP_AUDIENCE, mcp_roles if mcp_roles is not None else roles)
        sign_in = self.idp.user_token(
            audience=AGENT_CLIENT, subject=subject, roles=roles, client_id="chat-ui"
        )
        context = await self.agent_services.authenticate(
            sign_in, application="accounts-agent", thread_id="th-1", request_id="r-55"
        )
        (tool,) = await self.agent_services.mcp_tools("accounts")
        with bind_request_context(context):
            return str(await tool.ainvoke({}))


async def test_the_server_sees_the_user_their_roles_and_the_agent_and_policy_decides(
    tmp_path: Path,
) -> None:
    async with Deployment(tmp_path) as world:
        answer = await world.ask_as("user-oid-7", ["analyst"])
    assert answer == frame_untrusted("accounts.whoami", "you are known")

    (record,) = world.server_fakes.audit.records
    assert record.outcome is AuditOutcome.SUCCESS
    assert record.subject == "user-oid-7"
    assert record.tenant == world.idp.tenant_id
    assert record.request_id == "r-55"
    # The token named the agent by its client ID; the registry gave it its name.
    assert record.attributes["calling_agent"] == "accounts-agent"
    assert record.attributes["policy_reason_code"] == "analysts-through-the-accounts-agent"
    assert record.attributes["policy_decision_id"] == "decision-1"

    # The token the server received was issued for the server, not the one the user signed in with.
    own, for_the_user = [r for r in world.idp.requests if r.method == "POST"]
    # The agent listed the tools in its own name, before any user was involved.
    assert b"grant_type=client_credentials" in own.content
    assert b"requested_token_use=on_behalf_of" in for_the_user.content


async def test_roles_are_those_the_user_holds_at_the_server_and_the_wrong_role_is_denied(
    tmp_path: Path,
) -> None:
    async with Deployment(tmp_path) as world:
        answer = await world.ask_as("user-oid-8", ["analyst"], mcp_roles=["intern"])
    assert "The call was denied (reason: no_matching_rule)." in answer
    (record,) = world.server_fakes.audit.records
    assert record.outcome is AuditOutcome.DENIED
    assert record.subject == "user-oid-8"


async def test_an_agent_that_is_not_registered_is_refused_whatever_the_users_role(
    tmp_path: Path,
) -> None:
    async with Deployment(tmp_path) as world:
        world.idp.assign_roles("user-oid-7", MCP_AUDIENCE, ["analyst"])
        via_rogue = world.idp.user_token(
            audience=MCP_AUDIENCE, subject="user-oid-7", roles=["analyst"], client_id=ROGUE_CLIENT
        )
        from mcp import Client

        from ai_agent_lib_core.integrations.mcp import META_AUTHORIZATION

        async with Client(world.server) as client:
            meta: Any = {META_AUTHORIZATION: via_rogue}
            result = await client.call_tool("accounts.whoami", {}, meta=meta)
            unsigned = await client.call_tool("accounts.whoami", {})
    assert mcp_result_text(result) == "The call was denied (reason: agent_unregistered)."
    # A deployed server has no development identity: no token, no call.
    assert mcp_result_text(unsigned) == "The call was denied (reason: identity_missing)."
    reasons = [r.attributes.get("identity_reason_code") for r in world.server_fakes.audit.records]
    assert reasons == [None, "credential_missing"]


async def test_the_sign_in_token_itself_is_refused_by_the_server(tmp_path: Path) -> None:
    """Forwarding the user's own token does not work: it was issued for the agent."""
    async with Deployment(tmp_path) as world:
        sign_in = world.idp.user_token(audience=AGENT_CLIENT, subject="u", roles=["analyst"])
        from mcp import Client

        from ai_agent_lib_core.integrations.mcp import META_AUTHORIZATION

        async with Client(world.server) as client:
            meta: Any = {META_AUTHORIZATION: sign_in}
            result = await client.call_tool("accounts.whoami", {}, meta=meta)
    assert result.is_error
    assert world.server_fakes.audit.records[0].attributes["identity_reason_code"] == (
        "credential_audience"
    )


async def test_the_agent_refuses_to_start_a_request_without_a_valid_sign_in(tmp_path: Path) -> None:
    async with Deployment(tmp_path) as world:
        expired = world.idp.user_token(audience=AGENT_CLIENT, subject="u", expires_in=-120)
        for token in (None, "garbage", expired):
            with pytest.raises(PolicyDenied):
                await world.agent_services.authenticate(
                    token, application="accounts-agent", thread_id="th-1"
                )

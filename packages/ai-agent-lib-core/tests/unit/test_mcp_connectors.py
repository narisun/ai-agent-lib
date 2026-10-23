"""Connectors per server entry: prefixed tool names, one connector per transport, classification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, ClassVar, cast

import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent_lib_core import bind_request_context
from ai_agent_lib_core.adapters import StaticIdentityOptions, StaticIdentityVerifier
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AgentLibError,
    CallLimits,
    Classification,
    ConfigurationError,
    DeploymentEnv,
    IntegrityError,
    Limits,
    PolicyDenied,
    Principal,
    RequestContext,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
    TransientError,
    UpstreamError,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.di.mcp import McpBindings
from ai_agent_lib_core.di.providers import ConnectorContext, McpConnectorFactory
from ai_agent_lib_core.integrations.mcp.client import (
    GovernedMcpTool,
    HttpMcpConnector,
    McpConnector,
    SupportsErrorClassification,
)
from ai_agent_lib_core.pipeline import Operations
from ai_agent_lib_core.testing import FakeRegistry, Fakes, FrozenClock
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/people-abc123"
PEOPLE = ServerEntry(
    id="people",
    owner="hr",
    url="http://localhost:8002/mcp",
    audience="people-mcp",
    tool_prefix="people___",
    tools=(ToolEntry(name="lookup", version="1.0.0", read_only=True),),
)
HOSTED = ServerEntry(
    id="hosted",
    owner="hr",
    runtime_arn=ARN,
    auth="sigv4",
    tools=(ToolEntry(name="lookup", version="1.0.0", read_only=True),),
)
AGENT = AgentEntry(
    id="people-agent",
    owner="hr",
    version="0.1.0",
    mcp_servers=("people", "hosted"),
    classification_ceiling=Classification.RESTRICTED,
)
CONTEXT = RequestContext(
    principal=Principal(subject="u-7", tenant="t-9", roles=frozenset({"analyst"})),
    application="people-agent",
    request_id="r-42",
    thread_id="th-1",
    classification_ceiling=Classification.RESTRICTED,
)


def people_server(
    ran: list[str], *, prefixed: bool = True, unprefixed: bool = True
) -> MCPServer[Any]:
    """A server that lists ``lookup`` under a gateway's prefix, and maybe without it as well."""
    server: MCPServer[Any] = MCPServer("people-mcp")
    if prefixed:

        @server.tool(name="people___lookup", description="People in one office.")
        def prefixed_lookup(office: str) -> str:
            ran.append(f"prefixed:{office}")
            return f"people in {office}"

    if unprefixed:

        @server.tool(name="lookup", description="Not the registered tool.")
        def unprefixed_lookup(office: str) -> str:
            ran.append(f"unprefixed:{office}")
            return "the wrong tool"

    return server


class Agent:
    """An agent container over an in-process server, with the connector of the test's choosing."""

    def __init__(self, connector: Any, servers: list[ServerEntry] | None = None) -> None:
        clock = FrozenClock()
        self.fakes = Fakes(
            clock=clock,
            identity=StaticIdentityVerifier(StaticIdentityOptions(), clock),
            registry=FakeRegistry([AGENT], servers or [PEOPLE], revision="rev-a"),
            mcp_connector=connector,
        )
        self.services = self.fakes.container(
            ServiceConfig.for_testing(deployment_env=DeploymentEnv.LOCAL)
        )

    async def __aenter__(self) -> Agent:
        await self.services.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.services.aclose()


# ----------------------------------------------------------------- the prefix


async def test_a_registered_tool_is_matched_under_the_servers_prefix_and_called_with_it() -> None:
    ran: list[str] = []
    connector = InProcessMcpConnector({"people": people_server(ran)})
    async with Agent(connector) as agent:
        (tool,) = await agent.services.mcp_tools("people")
        assert isinstance(tool, GovernedMcpTool)
        assert tool.name == "lookup", "the model sees the registered name"
        assert tool.description == "People in one office.", "but the prefixed tool's description"
        with bind_request_context(CONTEXT):
            result = await tool.ainvoke({"office": "lisbon"})
        assert "people in lisbon" in result
    assert ran == ["prefixed:lisbon"], "the unprefixed tool of the same name is never called"
    named = [r.attributes["tool"] for r in agent.fakes.audit.records if "tool" in r.attributes]
    assert named, "the call was audited"
    assert set(named) == {"lookup"}, (
        "the registry name, not the server's, is what is audited and checked"
    )


async def test_an_unprefixed_offering_does_not_satisfy_a_prefixed_registration() -> None:
    connector = InProcessMcpConnector({"people": people_server([], prefixed=False)})
    async with Agent(connector) as agent:
        with pytest.raises(IntegrityError, match="does not offer it as 'people___lookup'"):
            await agent.services.mcp_tools("people")


# --------------------------------------------------- one connector per kind


class _FakeConnector:
    """A connector a provider pack would build, which records that it was built and closed."""

    def __init__(self, context: ConnectorContext) -> None:
        self.context = context
        self.closed = 0

    def connect(
        self, server: ServerEntry, token: SecretStr | None
    ) -> AbstractAsyncContextManager[Client]:
        raise AssertionError("never connected in this test")

    def credential_meta(self, token: SecretStr | None) -> dict[str, str]:
        return {}

    async def aclose(self) -> None:
        self.closed += 1


class _Factories:
    """A registry of connector factories by kind, as the provider registry keeps them."""

    def __init__(self) -> None:
        self.by_kind: dict[str, McpConnectorFactory] = {}
        self.built: list[_FakeConnector] = []

    def fake(self, kind: str) -> None:
        def build(context: ConnectorContext) -> object:
            connector = _FakeConnector(context)
            self.built.append(connector)
            return connector

        self.by_kind[kind] = build

    def lookup(self, kind: str) -> McpConnectorFactory | None:
        return self.by_kind.get(kind)


class _CapturingBindings:
    """Stands in for the LangGraph bindings: remembers the connector it is handed."""

    connectors: ClassVar[list[Any]] = []

    async def mcp_tools(self, server: ServerEntry, **kwargs: Any) -> list[Any]:
        self.connectors.append(kwargs["connector"])
        return []


def bindings_with(
    factories: _Factories, *, connector: McpConnector | None = None, context: bool = True
) -> McpBindings:
    fakes = Fakes(registry=FakeRegistry([AGENT], [PEOPLE, HOSTED]))
    resources = fakes.container()._graph.resources
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
        operations=Operations(limits=Limits(tool=CallLimits(timeout_seconds=4.5))),
        connector=connector,
        bindings=cast(Any, _CapturingBindings),
        connector_factory=factories.lookup,
        connector_context=(
            (
                lambda: ConnectorContext(
                    deployment_env=DeploymentEnv.LOCAL,
                    tls_ca_bundle=None,
                    external=ServiceConfig.for_testing().external,
                    clock=fakes.clock,
                    timeout_seconds=4.5,
                    resources=resources,
                )
            )
            if context
            else None
        ),
    )


async def test_an_agentcore_entry_gets_the_registered_connector_built_once() -> None:
    factories = _Factories()
    factories.fake("agentcore")
    bindings = bindings_with(factories)
    _CapturingBindings.connectors = []

    first = bindings.mcp_connector(HOSTED)
    assert isinstance(first, _FakeConnector)
    assert bindings.mcp_connector(HOSTED) is first, "one connector per kind"
    assert len(factories.built) == 1
    assert first.context.timeout_seconds == 4.5, "the tool call limit reaches the factory"

    http = bindings.mcp_connector(PEOPLE)
    assert isinstance(http, HttpMcpConnector)
    assert bindings.mcp_connector() is http, "no entry means the HTTP connector"

    await bindings.mcp_tools("hosted")
    assert _CapturingBindings.connectors == [first], (
        "the derived URL, qualifier query included, passes the TLS check"
    )

    await bindings.aclose()
    assert first.closed == 1
    await bindings.aclose()
    assert first.closed == 2, "the connector itself must make closing twice harmless"
    await http.aclose()


async def test_an_agentcore_entry_without_a_pack_is_a_configuration_error() -> None:
    bindings = bindings_with(_Factories())
    with pytest.raises(ConfigurationError, match="transport 'agentcore'") as refused:
        bindings.mcp_connector(HOSTED)
    assert refused.value.expected == (
        "a provider pack that registers an MCP connector for 'agentcore'"
    )
    assert refused.value.fix == "install ai-agent-lib-aws"
    assert isinstance(bindings.mcp_connector(PEOPLE), HttpMcpConnector), "HTTP still works"
    await bindings.aclose()


async def test_bindings_that_stand_alone_serve_http_only() -> None:
    factories = _Factories()
    factories.fake("agentcore")
    bindings = bindings_with(factories, context=False)
    with pytest.raises(ConfigurationError, match="transport 'agentcore'"):
        bindings.mcp_connector(HOSTED)
    assert factories.built == []


async def test_an_injected_connector_serves_every_entry() -> None:
    factories = _Factories()
    factories.fake("agentcore")
    injected = InProcessMcpConnector({})
    bindings = bindings_with(factories, connector=injected)
    assert bindings.mcp_connector(HOSTED) is injected
    assert bindings.mcp_connector(PEOPLE) is injected
    assert bindings.mcp_connector() is injected
    assert factories.built == []
    await bindings.aclose()


async def test_the_container_wires_the_registered_factory_and_shares_its_resources() -> None:
    fakes = Fakes(registry=FakeRegistry([AGENT], [PEOPLE, HOSTED]))
    providers = fakes.providers()
    built: list[_FakeConnector] = []

    def build(context: ConnectorContext) -> object:
        connector = _FakeConnector(context)
        built.append(connector)
        return connector

    providers.register_mcp_connector("agentcore", build)
    with pytest.raises(ValueError, match="already registered for 'agentcore'"):
        providers.register_mcp_connector("agentcore", build)
    providers.register_mcp_connector("agentcore", build, replace=True)
    services = ServiceContainer(
        fakes.config(), providers, clock=fakes.clock, ids=fakes.ids, telemetry=fakes.telemetry
    )
    async with services:
        connector = services._mcp_bindings().mcp_connector(HOSTED)
        assert isinstance(connector, _FakeConnector)
        assert connector.context.resources is services._graph.resources, (
            "the connector shares the adapters' resources, such as a session factory"
        )
        assert connector.context.clock is fakes.clock
        assert connector.context.deployment_env is DeploymentEnv.LOCAL
        with pytest.raises(RuntimeError, match="frozen"):
            providers.register_mcp_connector("other", build)
    assert built[0].closed == 1, "closed with the container"


# ------------------------------------------------------------ classification


class _ClassifyingConnector:
    """An in-process connector that speaks a platform's error vocabulary."""

    def __init__(self, servers: dict[str, MCPServer[Any]]) -> None:
        self._inner = InProcessMcpConnector(servers)
        self.fail: BaseException | None = None
        self.classified: list[str] = []

    @asynccontextmanager
    async def connect(self, server: ServerEntry, token: SecretStr | None) -> AsyncIterator[Client]:
        if self.fail is not None:
            raise self.fail
        async with self._inner.connect(server, token) as client:
            yield client

    def credential_meta(self, token: SecretStr | None) -> dict[str, str]:
        return {}

    def classify(self, error: BaseException, *, where: str) -> AgentLibError | None:
        self.classified.append(where)
        if not isinstance(error, MCPError):
            return None
        if error.code == -32001:
            return PolicyDenied(f"{where}: the platform refused the token", reason_code="platform")
        if error.code == -32003:
            return TransientError(f"{where}: the platform is throttling")
        if error.code == -32010:
            return UpstreamError(f"{where}: the platform failed", code="RuntimeClientError")
        return None


def test_the_protocol_is_checked_at_runtime() -> None:
    assert isinstance(_ClassifyingConnector({}), SupportsErrorClassification)
    assert not isinstance(InProcessMcpConnector({}), SupportsErrorClassification)
    assert not isinstance(HttpMcpConnector(timeout_seconds=None), SupportsErrorClassification)


async def test_the_connector_is_asked_first_when_listing_tools() -> None:
    connector = _ClassifyingConnector({"people": people_server([])})
    async with Agent(connector) as agent:
        connector.fail = MCPError(code=-32003, message="slow down, says the platform")
        with pytest.raises(TransientError, match="MCP server 'people', listing tools") as throttled:
            await agent.services.mcp_tools("people")
        assert throttled.value.__cause__ is connector.fail, "a passing fault keeps its cause"
        assert "slow down" not in str(throttled.value)

        connector.fail = MCPError(code=-32001, message="who are you")
        with pytest.raises(PolicyDenied) as denied:
            await agent.services.mcp_tools("people")
        assert denied.value.reason_code == "platform"
        assert denied.value.__cause__ is None, "a denial does not carry the platform's error"

        connector.fail = MCPError(code=-32099, message="something else")
        with pytest.raises(TransientError, match="could not be reached"):
            await agent.services.mcp_tools("people")
        assert connector.classified == ["MCP server 'people', listing tools"] * 3


async def test_the_connector_is_asked_first_when_calling_a_tool() -> None:
    connector = _ClassifyingConnector({"people": people_server([])})
    async with Agent(connector) as agent:
        (tool,) = await agent.services.mcp_tools("people")
        connector.fail = MCPError(code=-32010, message="the runtime crashed with your input")
        with (
            bind_request_context(CONTEXT),
            pytest.raises(UpstreamError, match="MCP server 'people', tool 'lookup'") as failed,
        ):
            await tool.ainvoke({"office": "lisbon"})
        assert failed.value.code == "RuntimeClientError"
        assert failed.value.__cause__ is connector.fail
        assert "your input" not in str(failed.value)

        connector.fail = MCPError(code=-32001, message="who are you")
        with bind_request_context(CONTEXT), pytest.raises(PolicyDenied) as denied:
            await tool.ainvoke({"office": "lisbon"})
        assert denied.value.__cause__ is None

        connector.fail = MCPError(code=-32099, message="something else")
        with bind_request_context(CONTEXT), pytest.raises(AgentLibError) as other:
            await tool.ainvoke({"office": "lisbon"})
        assert other.value.message.endswith("the server answered with an error"), (
            "an unclassified error falls through to the agent side's own mapping"
        )
        assert connector.classified[-1] == "MCP server 'people', tool 'lookup'"

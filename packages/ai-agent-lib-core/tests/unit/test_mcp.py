"""MCP: a governed server, and its registered tools as governed agent tools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from ai_agent_lib_core import bind_request_context
from ai_agent_lib_core.adapters import StaticIdentityOptions, StaticIdentityVerifier
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AuditOutcome,
    Classification,
    ConfigurationError,
    DeploymentEnv,
    GuardrailPoint,
    IntegrityError,
    PolicyDenied,
    Principal,
    RequestContext,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
    TransientError,
    ValidationFailed,
    schema_fingerprint,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.mcp import (
    META_AUTHORIZATION,
    META_CLASSIFICATION_CEILING,
    META_REQUEST_ID,
    mcp_result_text,
    tool_fingerprints,
    verify_registration,
)
from ai_agent_lib_core.integrations.mcp.client import GovernedMcpTool
from ai_agent_lib_core.pipeline import frame_untrusted
from ai_agent_lib_core.testing import (
    FakeGuardrails,
    FakeIdentityVerifier,
    FakePolicyDecisionPoint,
    FakeRegistry,
    Fakes,
    FrozenClock,
    GuardrailAnswer,
    PolicyAnswer,
    fake_accounts_source,
)
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector

LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"title": "Region", "type": "string"},
        "min_balance": {"default": 0, "title": "Min Balance", "type": "number"},
    },
    "required": ["region"],
    "title": "lookupArguments",
}
LOOKUP = ToolEntry(
    name="accounts.lookup",
    version="1.0.0",
    classification=Classification.RESTRICTED,
    read_only=True,
    schema_sha256=schema_fingerprint(LOOKUP_SCHEMA),
)
SERVER = ServerEntry(
    id="accounts",
    owner="treasury",
    url="http://localhost:8001/mcp",
    audience="accounts-mcp",
    tools=(LOOKUP,),
)
AGENT = AgentEntry(
    id="accounts-agent",
    owner="treasury",
    version="0.1.0",
    mcp_servers=("accounts",),
    classification_ceiling=Classification.RESTRICTED,
)
USER = Principal(subject="u-7", tenant="t-9", roles=frozenset({"analyst"}))
CONTEXT = RequestContext(
    principal=USER,
    application="accounts-agent",
    request_id="r-42",
    thread_id="th-1",
    classification_ceiling=Classification.RESTRICTED,
)


def build_server(services: ServiceContainer, ran: list[str]) -> MCPServer[Any]:
    """An MCP server written the way an application team writes one."""
    server: MCPServer[Any] = MCPServer(
        "accounts-mcp",
        middleware=[services.mcp_middleware("accounts", application="accounts-mcp")],
    )
    ledger = services.data_source("ledger")

    @server.tool(name="accounts.lookup", description="Accounts in one region.")
    async def lookup(region: str, min_balance: float = 0) -> str:
        ran.append("lookup")
        result = await ledger.query(
            "accounts_by_region", {"region": region, "min_balance": min_balance}
        )
        return json.dumps({"rows": [[str(value) for value in row] for row in result.rows]})

    @server.tool(name="accounts.close")
    def close(account: str) -> str:
        ran.append("close")
        return f"closed {account}"

    @server.tool(name="accounts.broken")
    def broken() -> str:
        raise RuntimeError("database password is hunter2")

    return server


class World:
    """An agent and an MCP server in one process, each with its own container."""

    def __init__(
        self,
        *,
        server_policy: PolicyAnswer | None = None,
        agent_policy: PolicyAnswer | None = None,
        server_guardrails: FakeGuardrails | None = None,
        server_entry: ServerEntry = SERVER,
        agent_entry: AgentEntry = AGENT,
        environment: DeploymentEnv = DeploymentEnv.LOCAL,
    ) -> None:
        clock = FrozenClock()
        self.clock = clock
        self.ran: list[str] = []
        self.environment = environment
        self.server_fakes = Fakes(
            clock=clock,
            identity=StaticIdentityVerifier(StaticIdentityOptions(audience="accounts-mcp"), clock),
            registry=FakeRegistry([agent_entry], [server_entry], revision="rev-s"),
            policy=FakePolicyDecisionPoint(
                (lambda request: server_policy) if server_policy is not None else None
            ),
            guardrails=server_guardrails if server_guardrails is not None else FakeGuardrails(),
            data_sources={"ledger": fake_accounts_source("ledger")},
        )
        self.agent_fakes = Fakes(
            clock=clock,
            identity=StaticIdentityVerifier(StaticIdentityOptions(), clock),
            registry=FakeRegistry([agent_entry], [server_entry], revision="rev-a"),
            policy=FakePolicyDecisionPoint(
                (lambda request: agent_policy) if agent_policy is not None else None
            ),
        )

    async def __aenter__(self) -> World:
        self.server_services = self.server_fakes.container()
        await self.server_services.start()
        self.server = build_server(self.server_services, self.ran)
        self.connector = InProcessMcpConnector({"accounts": self.server})
        self.agent_fakes.mcp_connector = self.connector
        config = ServiceConfig.for_testing(deployment_env=self.environment)
        self.agent_services = self.agent_fakes.container(config)
        await self.agent_services.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.agent_services.aclose()
        await self.server_services.aclose()

    async def lookup_tool(self) -> GovernedMcpTool:
        (tool,) = await self.agent_services.mcp_tools("accounts")
        assert isinstance(tool, GovernedMcpTool)
        return tool


# ------------------------------------------------------------- the whole path


async def test_an_agent_calls_a_registered_tool_and_both_sides_record_it() -> None:
    async with World() as world:
        tool = await world.lookup_tool()
        with bind_request_context(CONTEXT):
            result = await tool.ainvoke({"region": "west"})

    rows = '{"rows": [["5520", "Eve", "west", "12004.55"], ["5521", "Fay", "west", "1.0"]]}'
    assert result == frame_untrusted("accounts.lookup", rows)
    assert world.ran == ["lookup"]

    # The agent asked its policy about the tool by server and name.
    (agent_asked,) = world.agent_fakes.policy.requests
    assert agent_asked.to_input()["resource"] == {
        "kind": "tool",
        "name": "accounts/accounts.lookup",
        "classification": "restricted",
        "read_only": True,
        "server": "accounts",
    }
    (agent_record,) = world.agent_fakes.audit.records
    assert agent_record.event == "tool.call"
    assert agent_record.attributes["mcp_server"] == "accounts"
    assert agent_record.attributes["tool_registry_revision"] == "rev-a"
    assert agent_record.attributes["result_framed"] is True

    # The server saw the same user, acting through the agent, and recorded both steps.
    tool_asked, data_asked = world.server_fakes.policy.requests
    assert tool_asked.principal.subject == data_asked.principal.subject == "u-7"
    assert tool_asked.principal.roles == {"analyst"}
    assert tool_asked.principal.delegation_chain == ("accounts-agent",)
    assert (tool_asked.application, tool_asked.action) == ("accounts-mcp", "tool.call")
    assert data_asked.resource.name == "ledger.accounts_by_region"
    data_record, tool_record = world.server_fakes.audit.records
    assert (data_record.event, tool_record.event) == ("data.query", "tool.call")
    for record in (data_record, tool_record):
        assert record.outcome is AuditOutcome.SUCCESS
        assert (record.subject, record.tenant, record.application) == ("u-7", "t-9", "accounts-mcp")
        # One request ID joins the agent's log to the server's.
        assert record.request_id == agent_record.request_id == "r-42"
        assert record.thread_id == "th-1"
    assert "result_framed" not in tool_record.attributes
    assert "Eve" not in str(tool_record.to_dict())


async def test_only_registered_tools_are_offered_to_the_model() -> None:
    async with World() as world:
        tools = await world.agent_services.mcp_tools("accounts")
        offered_by_server = {tool.name for tool in await world.server.list_tools()}
    assert offered_by_server == {"accounts.lookup", "accounts.close", "accounts.broken"}
    (tool,) = tools
    assert tool.name == "accounts_lookup"
    assert tool.description == "Accounts in one region."
    assert tool.args_schema == LOOKUP_SCHEMA
    assert isinstance(tool, GovernedMcpTool)
    assert tool.read_only is True


# ------------------------------------------------------------ registry checks


async def test_a_schema_that_does_not_match_its_pin_is_an_integrity_error() -> None:
    drifted = ServerEntry(
        id="accounts",
        owner="treasury",
        url="http://localhost:8001/mcp",
        audience="accounts-mcp",
        tools=(ToolEntry(name="accounts.lookup", version="1.0.0", schema_sha256="ab" * 32),),
    )
    async with World(server_entry=drifted) as world:
        with pytest.raises(IntegrityError, match="does not match its pin"):
            await world.agent_services.mcp_tools("accounts")


async def test_a_pin_is_required_everywhere_but_local_development() -> None:
    unpinned = ServerEntry(
        id="accounts",
        owner="treasury",
        url="http://localhost:8001/mcp",
        audience="accounts-mcp",
        tools=(ToolEntry(name="accounts.lookup", version="1.0.0"),),
    )
    async with World(server_entry=unpinned) as world:
        assert len(await world.agent_services.mcp_tools("accounts")) == 1
    async with World(server_entry=unpinned, environment=DeploymentEnv.PROD) as world:
        with pytest.raises(IntegrityError, match="has no schema pin"):
            await world.agent_services.mcp_tools("accounts")


async def test_a_registered_tool_the_server_does_not_offer_is_an_integrity_error() -> None:
    extra = ServerEntry(
        id="accounts",
        owner="treasury",
        url="http://localhost:8001/mcp",
        tools=(LOOKUP, ToolEntry(name="accounts.transfer", version="1.0.0")),
    )
    async with World(server_entry=extra) as world:
        with pytest.raises(IntegrityError, match="does not offer it"):
            await world.agent_services.mcp_tools("accounts")


async def test_an_unregistered_or_unreachable_server_cannot_be_used() -> None:
    async with World() as world:
        with pytest.raises(ConfigurationError, match="registered servers: accounts"):
            await world.agent_services.mcp_tools("payments")
        world.connector._servers.clear()
        with pytest.raises(TransientError, match="is not running in this process"):
            await world.agent_services.mcp_tools("accounts")


async def test_an_agent_that_is_not_listed_for_the_server_is_refused_before_connecting() -> None:
    unlisted = AgentEntry(id="accounts-agent", owner="treasury", version="0.1.0")
    async with World(agent_entry=unlisted) as world:
        tool = await world.lookup_tool()
        connections = len(world.connector.connections)
        with bind_request_context(CONTEXT), pytest.raises(PolicyDenied) as caught:
            await tool.ainvoke({"region": "west"})
        assert caught.value.reason_code == "server_not_allowed"
        assert len(world.connector.connections) == connections
    assert world.ran == []


# -------------------------------------------------------------------- refusals


async def test_the_agents_policy_can_refuse_the_call_before_the_server_is_contacted() -> None:
    async with World(agent_policy="no_matching_rule") as world:
        tool = await world.lookup_tool()
        connections = len(world.connector.connections)
        with bind_request_context(CONTEXT), pytest.raises(PolicyDenied):
            await tool.ainvoke({"region": "west"})
        assert len(world.connector.connections) == connections
    assert world.server_fakes.audit.records == []


async def test_the_servers_policy_can_refuse_and_the_agent_is_told_only_the_reason() -> None:
    async with World(server_policy="no_matching_rule") as world:
        tool = await world.lookup_tool()
        with bind_request_context(CONTEXT):
            result = await tool.ainvoke({"region": "west"})
    assert result == frame_untrusted(
        "accounts.lookup",
        "The tool reported an error: The call was denied (reason: no_matching_rule).",
    )
    assert world.ran == []
    (record,) = world.server_fakes.audit.records
    assert record.outcome is AuditOutcome.DENIED
    assert record.attributes["reason_code"] == "no_matching_rule"
    assert world.agent_fakes.audit.records[0].attributes["tool_error"] is True


async def test_arguments_are_checked_against_the_schema_before_anything_is_sent() -> None:
    async with World() as world:
        tool = await world.lookup_tool()
        connections = len(world.connector.connections)
        with bind_request_context(CONTEXT):
            for bad in ({}, {"region": 5}, {"region": "west", "min_balance": "lots"}):
                with pytest.raises(ValidationFailed, match="do not match its input schema"):
                    await tool.ainvoke(bad)
        assert len(world.connector.connections) == connections
    assert world.ran == []


async def test_a_tool_that_crashes_reports_an_error_without_its_details() -> None:
    crashing = ServerEntry(
        id="accounts",
        owner="treasury",
        url="http://localhost:8001/mcp",
        audience="accounts-mcp",
        tools=(ToolEntry(name="accounts.broken", version="1.0.0"),),
    )
    async with World(server_entry=crashing) as world:
        (tool,) = await world.agent_services.mcp_tools("accounts")
        with bind_request_context(CONTEXT):
            result = await tool.ainvoke({})
    assert "The tool reported an error" in result
    assert "hunter2" not in result
    assert world.server_fakes.audit.records[0].attributes["tool_error"] is True


# ---------------------------------------------------------- the server alone


async def direct_call(world: World, name: str, arguments: dict[str, Any], **meta: Any) -> Any:
    async with Client(world.server) as client:
        return await client.call_tool(name, arguments, meta=meta)  # type: ignore[arg-type]


async def test_a_call_without_a_token_is_the_servers_own_development_principal() -> None:
    async with World() as world:
        result = await direct_call(world, "accounts.lookup", {"region": "west"})
    assert not result.is_error
    assert world.server_fakes.audit.records[-1].subject == "dev-user"


@pytest.mark.parametrize(
    ("make_token", "reason"),
    [
        (lambda token: token[:-3] + "xyz", "credential_invalid"),
        (lambda token: "agentlib-dev.not-a-token", "credential_invalid"),
    ],
)
async def test_a_bad_token_is_refused_and_the_reason_is_recorded(
    make_token: Any, reason: str
) -> None:
    async with World() as world:
        good = await world.agent_services.identity.exchange(CONTEXT, "accounts-mcp")  # type: ignore[attr-defined]
        token = make_token(good.get_secret_value())
        result = await direct_call(
            world, "accounts.lookup", {"region": "west"}, **{META_AUTHORIZATION: token}
        )
    assert result.is_error
    assert mcp_result_text(result) == "The call was denied (reason: identity_missing)."
    assert world.ran == []
    (record,) = world.server_fakes.audit.records
    assert record.outcome is AuditOutcome.DENIED
    assert record.attributes["identity_reason_code"] == reason
    assert record.subject == "unknown"


async def test_a_token_for_another_service_or_an_expired_one_is_refused() -> None:
    async with World() as world:
        exchange = world.agent_services.identity.exchange  # type: ignore[attr-defined]
        elsewhere = (await exchange(CONTEXT, "payments-mcp")).get_secret_value()
        mine = (await exchange(CONTEXT, "accounts-mcp")).get_secret_value()
        wrong = await direct_call(
            world, "accounts.lookup", {"region": "west"}, **{META_AUTHORIZATION: elsewhere}
        )
        world.clock.advance(301)
        late = await direct_call(
            world, "accounts.lookup", {"region": "west"}, **{META_AUTHORIZATION: mine}
        )
    assert wrong.is_error
    assert late.is_error
    reasons = [r.attributes["identity_reason_code"] for r in world.server_fakes.audit.records]
    assert reasons == ["credential_audience", "credential_expired"]


async def test_a_caller_can_lower_the_classification_ceiling_but_not_raise_it() -> None:
    async with World() as world:
        lowered = await direct_call(
            world,
            "accounts.lookup",
            {"region": "west"},
            **{META_CLASSIFICATION_CEILING: "internal"},
        )
        nonsense = await direct_call(
            world, "accounts.lookup", {"region": "west"}, **{META_CLASSIFICATION_CEILING: "cosmic"}
        )
    assert mcp_result_text(lowered) == "The call was denied (reason: classification_exceeded)."
    assert not nonsense.is_error


async def test_caller_supplied_request_ids_are_used_only_if_they_are_plain() -> None:
    async with World() as world:
        await direct_call(
            world, "accounts.lookup", {"region": "west"}, **{META_REQUEST_ID: "abc-1"}
        )
        await direct_call(
            world, "accounts.lookup", {"region": "west"}, **{META_REQUEST_ID: "x" * 500}
        )
        await direct_call(
            world, "accounts.lookup", {"region": "west"}, **{META_REQUEST_ID: {"a": 1}}
        )
    request_ids = [r.request_id for r in world.server_fakes.audit.records if r.event == "tool.call"]
    assert request_ids[0] == "abc-1"
    assert all(request_id.startswith("id-") for request_id in request_ids[1:])


async def test_a_tool_that_is_not_registered_cannot_be_called_even_though_it_exists() -> None:
    async with World() as world:
        result = await direct_call(world, "accounts.close", {"account": "4411"})
    assert mcp_result_text(result) == "The call was denied (reason: tool_unregistered)."
    assert world.ran == []


async def test_the_servers_guardrails_can_stop_a_result_from_leaving() -> None:
    def answer(point: GuardrailPoint, text: str) -> GuardrailAnswer:
        return "pii.email" if point is GuardrailPoint.TOOL_RESULT else None

    guardrails = FakeGuardrails(answer)
    async with World(server_guardrails=guardrails) as world:
        result = await direct_call(world, "accounts.lookup", {"region": "west"})
    assert mcp_result_text(result) == "The call was denied (reason: guardrail_pii)."
    assert world.ran == ["lookup"]
    # The guardrail was shown the text of the result, not its wire form.
    assert guardrails.checked[-1][1].startswith('{"rows": [["5520"')


async def test_requests_that_are_not_tool_calls_pass_through() -> None:
    async with World() as world:
        async with Client(world.server) as client:
            listed = await client.list_tools()
        assert len(listed.tools) == 3
    assert world.server_fakes.audit.records == []


async def test_a_server_must_be_registered_to_be_governed() -> None:
    async with Fakes().container() as services:
        with pytest.raises(ConfigurationError, match="not in the tool registry"):
            services.mcp_middleware("accounts")


# ------------------------------------------------------------ startup checks


async def test_fingerprints_are_what_the_registry_pins() -> None:
    async with World() as world:
        fingerprints = await tool_fingerprints(world.server)
    assert fingerprints["accounts.lookup"] == LOOKUP.schema_sha256
    assert set(fingerprints) == {"accounts.lookup", "accounts.close", "accounts.broken"}


async def test_a_server_can_check_itself_against_the_registry_at_startup() -> None:
    async with World() as world:
        fingerprints = await tool_fingerprints(world.server)
        complete = ServerEntry(
            id="accounts",
            owner="treasury",
            url="http://localhost:8001/mcp",
            tools=tuple(
                ToolEntry(name=name, version="1", schema_sha256=pin)
                for name, pin in fingerprints.items()
            ),
        )
        await verify_registration(world.server, complete)

        with pytest.raises(IntegrityError) as caught:
            await verify_registration(
                world.server,
                ServerEntry(
                    id="accounts",
                    owner="treasury",
                    url="http://localhost:8001/mcp",
                    tools=(
                        ToolEntry(name="accounts.lookup", version="1", schema_sha256="cd" * 32),
                        ToolEntry(name="accounts.close", version="1"),
                        ToolEntry(name="accounts.transfer", version="1"),
                    ),
                ),
            )
    message = str(caught.value)
    assert "accounts.transfer is registered but not offered" in message
    assert "accounts.broken is offered but not registered" in message
    assert "accounts.close has no schema pin" in message
    assert "the input schema of accounts.lookup does not match its pin" in message


def test_result_text_names_content_that_is_not_text() -> None:
    wire = {"content": [{"type": "text", "text": "a"}, {"type": "image", "data": "..."}]}
    assert mcp_result_text(wire) == "a\n[image content omitted]"
    assert mcp_result_text({}) == ""


async def test_a_tool_call_that_is_not_well_formed_never_reaches_a_tool() -> None:
    async with World() as world:
        middleware = world.server_services.mcp_middleware("accounts")
        reached: list[object] = []

        async def call_next(ctx: object) -> object:
            reached.append(ctx)
            return "ran"

        for params in (
            {"name": 7, "arguments": {}},
            {"name": "accounts.close", "arguments": ["4411"]},
            {"arguments": {"account": "4411"}},
        ):
            ctx: Any = SimpleNamespace(method="tools/call", params=params, request_id="1", meta={})
            result = await middleware(ctx, call_next)  # type: ignore[arg-type]
            assert mcp_result_text(result).startswith("The call was refused")
        # A call sent as a notification, with no request ID, is refused as well.
        notification: Any = SimpleNamespace(
            method="tools/call", params={"name": "accounts.close"}, request_id=None, meta={}
        )
        await middleware(notification, call_next)  # type: ignore[arg-type]
    assert reached == []
    assert world.ran == []


async def test_outside_local_development_an_agent_must_be_able_to_obtain_tokens() -> None:
    world = World(environment=DeploymentEnv.PROD)
    # An identity provider that verifies callers but cannot exchange their tokens.
    world.agent_fakes.identity = FakeIdentityVerifier()
    async with world:
        with pytest.raises(ConfigurationError, match="exchange options"):
            await world.agent_services.mcp_tools("accounts")
    assert world.connector.connections == []


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://accounts.internal.example/mcp", True),
        ("http://localhost:8001/mcp", True),  # a server on this machine, such as a sidecar
        ("http://accounts.internal.example/mcp", False),
        ("http://10.0.3.7:8001/mcp", False),
    ],
)
async def test_a_deployed_agent_sends_tokens_only_over_tls(url: str, allowed: bool) -> None:
    entry = ServerEntry(
        id="accounts", owner="treasury", url=url, audience="accounts-mcp", tools=(LOOKUP,)
    )
    async with World(server_entry=entry, environment=DeploymentEnv.PROD) as world:
        if allowed:
            assert len(await world.agent_services.mcp_tools("accounts")) == 1
        else:
            with pytest.raises(ConfigurationError, match="must use https"):
                await world.agent_services.mcp_tools("accounts")
            assert world.connector.connections == []
    # On a developer's machine any registered address may be used.
    async with World(server_entry=entry) as world:
        assert len(await world.agent_services.mcp_tools("accounts")) == 1


async def test_the_servers_span_joins_the_agents_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    spans = InMemorySpanExporter()
    tracers = TracerProvider()
    tracers.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(trace, "get_tracer", lambda name, **_: tracers.get_tracer(name))
    async with World() as world:
        tool = await world.lookup_tool()
        with (
            tracers.get_tracer("test").start_as_current_span("agent.invoke") as request,
            bind_request_context(CONTEXT),
        ):
            await tool.ainvoke({"region": "west"})

    (served,) = [span for span in spans.get_finished_spans() if span.name == "mcp.tool_call"]
    assert served.context.trace_id == request.get_span_context().trace_id
    assert served.parent is not None
    assert served.parent.span_id == request.get_span_context().span_id
    tracers.shutdown()

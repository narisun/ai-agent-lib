"""Registries: the document format, the file provider and the registry stage."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_core.adapters import FileRegistryOptions, FileRegistrySource
from ai_agent_lib_core.adapters.strict_yaml import StrictYamlError, load_strict_yaml
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AgentSnapshot,
    Classification,
    ConfigurationError,
    EntryStatus,
    PolicyDenied,
    Principal,
    ProviderSelection,
    RequestContext,
    Section,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
    ToolSnapshot,
    schema_fingerprint,
)
from ai_agent_lib_core.di import ServiceContainer, ServiceProviders
from ai_agent_lib_core.pipeline import (
    AgentCheck,
    Pipeline,
    ToolCall,
    ToolStage,
    build_tool_pipeline,
)
from ai_agent_lib_core.testing import (
    FakeRegistry,
    Fakes,
    FrozenClock,
    InMemoryAuditSink,
    RecordingTelemetry,
    SequentialIds,
)

TOOLS = """\
schema: agentlib.registry/v1
servers:
  - id: accounts
    owner: treasury-data
    url: http://localhost:8001/mcp
    audience: accounts-mcp
    tools:
      - name: accounts.lookup
        version: 1.2.0
        schema_sha256: {pin}
        classification: restricted
        read_only: true
""".format(pin="0f" * 32)

AGENTS = """\
schema: agentlib.registry/v1
agents:
  - id: accounts-agent
    owner: treasury-data
    version: 0.3.0
    url: http://localhost:8000
    description: Answers account balance questions
    mcp_servers: [accounts]
    model_aliases: [default]
    classification_ceiling: restricted
    status: active
"""


def write(
    tmp_path: Path, tools: str | None = TOOLS, agents: str | None = AGENTS
) -> FileRegistryOptions:
    options = FileRegistryOptions(
        agents_path=tmp_path / "agents.yaml", tools_path=tmp_path / "mcp-tools.yaml"
    )
    if tools is not None:
        options.tools_path.write_text(tools, encoding="utf-8")
    if agents is not None:
        options.agents_path.write_text(agents, encoding="utf-8")
    return options


# ----------------------------------------------------------------- documents


def test_the_documents_from_the_specification_load(tmp_path: Path) -> None:
    source = FileRegistrySource(write(tmp_path))
    server = source.tools.get("accounts")
    assert server is not None
    assert server.audience == "accounts-mcp"
    assert server.tools == (
        ToolEntry(
            name="accounts.lookup",
            version="1.2.0",
            classification=Classification.RESTRICTED,
            read_only=True,
            schema_sha256="0f" * 32,
        ),
    )
    agent = source.agents.get("accounts-agent")
    assert agent is not None
    assert agent.mcp_servers == ("accounts",)
    assert agent.classification_ceiling is Classification.RESTRICTED
    assert agent.status is EntryStatus.ACTIVE


def test_the_revision_identifies_the_content(tmp_path: Path) -> None:
    first = FileRegistrySource(write(tmp_path))
    same = FileRegistrySource(write(tmp_path))
    changed = FileRegistrySource(write(tmp_path, tools=TOOLS.replace("1.2.0", "1.2.1")))
    assert first.tools.revision == same.tools.revision
    assert first.tools.revision.startswith("sha256:")
    assert changed.tools.revision != first.tools.revision
    assert changed.agents.revision == first.agents.revision


def test_a_missing_file_registers_nothing(tmp_path: Path) -> None:
    source = FileRegistrySource(write(tmp_path, tools=None, agents=None))
    assert source.tools.entries() == ()
    assert source.agents.entries() == ()
    assert source.tools.revision == source.agents.revision == "absent"


@pytest.mark.parametrize(
    ("tools", "problem"),
    [
        (TOOLS.replace("agentlib.registry/v1", "agentlib.registry/v2"), "schema"),
        (TOOLS.replace("schema: agentlib.registry/v1\n", ""), "schema"),
        (TOOLS + "    command: rm -rf /\n", "command"),
        (TOOLS + "        entrypoint: os.system\n", "entrypoint"),
        (TOOLS.replace("http://localhost:8001/mcp", "http://user:pw@localhost/mcp"), "credentials"),
        (TOOLS.replace("localhost:8001/mcp", "localhost/mcp?api_key=abc"), "credentials"),
        (TOOLS.replace("http://localhost:8001/mcp", "file:///etc/passwd"), "http or https"),
        (TOOLS.replace("0f" * 32, "abc"), "schema_sha256"),
        (TOOLS.replace("restricted", "top-secret"), "classification"),
        (TOOLS.replace("read_only: true", "read_only: sometimes"), "read_only"),
        (TOOLS.replace("id: accounts", "id: Accounts Server"), "servers.0.id"),
        (TOOLS + TOOLS.split("servers:\n")[1], "server registered more than once"),
        (
            TOOLS + "      - name: accounts.lookup\n        version: 2.0.0\n",
            "tool of server 'accounts' registered more than once",
        ),
        (TOOLS.replace("owner: treasury-data", "owner: a\n    owner: b"), "appears twice"),
        ("- just\n- a list\n", "<root>"),
        ("servers: [unclosed\n", "not valid YAML"),
    ],
)
def test_an_invalid_tool_registry_stops_startup(tmp_path: Path, tools: str, problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem) as caught:
        FileRegistrySource(write(tmp_path, tools=tools))
    assert "mcp-tools.yaml" in str(caught.value)
    assert "pw@" not in str(caught.value)
    assert "abc" not in str(caught.value)


@pytest.mark.parametrize(
    ("agents", "problem"),
    [
        (AGENTS.replace("status: active", "status: retired"), "status"),
        (AGENTS + "    import_path: agents.accounts:build\n", "import_path"),
        (AGENTS + AGENTS.split("agents:\n")[1], "agent registered more than once"),
        (
            AGENTS.replace("[accounts]", "[accounts, payments]"),
            r"not in the tool registry: \['payments'\]",
        ),
        (AGENTS.replace("    owner: treasury-data\n", ""), "owner"),
        (
            AGENTS + "  - {id: other-agent, owner: o, version: '1', client_id: accounts-agent}\n",
            "client ID is registered more than once",
        ),
    ],
)
def test_an_invalid_agent_registry_stops_startup(tmp_path: Path, agents: str, problem: str) -> None:
    with pytest.raises(ConfigurationError, match=problem) as caught:
        FileRegistrySource(write(tmp_path, agents=agents))
    assert "agents.yaml" in str(caught.value)


def test_the_format_is_chosen_by_the_file_extension(tmp_path: Path) -> None:
    as_json = tmp_path / "mcp-tools.json"
    as_json.write_text('{"schema": "agentlib.registry/v1", "servers": []}', encoding="utf-8")
    source = FileRegistrySource(
        FileRegistryOptions(agents_path=tmp_path / "none.yaml", tools_path=as_json)
    )
    assert source.tools.entries() == ()
    as_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        FileRegistrySource(FileRegistryOptions(agents_path=tmp_path / "n.yaml", tools_path=as_json))
    other = tmp_path / "mcp-tools.toml"
    other.write_text("", encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"must end in \.yaml, \.yml or \.json"):
        FileRegistrySource(FileRegistryOptions(agents_path=tmp_path / "n.yaml", tools_path=other))


def test_strict_yaml_is_plain_unambiguous_data() -> None:
    assert load_strict_yaml("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}
    with pytest.raises(StrictYamlError, match="not valid YAML"):
        load_strict_yaml("a: !!python/object/apply:os.system ['true']\n")
    with pytest.raises(StrictYamlError, match="'a' appears twice"):
        load_strict_yaml("a: 1\na: 2\n")
    with pytest.raises(StrictYamlError, match="put the name in quotes"):
        load_strict_yaml("outer:\n  on: 1\n")


# -------------------------------------------------------------- value types


def test_a_schema_fingerprint_ignores_key_order_and_spacing() -> None:
    one = {"type": "object", "properties": {"region": {"type": "string"}}, "required": ["region"]}
    two = {"required": ["region"], "properties": {"region": {"type": "string"}}, "type": "object"}
    assert schema_fingerprint(one) == schema_fingerprint(two)
    assert len(schema_fingerprint(one)) == 64
    assert schema_fingerprint(one) != schema_fingerprint({**one, "required": []})


def test_entries_and_snapshots_reject_inconsistent_data() -> None:
    with pytest.raises(ValueError, match="64 lower-case hexadecimal"):
        ToolEntry(name="t", version="1", schema_sha256="XYZ")
    tool = ToolEntry(name="t", version="1")
    with pytest.raises(ValueError, match="lists a tool more than once"):
        ServerEntry(id="s", owner="o", url="http://localhost", tools=(tool, tool))
    server = ServerEntry(id="s", owner="o", url="http://localhost")
    with pytest.raises(ValueError, match="registered more than once"):
        ToolSnapshot([server, server])
    agent = AgentEntry(id="a", owner="o", version="1")
    with pytest.raises(ValueError, match="registered more than once"):
        AgentSnapshot([agent, agent])
    assert AgentSnapshot().revision == "empty"


# ------------------------------------------------------------------ container


async def test_the_file_provider_is_the_local_default_and_may_run_anywhere(tmp_path: Path) -> None:
    options = write(tmp_path)
    spec = ServiceProviders.default().lookup(Section.REGISTRY, "file")
    assert not spec.local_only
    providers = Fakes().providers().register(Section.REGISTRY, "file", spec.factory)
    config = ServiceConfig.for_testing(
        sections={
            Section.REGISTRY: ProviderSelection(
                "file",
                {"agents_path": str(options.agents_path), "tools_path": str(options.tools_path)},
            )
        }
    )
    async with ServiceContainer(config, providers) as services:
        assert isinstance(services.registry, FileRegistrySource)
        assert services.registry.tools.get("accounts") is not None


def test_the_aws_profile_names_its_registry_provider_and_how_to_get_it() -> None:
    # A registry without the AWS pack, as on a machine where it is not installed.
    with pytest.raises(ConfigurationError, match="pip install ai-agent-lib-aws"):
        ServiceProviders().lookup(Section.REGISTRY, "s3_file")


# -------------------------------------------------------------- registry stage

LOOKUP = ToolEntry(
    name="accounts.lookup",
    version="1.2.0",
    classification=Classification.CONFIDENTIAL,
    read_only=True,
)
SERVER = ServerEntry(
    id="accounts", owner="treasury", url="http://localhost:8001/mcp", tools=(LOOKUP,)
)
AGENT = AgentEntry(
    id="accounts-agent",
    owner="treasury",
    version="0.3.0",
    mcp_servers=("accounts",),
    classification_ceiling=Classification.RESTRICTED,
)


def request_context(application: str = "accounts-agent", **overrides: object) -> RequestContext:
    values: dict[str, object] = {
        "principal": Principal(subject="u-1", tenant="t-1"),
        "application": application,
        "request_id": "r-1",
        "thread_id": "th-1",
        "classification_ceiling": Classification.RESTRICTED,
        **overrides,
    }
    return RequestContext(**values)  # type: ignore[arg-type]


class Harness:
    def __init__(
        self, registry: FakeRegistry, *, agent_check: AgentCheck = AgentCheck.SELF
    ) -> None:
        self.audit = InMemoryAuditSink()
        self.telemetry = RecordingTelemetry()
        self.seen: list[ToolCall] = []
        pipeline: Pipeline[ToolStage, ToolCall, str] = build_tool_pipeline(
            audit=self.audit,
            telemetry=self.telemetry,
            clock=FrozenClock(),
            ids=SequentialIds(),
            registry=registry,
            registry_agent_check=agent_check,
        )
        self.handler = pipeline.bind(self.terminal)

    async def terminal(self, call: ToolCall) -> str:
        self.seen.append(call)
        return "result"

    async def call(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "context": request_context(),
            "tool": "accounts.lookup",
            "server": "accounts",
            **overrides,
        }
        result: str = await self.handler(ToolCall(**values))  # type: ignore[arg-type]
        return result


async def test_a_registered_tool_passes_and_the_registry_decides_read_only() -> None:
    harness = Harness(FakeRegistry([AGENT], [SERVER], revision="rev-7"))
    assert await harness.call(read_only=False) == "result"
    assert harness.seen[0].read_only is True
    attributes = harness.audit.records[0].attributes
    assert attributes["tool_registry_revision"] == "rev-7"
    assert attributes["agent_registry_revision"] == "rev-7"
    assert attributes["mcp_server"] == "accounts"
    assert attributes["tool_version"] == "1.2.0"
    assert attributes["tool_classification"] == "confidential"
    assert attributes["read_only"] is True
    # Telemetry is labelled with the stable facts only.
    assert "tool_registry_revision" not in harness.telemetry.events[0][1]
    assert harness.telemetry.events[0][1]["mcp_server"] == "accounts"


@pytest.mark.parametrize(
    ("registry", "overrides", "reason"),
    [
        (FakeRegistry([AGENT], []), {}, "server_unregistered"),
        (FakeRegistry([AGENT], [SERVER]), {"tool": "accounts.close"}, "tool_unregistered"),
        (FakeRegistry([], [SERVER]), {}, "agent_unregistered"),
        (
            FakeRegistry(
                [
                    AgentEntry(
                        id="accounts-agent",
                        owner="o",
                        version="1",
                        mcp_servers=("accounts",),
                        status=EntryStatus.DISABLED,
                    )
                ],
                [SERVER],
            ),
            {},
            "agent_disabled",
        ),
        (
            FakeRegistry([AgentEntry(id="accounts-agent", owner="o", version="1")], [SERVER]),
            {},
            "server_not_allowed",
        ),
        (
            FakeRegistry(
                [
                    AgentEntry(
                        id="accounts-agent", owner="o", version="1", mcp_servers=("accounts",)
                    )
                ],
                [SERVER],
            ),
            {},
            "classification_exceeded",
        ),
        (
            FakeRegistry([AGENT], [SERVER]),
            {"context": request_context(classification_ceiling=Classification.INTERNAL)},
            "classification_exceeded",
        ),
        (FakeRegistry([AGENT], [SERVER]), {"context": None}, "identity_missing"),
    ],
)
async def test_the_registry_stage_only_narrows(
    registry: FakeRegistry, overrides: dict[str, object], reason: str
) -> None:
    harness = Harness(
        registry,
    )
    with pytest.raises(PolicyDenied) as caught:
        await harness.call(**overrides)
    assert caught.value.reason_code == reason
    assert harness.seen == []
    record = harness.audit.records[0]
    assert record.outcome.value == "denied"
    assert record.attributes["reason_code"] == reason


def through(*actors: str) -> RequestContext:
    """The context an MCP server builds for a caller who came through ``actors``."""
    principal = Principal(subject="u-1", tenant="t-1", delegation_chain=actors)
    return request_context("accounts-mcp", principal=principal)


BY_CLIENT_ID = AgentEntry(
    id="accounts-agent",
    owner="treasury",
    version="0.3.0",
    mcp_servers=("accounts",),
    classification_ceiling=Classification.RESTRICTED,
    client_id="11111111-2222-3333-4444-555555555555",
)


async def test_a_server_holds_the_calling_agent_to_the_registry() -> None:
    harness = Harness(
        FakeRegistry([BY_CLIENT_ID], [SERVER]), agent_check=AgentCheck.CALLER_REQUIRED
    )
    # The token names the agent by its client ID; from here on it goes by its registry ID.
    assert await harness.call(context=through("11111111-2222-3333-4444-555555555555")) == "result"
    seen = harness.seen[0].context
    assert seen is not None
    assert seen.principal.delegation_chain == ("accounts-agent",)
    assert seen.principal.actor == "accounts-agent"
    assert harness.audit.records[0].attributes["calling_agent"] == "accounts-agent"
    # A development token names the agent by its registry ID, which works the same way.
    assert await harness.call(context=through("accounts-agent")) == "result"


@pytest.mark.parametrize(
    ("agents", "actors", "reason"),
    [
        ([BY_CLIENT_ID], (), "agent_unidentified"),
        ([BY_CLIENT_ID], ("99999999-0000-0000-0000-000000000000",), "agent_unregistered"),
        ([], ("accounts-agent",), "agent_unregistered"),
        (
            [AgentEntry(id="accounts-agent", owner="o", version="1")],
            ("accounts-agent",),
            "server_not_allowed",
        ),
        (
            [
                AgentEntry(
                    id="accounts-agent",
                    owner="o",
                    version="1",
                    mcp_servers=("accounts",),
                    status=EntryStatus.DISABLED,
                )
            ],
            ("accounts-agent",),
            "agent_disabled",
        ),
        (
            [AgentEntry(id="accounts-agent", owner="o", version="1", mcp_servers=("accounts",))],
            ("accounts-agent",),
            "classification_exceeded",
        ),
        # Only the agent that presented the request counts, not one further up the chain.
        ([BY_CLIENT_ID], ("accounts-agent", "rogue-agent"), "agent_unregistered"),
    ],
)
async def test_a_deployed_server_refuses_a_call_no_registered_agent_stands_behind(
    agents: list[AgentEntry], actors: tuple[str, ...], reason: str
) -> None:
    harness = Harness(FakeRegistry(agents, [SERVER]), agent_check=AgentCheck.CALLER_REQUIRED)
    with pytest.raises(PolicyDenied) as caught:
        await harness.call(context=through(*actors))
    assert caught.value.reason_code == reason
    assert harness.seen == []


async def test_in_local_development_a_tool_can_also_be_called_with_no_agent() -> None:
    harness = Harness(FakeRegistry([], [SERVER]), agent_check=AgentCheck.CALLER)
    assert await harness.call(context=through()) == "result"
    assert "calling_agent" not in harness.audit.records[0].attributes
    # An agent that is named is still checked, and an unregistered tool is still refused.
    with pytest.raises(PolicyDenied):
        await harness.call(context=through("rogue-agent"))
    with pytest.raises(PolicyDenied):
        await harness.call(context=through(), tool="accounts.close")


def test_an_agent_is_found_by_its_client_id_or_its_registry_id() -> None:
    agents = AgentSnapshot([BY_CLIENT_ID])
    assert agents.resolve("11111111-2222-3333-4444-555555555555") is BY_CLIENT_ID
    assert agents.resolve("accounts-agent") is BY_CLIENT_ID
    assert agents.resolve("someone-else") is None
    twin = AgentEntry(id="other", owner="o", version="1", client_id=BY_CLIENT_ID.client_id)
    with pytest.raises(ValueError, match="client ID is registered more than once"):
        AgentSnapshot([BY_CLIENT_ID, twin])
    shadow = AgentEntry(id="x", owner="o", version="1", client_id="accounts-agent")
    with pytest.raises(ValueError, match="client ID is registered more than once"):
        AgentSnapshot([BY_CLIENT_ID, shadow])


async def test_a_function_tool_in_the_applications_own_code_is_not_a_registry_matter() -> None:
    harness = Harness(FakeRegistry())
    assert await harness.call(server=None, tool="add", read_only=True) == "result"
    assert harness.seen[0].read_only is True
    assert "tool_registry_revision" not in harness.audit.records[0].attributes

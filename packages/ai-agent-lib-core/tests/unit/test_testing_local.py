"""Testing a service as configured: its own .env file, local adapters, a scripted model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from ai_agent_lib_core import Principal, RequestContext, ServiceContainer, bind_request_context
from ai_agent_lib_core.adapters import JsonlAuditOptions
from ai_agent_lib_core.adapters.registry_documents import agents_document, tools_document
from ai_agent_lib_core.config import EnvSetting, Key, options_key, provider_key, render_env_file
from ai_agent_lib_core.contracts import (
    Classification,
    ConfigurationError,
    Section,
    ServerEntry,
    ToolEntry,
)
from ai_agent_lib_core.di import MODEL_PORT, ServiceProviders
from ai_agent_lib_core.integrations.langgraph import SqliteCheckpointOptions
from ai_agent_lib_core.integrations.mcp import mcp_result_text
from ai_agent_lib_core.testing import (
    TEST_AGENT,
    Fakes,
    audit_records,
    last_shown_to_model,
    load_test_config,
    scripted_model,
    scripted_providers,
)
from ai_agent_lib_core.testing.mcp import call_tool_as

RULES = """\
schema: agentlib.rules/v1
rules:
  - id: hello-uses-its-models
    actions: [model.route]
    applications: [hello-agent]
    resources: [default]
"""


def service(tmp_path: Path, *settings: EnvSetting) -> Path:
    folder = tmp_path / "hello-agent"
    (folder / "policies").mkdir(parents=True)
    (folder / "policies" / "rules.yaml").write_text(RULES, encoding="utf-8")
    text = render_env_file(
        [EnvSetting(options_key(Section.POLICY), {"path": "policies/rules.yaml"}), *settings]
    )
    (folder / ".env.example").write_text(text, encoding="utf-8")
    return folder / ".env.example"


async def test_a_service_runs_from_its_own_file_with_a_scripted_model(tmp_path: Path) -> None:
    # The file asks for a real model; a test never gets one.
    dotenv = service(
        tmp_path, EnvSetting(Key.MODEL_PROVIDER, "anthropic"), EnvSetting(Key.MODEL_ID, "a-model")
    )
    state = tmp_path / "state"
    config = load_test_config(dotenv, state_dir=state)
    assert (config.model.provider, config.model.model_id) == ("fake", "fake-model")
    audit = config.section(Section.AUDIT).parse_options(JsonlAuditOptions)
    checkpoint = config.section(Section.CHECKPOINT).parse_options(SqliteCheckpointOptions)
    assert (audit.path, checkpoint.path) == (state / "audit.jsonl", state / "checkpoints.sqlite")

    context = RequestContext(
        principal=Principal(subject="u-1", tenant="t-1"),
        application="hello-agent",
        request_id="r-1",
        thread_id="th-1",
    )
    async with ServiceContainer(config, scripted_providers("hello there")) as services:
        with pytest.raises(LookupError, match="never called"):
            last_shown_to_model(services)
        with bind_request_context(context):
            reply = await services.model().ainvoke("hi")
        # What the model was sent is there to look at afterwards.
        assert len(scripted_model(services).models[0].calls) == 1
        assert last_shown_to_model(services) == "hi"
    assert reply.content == "hello there"
    (record,) = audit_records(state)
    assert (record["event"], record["attributes"]["policy_reason_code"]) == (
        "model.call",
        "hello-uses-its-models",
    )
    assert audit_records(tmp_path / "elsewhere") == []


def test_other_stores_and_the_real_model_can_be_kept(tmp_path: Path) -> None:
    dotenv = service(
        tmp_path,
        EnvSetting(provider_key(Section.CHECKPOINT), "none"),
        EnvSetting(Key.MODEL_PROVIDER, "anthropic"),
    )
    config = load_test_config(dotenv, state_dir=tmp_path, fake_model=False)
    assert config.model.provider == "anthropic"
    assert config.section(Section.CHECKPOINT).provider == "none"
    assert dict(config.section(Section.CHECKPOINT).options) == {}


def test_the_file_must_exist_and_the_process_environment_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_test_config(tmp_path / ".env.example", state_dir=tmp_path)
    from ai_agent_lib_core.config import variable_for

    monkeypatch.setenv(variable_for(provider_key(Section.CHECKPOINT)), "none")
    config = load_test_config(service(tmp_path), state_dir=tmp_path)
    assert config.section(Section.CHECKPOINT).provider == "sqlite"


async def test_only_a_scripted_model_can_be_looked_at(tmp_path: Path) -> None:
    class AnotherModel:
        def create(self, model_id: str, **settings: Any) -> Any:
            raise NotImplementedError

    providers = ServiceProviders.default().register(
        MODEL_PORT, "fake", lambda context: AnotherModel(), replace=True
    )
    config = load_test_config(service(tmp_path), state_dir=tmp_path)
    async with ServiceContainer(config, providers) as services:
        with pytest.raises(TypeError, match="no scripted model"):
            scripted_model(services)


# ------------------------------------------------------------- an MCP server

SERVER_RULES = """\
schema: agentlib.rules/v1
rules:
  - id: staff-call-the-tools
    actions: [tool.call]
    applications: [hello-mcp]
    roles: [analyst]
    resources: ["hello/*"]
"""


def server_service(tmp_path: Path, *settings: EnvSetting) -> Path:
    """A service folder for an MCP server with one tool, registered and allowed for analysts."""
    folder = tmp_path / "hello-mcp"
    (folder / "registry").mkdir(parents=True)
    (folder / "rules.yaml").write_text(SERVER_RULES, encoding="utf-8")
    entry = ServerEntry(
        id="hello",
        owner="tests",
        url="http://127.0.0.1:1/mcp",
        audience="hello-mcp",
        tools=(
            ToolEntry(
                name="hello.echo",
                version="1",
                classification=Classification.INTERNAL,
                read_only=True,
                description="Says it back.",
            ),
        ),
    )
    (folder / "registry/tools.json").write_text(json.dumps(tools_document([entry])), "utf-8")
    (folder / "registry/agents.json").write_text(json.dumps(agents_document([])), "utf-8")
    identity = {"audience": "hello-mcp", "subject": "dev-user", "tenant": "t", "roles": ["analyst"]}
    text = render_env_file(
        [
            EnvSetting(options_key(Section.POLICY), {"path": "rules.yaml"}),
            EnvSetting(
                options_key(Section.REGISTRY),
                {"agents_path": "registry/agents.json", "tools_path": "registry/tools.json"},
            ),
            EnvSetting(options_key(Section.IDENTITY), identity),
            EnvSetting(provider_key(Section.CHECKPOINT), "none"),
            *settings,
        ]
    )
    (folder / ".env.example").write_text(text, encoding="utf-8")
    return folder / ".env.example"


def echo_server(services: ServiceContainer) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(
        "hello-mcp", **services.mcp_server_kwargs("hello", application="hello-mcp")
    )

    @server.tool(name="hello.echo")
    async def echo(text: str) -> str:
        """Say it back."""
        return text

    return server


async def test_a_server_is_called_through_a_test_agent_as_a_caller_with_roles(
    tmp_path: Path,
) -> None:
    dotenv = server_service(tmp_path)
    before = (dotenv.parent / "registry/agents.json").read_text(encoding="utf-8")
    config = load_test_config(dotenv, state_dir=tmp_path / "state", test_agent=TEST_AGENT)
    async with ServiceContainer(config) as services:
        server = echo_server(services)
        said = await call_tool_as(services, server, "hello.echo", {"text": "hi"}, roles=["analyst"])
        refused = await call_tool_as(
            services, server, "hello.echo", {"text": "hi"}, roles=["intern"]
        )
    assert (said.is_error, mcp_result_text(said)) == (False, "hi")
    assert mcp_result_text(refused) == "The call was denied (reason: no_matching_rule)."
    # The caller in the audit record is the one the test named, through the test agent.
    first, second = audit_records(tmp_path / "state")
    assert (first["subject"], first["outcome"], second["outcome"]) == ("u-1", "success", "denied")
    # The service's own registry file is not touched: the test agent lives in a copy.
    assert (dotenv.parent / "registry/agents.json").read_text(encoding="utf-8") == before


async def test_without_a_test_agent_a_server_answers_no_one(tmp_path: Path) -> None:
    config = load_test_config(server_service(tmp_path), state_dir=tmp_path / "state")
    async with ServiceContainer(config) as services:
        result = await call_tool_as(
            services, echo_server(services), "hello.echo", {"text": "hi"}, roles=["analyst"]
        )
    assert result.is_error
    assert "agent_unregistered" in mcp_result_text(result)


async def test_roles_say_who_the_caller_without_a_token_is(tmp_path: Path) -> None:
    dotenv = server_service(tmp_path)
    as_written = load_test_config(dotenv, state_dir=tmp_path)
    as_manager = load_test_config(dotenv, state_dir=tmp_path, roles=["manager", "auditor"])
    for config, expected in ((as_written, {"analyst"}), (as_manager, {"manager", "auditor"})):
        async with ServiceContainer(config) as services:
            context = await services.authenticate(None, application="hello-mcp", thread_id="t")
        assert context.principal.roles == expected


def test_arguments_that_do_not_fit_the_configured_adapters_are_refused(tmp_path: Path) -> None:
    elsewhere = server_service(
        tmp_path,
        EnvSetting(provider_key(Section.REGISTRY), "s3_file"),
        EnvSetting(provider_key(Section.IDENTITY), "jwt"),
    )
    with pytest.raises(ConfigurationError, match="only be added to a registry kept in files"):
        load_test_config(elsewhere, state_dir=tmp_path, test_agent=TEST_AGENT)
    with pytest.raises(ConfigurationError, match="only be given to the development identity"):
        load_test_config(elsewhere, state_dir=tmp_path, roles=["manager"])


async def test_a_test_caller_needs_an_identity_that_can_issue_a_token() -> None:
    async with Fakes().container() as services:
        with pytest.raises(TypeError, match="cannot issue a token for a test caller"):
            await call_tool_as(services, MCPServer("x"), "x.y", {}, roles=[])

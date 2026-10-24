"""A local agent calls an MCP server that runs elsewhere with the developer's own token."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from agentcraft2_core import bind_request_context
from agentcraft2_core.adapters import (
    StaticIdentityOptions,
    StaticIdentityVerifier,
    TokenFileExchanger,
)
from agentcraft2_core.contracts import (
    AgentEntry,
    Classification,
    ConfigurationError,
    DeploymentEnv,
    IntegrityError,
    Principal,
    RequestContext,
    ServerEntry,
    ServiceConfig,
    ToolEntry,
)
from agentcraft2_core.integrations.mcp import META_AUTHORIZATION
from agentcraft2_core.testing import FakeRegistry, Fakes, FrozenClock

TOKEN = "header.payload.signature"
REMOTE = ServerEntry(
    id="people",
    owner="hr",
    url="https://people-mcp.dev.example/mcp",
    audience="people-mcp",
    tools=(ToolEntry(name="lookup", version="1.0.0", read_only=True),),
)
SIDECAR = ServerEntry(
    id="people",
    owner="hr",
    url="http://localhost:8002/mcp",
    audience="people-mcp",
    tools=(ToolEntry(name="lookup", version="1.0.0", read_only=True),),
)
AGENT = AgentEntry(
    id="people-agent",
    owner="hr",
    version="0.1.0",
    mcp_servers=("people",),
    classification_ceiling=Classification.RESTRICTED,
)
CONTEXT = RequestContext(
    principal=Principal(subject="u-7", tenant="t-9", roles=frozenset({"analyst"})),
    application="people-agent",
    request_id="r-42",
    thread_id="th-1",
    classification_ceiling=Classification.RESTRICTED,
)


class RecordingConnector:
    """Reaches an in-process server and records the token each connection was made with."""

    def __init__(self) -> None:
        server: MCPServer[Any] = MCPServer("people-mcp")

        @server.tool(name="lookup", description="People in one office.")
        def lookup(office: str) -> str:
            return f"people in {office}"

        self._server = server
        self.tokens: list[str | None] = []

    @asynccontextmanager
    async def connect(self, server: ServerEntry, token: SecretStr | None) -> AsyncIterator[Client]:
        self.tokens.append(token.get_secret_value() if token is not None else None)
        async with Client(self._server) as client:
            yield client

    def credential_meta(self, token: SecretStr | None) -> Mapping[str, str]:
        return {META_AUTHORIZATION: token.get_secret_value()} if token is not None else {}


def _agent(
    tmp_path: Path, entry: ServerEntry, connector: RecordingConnector, env: DeploymentEnv
) -> Any:
    clock = FrozenClock()
    fakes = Fakes(
        clock=clock,
        identity=StaticIdentityVerifier(StaticIdentityOptions(), clock),
        registry=FakeRegistry([AGENT], [entry], revision="rev-a"),
        mcp_connector=connector,
    )
    return fakes.container(ServiceConfig.for_testing(deployment_env=env, state_dir=tmp_path))


def _leave_token(tmp_path: Path, text: str = TOKEN + "\n") -> Path:
    path = tmp_path / "tokens" / "people.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ the agent, end to end


async def test_a_local_agent_presents_the_developers_token_to_a_server_elsewhere(
    tmp_path: Path,
) -> None:
    _leave_token(tmp_path)
    connector = RecordingConnector()
    services = _agent(tmp_path, REMOTE, connector, DeploymentEnv.LOCAL)
    await services.start()
    try:
        (tool,) = await services.mcp_tools("people")
        with bind_request_context(CONTEXT):
            result = await tool.ainvoke({"office": "lisbon"})
    finally:
        await services.aclose()
    assert "people in lisbon" in result
    # Listing the tools and the call each presented the developer's token, not the key's.
    assert connector.tokens
    assert set(connector.tokens) == {TOKEN}


async def test_a_refreshed_token_is_used_at_once(tmp_path: Path) -> None:
    path = _leave_token(tmp_path)
    connector = RecordingConnector()
    services = _agent(tmp_path, REMOTE, connector, DeploymentEnv.LOCAL)
    await services.start()
    try:
        (tool,) = await services.mcp_tools("people")
        path.write_text("fresh.token.value\n", encoding="utf-8")
        with bind_request_context(CONTEXT):
            await tool.ainvoke({"office": "lisbon"})
    finally:
        await services.aclose()
    assert connector.tokens[-1] == "fresh.token.value"


async def test_without_a_token_file_the_error_says_where_to_put_one(tmp_path: Path) -> None:
    connector = RecordingConnector()
    services = _agent(tmp_path, REMOTE, connector, DeploymentEnv.LOCAL)
    await services.start()
    try:
        with pytest.raises(ConfigurationError) as caught:
            await services.mcp_tools("people")
    finally:
        await services.aclose()
    error = caught.value
    assert "runs elsewhere" in error.message
    assert str(tmp_path / "tokens" / "people.token") in (error.fix or "")
    assert "people-mcp" in (error.expected or "")
    assert connector.tokens == []  # nothing was sent without a real token


async def test_a_server_on_this_machine_keeps_the_development_identity(tmp_path: Path) -> None:
    connector = RecordingConnector()
    services = _agent(tmp_path, SIDECAR, connector, DeploymentEnv.LOCAL)
    await services.start()
    try:
        (tool,) = await services.mcp_tools("people")
        with bind_request_context(CONTEXT):
            await tool.ainvoke({"office": "lisbon"})
    finally:
        await services.aclose()
    assert connector.tokens
    assert TOKEN not in connector.tokens
    assert all(token is not None for token in connector.tokens)  # the development token


async def test_a_deployed_service_never_reads_a_token_file(tmp_path: Path) -> None:
    _leave_token(tmp_path)
    connector = RecordingConnector()
    services = _agent(tmp_path, REMOTE, connector, DeploymentEnv.PROD)
    await services.start()
    try:
        with pytest.raises(IntegrityError):  # the deployed rules apply: the tools must be pinned
            await services.mcp_tools("people")
    finally:
        await services.aclose()
    assert TOKEN not in connector.tokens


# ------------------------------------------------------------------------- the exchanger


async def test_the_token_is_presented_for_the_person_and_for_the_service(tmp_path: Path) -> None:
    exchanger = TokenFileExchanger(_leave_token(tmp_path), "people")
    assert (await exchanger.exchange(CONTEXT, "people-mcp")).get_secret_value() == TOKEN
    assert (await exchanger.service_token("people-mcp")).get_secret_value() == TOKEN


async def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    exchanger = TokenFileExchanger(_leave_token(tmp_path, "\n  \n"), "people")
    with pytest.raises(ConfigurationError, match="is empty"):
        await exchanger.service_token("people-mcp")


@pytest.mark.parametrize(
    "text",
    [
        "Bearer abc.def.ghi",
        "abc.def.ghi\nX-Other: header",
        "abc def",
        "abc\x00def",
    ],
)
async def test_text_that_is_not_a_token_is_refused_without_repeating_it(
    tmp_path: Path, text: str
) -> None:
    exchanger = TokenFileExchanger(_leave_token(tmp_path, text), "people")
    with pytest.raises(ConfigurationError) as caught:
        await exchanger.service_token("people-mcp")
    assert "does not hold a bearer token" in caught.value.message
    assert "abc" not in str(caught.value)
    assert "X-Other" not in str(caught.value)


async def test_a_file_that_cannot_be_read_says_why(tmp_path: Path) -> None:
    folder = tmp_path / "tokens" / "people.token"
    folder.mkdir(parents=True)  # a folder where the file should be
    exchanger = TokenFileExchanger(folder, "people")
    with pytest.raises(ConfigurationError, match="could not be read"):
        await exchanger.service_token("people-mcp")

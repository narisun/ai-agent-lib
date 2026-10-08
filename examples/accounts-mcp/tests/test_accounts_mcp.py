"""The reference MCP server, alone and with the reference agent, with no network.

Everything here is real except the model and the transport: the DuckDB data
source over the CSV file, the rules policy from this example's own rules file,
the file registries, development tokens and the JSONL audit log.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from accounts_mcp_support import agent_config, records, scripted, server_config
from langchain_core.messages import AIMessage
from mcp import Client
from mcp.server.mcpserver import MCPServer

from accounts_agent import APPLICATION as AGENT
from accounts_agent import ask
from accounts_mcp import APPLICATION, SERVER_ID, build_server
from ai_agent_lib_core import Classification, Principal, RequestContext, ServiceContainer
from ai_agent_lib_core.contracts import MASK
from ai_agent_lib_core.integrations.mcp import (
    META_AUTHORIZATION,
    mcp_result_text,
    verify_registration,
)
from ai_agent_lib_core.pipeline import frame_untrusted
from ai_agent_lib_core.testing import FakeChatModelProvider, calls_tool, scripted_model
from ai_agent_lib_core.testing.mcp import InProcessMcpConnector


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[tuple[ServiceContainer, MCPServer[Any]]]:
    """The server, started the way its command line starts it."""
    async with ServiceContainer(server_config(tmp_path)) as services:
        await services.validate()
        yield services, build_server(services)


async def call_as(
    services: ServiceContainer,
    server: MCPServer[Any],
    roles: list[str],
    tool: str,
    arguments: dict[str, Any],
) -> Any:
    """Call a tool the way an agent does for a caller with ``roles``."""
    caller = RequestContext(
        principal=Principal(subject="u-7", tenant="acme", roles=frozenset(roles)),
        application=AGENT,
        request_id="r-1",
        thread_id="th-1",
    )
    token = await services.identity.exchange(caller, APPLICATION)  # type: ignore[attr-defined]
    async with Client(server) as client:
        meta: Any = {META_AUTHORIZATION: token.get_secret_value()}
        return await client.call_tool(tool, arguments, meta=meta)


# ------------------------------------------------------------ the server alone


async def test_the_tools_match_their_pins_in_the_registry(
    running: tuple[ServiceContainer, MCPServer[Any]],
) -> None:
    services, server = running
    entry = services.registry.tools.get(SERVER_ID)
    assert entry is not None
    await verify_registration(server, entry, require_pins=True)
    assert entry.audience == APPLICATION


async def test_a_manager_sees_every_column(
    running: tuple[ServiceContainer, MCPServer[Any]],
) -> None:
    services, server = running
    result = await call_as(services, server, ["manager"], "accounts.by_region", {"region": "west"})
    table = result.structured_content
    assert table["columns"] == ["account_id", "holder", "region", "balance"]
    assert table["rows"] == [[5520, "Eve Ellis", "west", 12004.55], [5521, "Fay Ford", "west", 1.0]]
    assert table["masked_columns"] == []
    assert table["source"]["name"] == "ledger"


async def test_an_analyst_sees_balances_but_not_holders(
    running: tuple[ServiceContainer, MCPServer[Any]], tmp_path: Path
) -> None:
    services, server = running
    result = await call_as(services, server, ["analyst"], "accounts.balance", {"account_id": 4412})
    table = result.structured_content
    assert table["rows"] == [[4412, MASK, 87300.1]]
    assert table["masked_columns"] == ["holder"]

    data, tool = records(tmp_path / "server-audit.jsonl")
    assert (data["event"], tool["event"]) == ("data.query", "tool.call")
    assert data["attributes"]["policy_reason_code"] == "analysts-query-accounts"
    assert data["attributes"]["masked_columns"] == "holder"
    assert tool["attributes"]["policy_reason_code"] == "staff-call-account-tools"
    for record in (data, tool):
        assert record["subject"] == "u-7"
        assert record["application"] == APPLICATION
        assert record["attributes"]["policy_decision_id"]
        assert record["attributes"]["policy_bundle_revision"].startswith("sha256:")
        assert "Bo Banks" not in json.dumps(record)
    assert data["attributes"]["policy_decision_id"] != tool["attributes"]["policy_decision_id"]


async def test_any_other_role_is_denied_before_the_tool_runs(
    running: tuple[ServiceContainer, MCPServer[Any]], tmp_path: Path
) -> None:
    services, server = running
    result = await call_as(services, server, ["intern"], "accounts.by_region", {"region": "west"})
    assert result.is_error
    assert mcp_result_text(result) == "The call was denied (reason: no_matching_rule)."
    (record,) = records(tmp_path / "server-audit.jsonl")
    assert (record["event"], record["outcome"]) == ("tool.call", "denied")
    assert record["attributes"]["policy_decision_id"]


async def test_an_unknown_account_is_an_empty_table_not_an_error(
    running: tuple[ServiceContainer, MCPServer[Any]],
) -> None:
    services, server = running
    result = await call_as(services, server, ["manager"], "accounts.balance", {"account_id": 1})
    assert not result.is_error
    assert result.structured_content["rows"] == []


# ------------------------------------------------- the agent and the server


def asks_for_the_west() -> AIMessage:
    # The tool is registered as accounts.by_region; the helper spells it as a model sees it.
    return calls_tool("accounts.by_region", region="west")


async def ask_as(
    roles: list[str], tmp_path: Path, server: MCPServer[Any]
) -> tuple[str, FakeChatModelProvider]:
    providers = scripted(asks_for_the_west(), "There are two accounts in the west.")
    connector = InProcessMcpConnector({SERVER_ID: server})
    async with ServiceContainer(
        agent_config(tmp_path, roles), providers, mcp_connector=connector
    ) as services:
        principal = await services.identity.verify(None)
        context = RequestContext(
            principal=principal,
            application=AGENT,
            request_id="r-77",
            thread_id="th-1",
            classification_ceiling=Classification.RESTRICTED,
        )
        answer = await ask(services, context, "Which accounts are in the west?")
        return answer, scripted_model(services)


async def test_the_agent_answers_from_the_server_and_an_analyst_never_sees_a_holder(
    running: tuple[ServiceContainer, MCPServer[Any]], tmp_path: Path
) -> None:
    _, server = running
    answer, model = await ask_as(["analyst"], tmp_path, server)
    assert answer == "There are two accounts in the west."

    # What the model was shown: the table, with the holder masked, framed as data.
    shown = str(model.models[-1].calls[1][-1].content)
    assert shown.startswith('<untrusted_tool_result tool="accounts.by_region">')
    assert MASK in shown
    assert "Eve Ellis" not in shown
    assert "12004.55" in shown

    agent_log = records(tmp_path / "agent-audit.jsonl")
    assert [r["event"] for r in agent_log] == ["model.call", "tool.call", "model.call"]
    tool_call = agent_log[1]
    assert tool_call["attributes"]["mcp_server"] == SERVER_ID
    assert tool_call["attributes"]["policy_reason_code"] == "agent-calls-the-accounts-server"
    assert tool_call["attributes"]["tool_registry_revision"].startswith("sha256:")

    server_log = records(tmp_path / "server-audit.jsonl")
    assert [r["event"] for r in server_log] == ["data.query", "tool.call"]
    for record in server_log:
        # The server acted for the same person, and the two logs share the request ID.
        assert (record["subject"], record["tenant"]) == ("u-7", "acme")
        assert record["request_id"] == tool_call["request_id"] == "r-77"
        assert record["attributes"]["policy_decision_id"]


async def test_a_caller_the_server_does_not_allow_gets_a_refusal_the_model_can_explain(
    running: tuple[ServiceContainer, MCPServer[Any]], tmp_path: Path
) -> None:
    _, server = running
    _, model = await ask_as(["intern"], tmp_path, server)
    shown = str(model.models[-1].calls[1][-1].content)
    assert shown == frame_untrusted(
        "accounts.by_region",
        "The tool reported an error: The call was denied (reason: no_matching_rule).",
    )
    (denied,) = records(tmp_path / "server-audit.jsonl")
    assert denied["outcome"] == "denied"

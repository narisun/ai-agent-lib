"""Tool registry entries for MCP servers hosted on AgentCore runtimes."""

from __future__ import annotations

import pytest

from ai_agent_lib_core.adapters.registry_documents import parse_tools_document, tools_document
from ai_agent_lib_core.adapters.strict_yaml import load_strict_yaml
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    ServerEntry,
    ToolEntry,
    runtime_invocation_url,
)

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/accounts_mcp-abc123"
URL = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/"
    "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012%3Aruntime%2Faccounts_mcp-abc123"
    "/invocations?qualifier=DEFAULT"
)

PLAIN = """\
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

HOSTED = f"""\
schema: agentlib.registry/v1
servers:
  - id: accounts
    owner: treasury-data
    runtime_arn: {ARN}
    auth: sigv4
    tool_prefix: accounts___
    tools:
      - name: lookup
        version: 1.2.0
        classification: restricted
        read_only: true
"""


# --------------------------------------------------------------- the helper


def test_the_invocation_url_encodes_the_arn_as_one_path_segment() -> None:
    assert runtime_invocation_url(ARN) == URL
    assert runtime_invocation_url(ARN, "blue").endswith("/invocations?qualifier=blue")
    gov = "arn:aws-us-gov:bedrock-agentcore:us-gov-west-1:123456789012:runtime/r-1"
    assert runtime_invocation_url(gov).startswith(
        "https://bedrock-agentcore.us-gov-west-1.amazonaws.com/runtimes/arn%3Aaws-us-gov%3A"
    )


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:bedrock:us-east-1:123456789012:runtime/r-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/m-1",
        "arn:aws:bedrock-agentcore:us-east-1:12345:runtime/r-1",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r 1",
        "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations",
        "",
    ],
)
def test_anything_but_a_runtime_arn_is_refused(arn: str) -> None:
    with pytest.raises(ValueError, match="runtime_arn must be shaped"):
        runtime_invocation_url(arn)


@pytest.mark.parametrize("qualifier", ["", "a b", "x/y", "q?x=1", "a" * 65])
def test_a_qualifier_is_a_plain_name(qualifier: str) -> None:
    with pytest.raises(ValueError, match="qualifier must be a plain name"):
        runtime_invocation_url(ARN, qualifier)


# ----------------------------------------------------------------- the entry


def test_an_entry_with_a_runtime_arn_derives_its_url() -> None:
    entry = ServerEntry(id="accounts", owner="treasury", runtime_arn=ARN)
    assert entry.url == URL
    assert entry.transport == "agentcore"
    assert entry.auth == "bearer"
    assert entry.qualifier == "DEFAULT"
    assert entry.tool_prefix == ""
    # Giving the derived URL as well is harmless: it is the same one address.
    assert ServerEntry(id="accounts", owner="treasury", url=URL, runtime_arn=ARN).url == URL


def test_a_plain_entry_is_unchanged() -> None:
    entry = ServerEntry(id="accounts", owner="treasury", url="http://localhost:8001/mcp")
    assert entry.transport == "http"
    assert entry.runtime_arn is None
    with pytest.raises(ValueError, match="url"):
        ServerEntry(id="accounts", owner="treasury")


def test_an_entry_has_one_address_not_two() -> None:
    with pytest.raises(ValueError, match="one address, not two"):
        ServerEntry(
            id="accounts", owner="treasury", url="https://other.example/mcp", runtime_arn=ARN
        )


def test_sigv4_needs_a_runtime_to_sign_for() -> None:
    with pytest.raises(ValueError, match="auth sigv4 needs a runtime_arn"):
        ServerEntry(id="accounts", owner="treasury", url="https://mcp.example/mcp", auth="sigv4")
    entry = ServerEntry(id="accounts", owner="treasury", runtime_arn=ARN, auth="sigv4")
    assert entry.auth == "sigv4"
    with pytest.raises(ValueError, match="auth must be bearer or sigv4"):
        ServerEntry(id="accounts", owner="treasury", runtime_arn=ARN, auth="basic")  # type: ignore[arg-type]


@pytest.mark.parametrize("prefix", ["with space", "tab\t", "a" * 65, "x/y"])
def test_a_tool_prefix_is_a_short_name_without_whitespace(prefix: str) -> None:
    with pytest.raises(ValueError, match="tool_prefix must be a short name"):
        ServerEntry(id="accounts", owner="treasury", runtime_arn=ARN, tool_prefix=prefix)
    assert (
        ServerEntry(
            id="accounts", owner="treasury", runtime_arn=ARN, tool_prefix="accounts___"
        ).tool_prefix
        == "accounts___"
    )


# -------------------------------------------------------------- the document


def test_a_hosted_server_parses_and_derives_its_url() -> None:
    (server,) = parse_tools_document(load_strict_yaml(HOSTED))
    assert server.url == URL
    assert server.runtime_arn == ARN
    assert server.transport == "agentcore"
    assert server.auth == "sigv4"
    assert server.tool_prefix == "accounts___"
    assert server.tools == (
        ToolEntry(
            name="lookup",
            version="1.2.0",
            classification=server.tools[0].classification,
            read_only=True,
        ),
    )


def test_a_hosted_server_may_name_its_qualifier() -> None:
    (server,) = parse_tools_document(
        load_strict_yaml(HOSTED.replace("auth: sigv4", "auth: sigv4\n    qualifier: blue"))
    )
    assert server.qualifier == "blue"
    assert server.url.endswith("?qualifier=blue")


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("    auth: sigv4", f"    auth: sigv4\n    url: {URL}"), "one address, not two"),
        (
            (f"    runtime_arn: {ARN}\n", "    url: https://mcp.example/mcp\n"),
            "auth sigv4 needs a runtime_arn",
        ),
        ((f"    runtime_arn: {ARN}\n    auth: sigv4\n", ""), "needs a url, or a runtime_arn"),
        (
            (f"runtime_arn: {ARN}", "runtime_arn: arn:aws:lambda:us-east-1:1:function:f"),
            "runtime_arn",
        ),
        (("    auth: sigv4", "    auth: basic"), "auth"),
        (("tool_prefix: accounts___", "tool_prefix: 'with space'"), "tool_prefix"),
        (("    auth: sigv4", "    auth: sigv4\n    region: us-east-1"), "region"),
    ],
)
def test_an_invalid_hosted_server_is_refused(change: tuple[str, str], problem: str) -> None:
    before, after = change
    assert before in HOSTED
    with pytest.raises(ConfigurationError, match=problem):
        parse_tools_document(load_strict_yaml(HOSTED.replace(before, after)))


def test_a_document_written_before_the_fields_existed_reads_back_as_it_was() -> None:
    raw = load_strict_yaml(PLAIN)
    servers = parse_tools_document(raw)
    assert tools_document(servers) == raw, "defaults are left out, so the document is unchanged"


def test_a_hosted_server_round_trips_through_its_document() -> None:
    raw = load_strict_yaml(HOSTED)
    servers = parse_tools_document(raw)
    written = tools_document(servers)
    assert written == raw, "the url is derived, so only the runtime_arn is written"
    assert parse_tools_document(written) == servers
    (server,) = servers
    with_qualifier = ServerEntry(
        id=server.id, owner=server.owner, runtime_arn=ARN, qualifier="blue", tools=server.tools
    )
    written_servers = tools_document([with_qualifier])["servers"]
    assert isinstance(written_servers, list)
    (document,) = written_servers
    assert isinstance(document, dict)
    assert document["qualifier"] == "blue"
    assert "url" not in document
    assert "auth" not in document, "the default auth is left out"
    assert "tool_prefix" not in document, "an empty prefix is left out"
    assert parse_tools_document(tools_document([with_qualifier])) == (with_qualifier,)

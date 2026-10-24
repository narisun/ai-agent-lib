"""The calls to MCP servers follow the standard proxy and CA variables."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from agentcraft2_core.adapters.http_support import proxied_client_options
from agentcraft2_core.config import ConfigResolver, MappingConfigSource
from agentcraft2_core.contracts import ConfigurationError
from agentcraft2_core.integrations.mcp.client import HttpMcpConnector

PROXY = "http://proxy.corp.example:3128"


def _proxy_of(client: Any, url: str) -> str | None:
    """Return the proxy a request to ``url`` would go through, or ``None`` for a direct call."""
    transport = client._transport_for_url(type(client.base_url)(url))
    proxy = getattr(getattr(transport, "_pool", None), "_proxy_url", None)
    return None if proxy is None else f"{proxy.scheme.decode()}://{proxy.host.decode()}"


def test_without_a_proxy_nothing_is_added() -> None:
    assert proxied_client_options(None, "example.com") == {}


def test_a_proxy_serves_every_host_except_this_machine_and_the_hosts_listed() -> None:
    client = httpx.AsyncClient(
        trust_env=False, **proxied_client_options(PROXY, "internal.example, .corp.example")
    )
    via = "http://proxy.corp.example"
    assert _proxy_of(client, "https://mcp.example.com/mcp") == via
    assert _proxy_of(client, "http://127.0.0.1:8100/mcp") is None
    assert _proxy_of(client, "http://localhost:8100/mcp") is None
    assert _proxy_of(client, "https://internal.example/mcp") is None
    assert _proxy_of(client, "https://api.internal.example/mcp") is None
    assert _proxy_of(client, "https://tools.corp.example/mcp") is None


def test_a_star_in_the_list_turns_the_proxy_off_for_every_host() -> None:
    client = httpx.AsyncClient(trust_env=False, **proxied_client_options(PROXY, "*"))
    assert _proxy_of(client, "https://mcp.example.com/mcp") is None


def test_the_http_connector_sends_remote_servers_through_the_proxy() -> None:
    connector = HttpMcpConnector(timeout_seconds=5, proxy=PROXY, no_proxy="internal.example")
    assert _proxy_of(connector._http, "https://mcp.example.com/mcp") == "http://proxy.corp.example"
    assert _proxy_of(connector._http, "https://tools.internal.example/mcp") is None
    assert _proxy_of(connector._http, "http://127.0.0.1:8100/mcp") is None


def test_the_http_connector_goes_direct_without_a_proxy() -> None:
    connector = HttpMcpConnector(timeout_seconds=5)
    assert _proxy_of(connector._http, "https://mcp.example.com/mcp") is None


def test_a_ca_file_that_is_not_one_stops_the_connector_with_the_reason(tmp_path: Path) -> None:
    broken = tmp_path / "ca.pem"
    broken.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="could not be read as a CA file"):
        HttpMcpConnector(timeout_seconds=5, ca_file=broken)


def test_the_standard_variables_reach_the_external_settings(tmp_path: Path) -> None:
    config = ConfigResolver(
        MappingConfigSource(
            {
                "SSL_CERT_FILE": "corp-ca.pem",
                "HTTPS_PROXY": PROXY,
                "NO_PROXY": "internal.example",
            }
        ),
        base_dir=tmp_path,
    ).resolve()
    assert config.external.ssl_cert_file == tmp_path / "corp-ca.pem"
    assert config.external.https_proxy == PROXY
    assert config.external.no_proxy == "internal.example"

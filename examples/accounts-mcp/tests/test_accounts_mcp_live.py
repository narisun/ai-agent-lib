"""Opt-in: the agent and the server over real HTTP, with a real OPA server deciding.

It needs the ``opa`` binary on ``PATH``::

    uv run pytest -m integration examples/accounts-mcp/tests/test_accounts_mcp_live.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import uvicorn
from accounts_mcp_support import HERE, agent_config, records, scripted, server_config

from accounts_agent import APPLICATION as AGENT
from accounts_agent import ask
from accounts_mcp import build_server
from ai_agent_lib_core import Classification, RequestContext, ServiceContainer
from ai_agent_lib_core.adapters import (
    ExchangingJwtIdentity,
    JwtIdentityOptions,
    JwtIdentityVerifier,
    OAuthTokenExchanger,
    SystemClock,
    resolve_jwt_options,
)
from ai_agent_lib_core.config import ConfigResolver, MappingConfigSource
from ai_agent_lib_core.contracts import MASK, SecretsProvider, Section, ServiceConfig
from ai_agent_lib_core.di import BuildContext, ServiceProviders
from ai_agent_lib_core.integrations.http import ServiceLifecycle, add_health_routes, serve
from ai_agent_lib_core.integrations.mcp import verify_registration
from ai_agent_lib_core.testing import calls_tool, last_shown_to_model
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

pytestmark = [pytest.mark.integration, pytest.mark.enable_socket]

BUNDLE = HERE.parents[1] / "policies" / "bundle"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def opa_url() -> Iterator[str]:
    """A local OPA server with the platform's Rego bundle and this server's rules."""
    binary = shutil.which("opa")
    if binary is None:
        pytest.skip("the opa binary is not on PATH")
    port = free_port()
    process = subprocess.Popen(  # noqa: S603 - a fixed command line, no shell
        [
            binary,
            "run",
            "--server",
            "--addr",
            f"127.0.0.1:{port}",
            "--log-level",
            "error",
            "-b",
            str(BUNDLE),
            str(HERE / "policies"),
        ]
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/health", trust_env=False).status_code == httpx.codes.OK:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise RuntimeError("the OPA server did not become healthy")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
async def served(tmp_path: Path, opa_url: str) -> AsyncIterator[int]:
    """The MCP server over Streamable HTTP on a free port, deciding with OPA."""
    config = server_config(
        tmp_path, EAP_POLICY_PROVIDER="opa", EAP_POLICY_OPTIONS=json.dumps({"url": opa_url})
    )
    port = free_port()
    async with ServiceContainer(config) as services:
        await services.validate()
        server = build_server(services)
        entry = services.registry.tools.get("accounts")
        assert entry is not None
        await verify_registration(server, entry)
        app = server.streamable_http_app(stateless_http=True, json_response=True)
        web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        serving = asyncio.create_task(web.serve())
        try:
            while not web.started:
                await asyncio.sleep(0.02)
            yield port
        finally:
            web.should_exit = True
            await serving


def agent_on(tmp_path: Path, port: int, roles: list[str]) -> ServiceConfig:
    """The agent's configuration, with a registry that points at the test server."""
    tools = (HERE / "registry" / "mcp-tools.yaml").read_text("utf-8")
    local = tmp_path / "mcp-tools.yaml"
    local.write_text(tools.replace("127.0.0.1:8001", f"127.0.0.1:{port}"), encoding="utf-8")
    config = agent_config(tmp_path, roles)
    selection = config.section("registry")  # type: ignore[arg-type]
    options = {**selection.options, "tools_path": str(local)}
    return config.with_section("registry", type(selection)(selection.provider, options))  # type: ignore[arg-type]


async def ask_over_http(tmp_path: Path, port: int, roles: list[str]) -> tuple[str, Any]:
    providers = scripted(calls_tool("accounts.by_region", region="west"), "Two accounts.")
    async with ServiceContainer(agent_on(tmp_path, port, roles), providers) as services:
        principal = await services.identity.verify(None)
        context = RequestContext(
            principal=principal,
            application=AGENT,
            request_id="r-live",
            thread_id="th-1",
            classification_ceiling=Classification.RESTRICTED,
        )
        answer = await ask(services, context, "Which accounts are in the west?")
        return answer, last_shown_to_model(services)


async def test_opa_allows_an_analyst_and_masks_the_holder(served: int, tmp_path: Path) -> None:
    answer, shown = await ask_over_http(tmp_path, served, ["analyst"])
    assert answer == "Two accounts."
    assert MASK in shown
    assert "Eve Ellis" not in shown
    assert "12004.55" in shown

    data, tool = records(tmp_path / "server-audit.jsonl")
    assert (data["event"], tool["event"]) == ("data.query", "tool.call")
    for record in (data, tool):
        assert record["outcome"] == "success"
        assert record["subject"] == "u-7"
        assert record["request_id"] == "r-live"
        assert record["attributes"]["policy_decision_id"]
        # Both bundles are on record: the platform's Rego and this server's rules.
        assert record["attributes"]["policy_bundle_revision"] == "bundle@0.1.0,policies@0.1.0"
    assert data["attributes"]["policy_reason_code"] == "analysts-query-accounts"
    assert data["attributes"]["masked_columns"] == "holder"


async def test_opa_shows_a_manager_every_column(served: int, tmp_path: Path) -> None:
    _, shown = await ask_over_http(tmp_path, served, ["manager"])
    assert "Eve Ellis" in shown
    assert MASK not in shown


async def test_opa_denies_any_other_role(served: int, tmp_path: Path) -> None:
    _, shown = await ask_over_http(tmp_path, served, ["intern"])
    assert "The call was denied (reason: no_matching_rule)." in shown
    (denied,) = records(tmp_path / "server-audit.jsonl")
    assert (denied["event"], denied["outcome"]) == ("tool.call", "denied")
    assert denied["attributes"]["policy_decision_id"]
    assert denied["attributes"]["policy_bundle_revision"] == "bundle@0.1.0,policies@0.1.0"


# ------------------------------------------------- with Entra-shaped tokens

AGENT_CLIENT = "22222222-2222-2222-2222-222222222222"
MCP_CLIENT = "33333333-3333-3333-3333-333333333333"
MCP_AUDIENCE = f"api://{MCP_CLIENT}"


def trusting(idp: FakeIdentityProvider, providers: ServiceProviders) -> ServiceProviders:
    """The local adapters, with the jwt provider reaching ``idp`` instead of the internet."""

    async def jwt_identity(context: BuildContext) -> JwtIdentityVerifier:
        options = context.selection.parse_options(JwtIdentityOptions)
        settings = resolve_jwt_options(options)
        if options.exchange is None or settings.token_url is None:
            return JwtIdentityVerifier(settings, context.clock, transport=idp.transport)
        secrets = cast(SecretsProvider, context.get(Section.SECRETS))
        exchanger = OAuthTokenExchanger(
            kind="on_behalf_of",
            token_url=settings.token_url,
            client_id=options.exchange.client_id,
            client_secret=await secrets.get_secret(options.exchange.client_secret),
            scope=settings.exchange_scope,
            clock=context.clock,
            transport=idp.transport,
        )
        return ExchangingJwtIdentity(settings, context.clock, exchanger, transport=idp.transport)

    return providers.register(Section.IDENTITY, "jwt", jwt_identity, replace=True)


def registries(tmp_path: Path, port: int) -> str:
    """The example registries, as they would read with Entra: client IDs and audiences."""
    tools = (HERE / "registry" / "mcp-tools.yaml").read_text("utf-8")
    tools = tools.replace("127.0.0.1:8001", f"127.0.0.1:{port}")
    tools = tools.replace("audience: accounts-mcp", f"audience: {MCP_AUDIENCE}")
    agents = (HERE / "registry" / "agents.yaml").read_text("utf-8")
    agents = agents.replace("    # client_id: 22222222", "    client_id: 22222222")
    (tmp_path / "mcp-tools.yaml").write_text(tools, encoding="utf-8")
    (tmp_path / "agents.yaml").write_text(agents, encoding="utf-8")
    return json.dumps(
        {
            "agents_path": str(tmp_path / "agents.yaml"),
            "tools_path": str(tmp_path / "mcp-tools.yaml"),
        }
    )


async def test_a_signed_in_user_reaches_the_tool_through_token_exchange_and_opa_decides(
    tmp_path: Path, opa_url: str
) -> None:
    idp = FakeIdentityProvider(clock=SystemClock())
    idp.register_client(AGENT_CLIENT, "agent-s3cret")
    port = free_port()
    registry_options = registries(tmp_path, port)

    server = server_config(
        tmp_path,
        EAP_IDENTITY_PROVIDER="jwt",
        EAP_IDENTITY_OPTIONS=json.dumps(idp.options([MCP_CLIENT, MCP_AUDIENCE])),
        EAP_POLICY_PROVIDER="opa",
        EAP_POLICY_OPTIONS=json.dumps({"url": opa_url}),
        EAP_REGISTRY_OPTIONS=registry_options,
    )
    agent = ConfigResolver(
        MappingConfigSource(
            {
                "EAP_MODEL_PROVIDER": "fake",
                "EAP_MODEL_ID": "fake-model",
                "EAP_IDENTITY_PROVIDER": "jwt",
                "EAP_IDENTITY_OPTIONS": json.dumps(
                    idp.options(
                        AGENT_CLIENT,
                        exchange={
                            "client_id": AGENT_CLIENT,
                            "client_secret": "entra_client_secret",
                        },
                    )
                ),
                "EAP_SECRET_ENTRA_CLIENT_SECRET": "agent-s3cret",
                "EAP_POLICY_OPTIONS": json.dumps(
                    {"path": str(HERE.parent / "accounts-agent/policies/agentlib/rules/data.yaml")}
                ),
                "EAP_REGISTRY_OPTIONS": registry_options,
                "EAP_AUDIT_OPTIONS": json.dumps({"path": str(tmp_path / "agent-audit.jsonl")}),
                "EAP_CHECKPOINT_OPTIONS": json.dumps({"path": str(tmp_path / "agent-cp.sqlite")}),
            }
        )
    ).resolve()

    async with ServiceContainer(server, trusting(idp, ServiceProviders.default())) as services:
        # Served as the server's own entry point serves it.
        mcp_server = build_server(services)
        lifecycle = ServiceLifecycle(services.validate)
        add_health_routes(mcp_server, lifecycle)
        app = mcp_server.streamable_http_app(stateless_http=True, json_response=True)
        stop = asyncio.Event()
        serving = asyncio.create_task(
            serve(app, lifecycle, host="127.0.0.1", port=port, stop=stop, drain_seconds=0)
        )
        try:
            while not lifecycle.ready:
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.02)
            call = calls_tool("accounts.by_region", region="west")

            async def ask_as(subject: str, roles_at_the_server: list[str]) -> str:
                # App roles are assigned per application: these are the user's at the MCP server.
                idp.assign_roles(subject, MCP_AUDIENCE, roles_at_the_server)
                sign_in = idp.user_token(
                    audience=AGENT_CLIENT, subject=subject, client_id="chat-ui"
                )
                providers = trusting(idp, scripted(call, "Done."))
                async with ServiceContainer(agent, providers) as agent_services:
                    context = await agent_services.authenticate(
                        sign_in, application=AGENT, thread_id=f"th-{subject}", request_id="r-entra"
                    )
                    await ask(agent_services, context, "Which accounts are in the west?")
                    return last_shown_to_model(agent_services)

            analyst = await ask_as("oid-analyst", ["analyst"])
            manager = await ask_as("oid-manager", ["manager"])
            intern = await ask_as("oid-intern", ["intern"])

            # The door: over HTTP nothing is answered without a valid token for this server.
            listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            accept = {"Accept": "application/json, text/event-stream"}
            async with httpx.AsyncClient(trust_env=False, headers=accept) as http:
                url = f"http://127.0.0.1:{port}/mcp"
                no_token = await http.post(url, json=listing)
                garbage = await http.post(
                    url, json=listing, headers={"Authorization": "Bearer not-a-token"}
                )
                # A token the user got for the agent is not a token for this server.
                forwarded = await http.post(
                    url,
                    json=listing,
                    headers={
                        "Authorization": "Bearer "
                        + idp.user_token(audience=AGENT_CLIENT, subject="oid-analyst")
                    },
                )
                metadata = await http.get(
                    f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp"
                )
                # A load balancer has no token: health and readiness answer without one.
                health = await http.get(f"http://127.0.0.1:{port}/healthz")
                ready = await http.get(f"http://127.0.0.1:{port}/readyz")
        finally:
            stop.set()
            await serving

    assert (health.status_code, health.json()) == (200, {"status": "ok"})
    assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
    assert not lifecycle.accepting

    assert MASK in analyst
    assert "Eve Ellis" not in analyst
    assert "Eve Ellis" in manager
    assert "The call was denied (reason: no_matching_rule)." in intern

    for refused in (no_token, garbage, forwarded):
        assert refused.status_code == httpx.codes.UNAUTHORIZED
        assert refused.headers["www-authenticate"].startswith("Bearer ")
        assert "resource_metadata=" in refused.headers["www-authenticate"]
        assert "accounts.by_region" not in refused.text
    # The standard discovery document says where tokens for this server come from.
    assert metadata.status_code == httpx.codes.OK
    assert idp.issuer in json.dumps(metadata.json()["authorization_servers"])

    everything = records(tmp_path / "server-audit.jsonl")
    # A token that was shown and refused leaves a record; a request with no token is just refused.
    assert [
        (r["outcome"], r["subject"], r["attributes"]["reason_code"])
        for r in everything
        if r["event"] == "request.authenticate"
    ] == [
        ("denied", "unknown", "credential_invalid"),
        ("denied", "unknown", "credential_audience"),
    ]
    log = [r for r in everything if r["event"] != "request.authenticate"]
    assert [(r["event"], r["outcome"], r["subject"]) for r in log] == [
        ("data.query", "success", "oid-analyst"),
        ("tool.call", "success", "oid-analyst"),
        ("data.query", "success", "oid-manager"),
        ("tool.call", "success", "oid-manager"),
        ("tool.call", "denied", "oid-intern"),
    ]
    for record in log:
        assert record["tenant"] == idp.tenant_id
        assert record["request_id"] == "r-entra"
        # The token named the agent by its client ID; the registry gave it its name.
        assert record["attributes"]["calling_agent"] == "accounts-agent"
        assert record["attributes"]["policy_decision_id"]
        assert record["attributes"]["policy_bundle_revision"] == "bundle@0.1.0,policies@0.1.0"

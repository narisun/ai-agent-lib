"""MCP client and server assembly from explicit ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from agentcraft2_core.adapters.http_support import checked_base_url
from agentcraft2_core.adapters.identity_static import StaticIdentityVerifier
from agentcraft2_core.adapters.token_file import TokenFileExchanger
from agentcraft2_core.config import STATE_FOLDER, options_key, variable_for
from agentcraft2_core.contracts import (
    AuditSink,
    Classification,
    Clock,
    ConfigurationError,
    DeploymentEnv,
    GuardrailCheck,
    IdentityVerifier,
    IdGenerator,
    PolicyDecisionPoint,
    RegistrySource,
    RequestVerifier,
    Section,
    ServerEntry,
    SupportsAsyncClose,
    Telemetry,
    TokenAuthenticator,
    TokenExchanger,
)
from agentcraft2_core.di.providers import ConnectorContext, McpConnectorFactory
from agentcraft2_core.pipeline import (
    AgentCheck,
    Operations,
    Pipeline,
    ToolCall,
    ToolStage,
    build_tool_pipeline,
)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from agentcraft2_core.integrations.langgraph import LangGraphBindings
    from agentcraft2_core.integrations.mcp import GovernedToolsMiddleware
    from agentcraft2_core.integrations.mcp.client import McpConnector

_HTTP = "http"
_THIS_MACHINE = frozenset({"localhost", "127.0.0.1", "::1"})


def _runs_elsewhere(entry: ServerEntry) -> bool:
    """Whether the server is reached over the network, not on this machine."""
    if entry.transport != _HTTP:
        return False
    return (urlsplit(entry.url).hostname or "") not in _THIS_MACHINE


def _no_factory(kind: str) -> McpConnectorFactory | None:  # noqa: ARG001 - the default knows none
    return None


class _OwnedConnectors:
    """The connectors these bindings create themselves, by transport kind, once needed.

    Kept in their own object so that the frozen bindings can create them on
    first use, and so that only a connector created here is closed here: an
    injected connector belongs to whoever injected it.
    """

    def __init__(self) -> None:
        self.by_kind: dict[str, McpConnector] = {}


@dataclass(frozen=True, slots=True)
class McpBindings:
    """Compose governed MCP clients and servers without owning service lifecycle.

    The resources these bindings do own are the connectors they create when
    none is injected: one HTTP connector, and one connector per other transport
    kind a provider pack registered a factory for; ``aclose`` releases them.
    Their timeout is the configured tool call limit, ``limits.tool.timeout_seconds``.

    Attributes:
        connector: An injected connector, which then serves every server entry.
        connector_factory: Finds the registered factory for a transport kind
            other than ``http``; by default none is known.
        connector_context: Builds what such a factory is given; ``None`` when
            the bindings stand alone, in which case only ``http`` is served.
        token_dir: The folder a developer leaves tokens in for the servers that run
            elsewhere, one file per server; used in local development only.
    """

    audit: AuditSink
    telemetry: Telemetry
    clock: Clock
    ids: IdGenerator
    identity: IdentityVerifier
    registry: RegistrySource
    policy: PolicyDecisionPoint
    guardrails: GuardrailCheck
    environment: DeploymentEnv
    identity_provider: str
    operations: Operations
    connector: McpConnector | None
    bindings: Callable[[], LangGraphBindings]
    connector_factory: Callable[[str], McpConnectorFactory | None] = _no_factory
    connector_context: Callable[[], ConnectorContext] | None = None
    token_dir: Path | None = None
    _owned: _OwnedConnectors = field(default_factory=_OwnedConnectors, init=False, repr=False)

    def mcp_connector(self, entry: ServerEntry | None = None) -> McpConnector:
        """Return the connector that reaches ``entry``: the injected one, or one per transport.

        Without an entry the HTTP connector is returned. Connectors are
        created on first use, so that a container which serves MCP tools but
        calls none never loads the agent side, and each is shared by every
        server of its kind and every call for as long as these bindings live.

        Raises:
            ConfigurationError: If the entry's transport is one no installed
                provider pack registered a connector for.
        """
        if self.connector is not None:
            return self.connector
        kind = entry.transport if entry is not None else _HTTP
        owned = self._owned.by_kind.get(kind)
        if owned is None:
            owned = self._http_connector() if kind == _HTTP else self._registered_connector(kind)
            self._owned.by_kind[kind] = owned
        return owned

    def _http_connector(self) -> McpConnector:
        # Imported here so that an agent which uses no MCP server does not load the SDK.
        from agentcraft2_core.integrations.mcp.client import HttpMcpConnector

        # The standard proxy and CA variables apply to the servers too, as they do to the model.
        external = self.connector_context().external if self.connector_context is not None else None
        return HttpMcpConnector(
            timeout_seconds=self.operations.limits.tool.timeout_seconds,
            proxy=external.https_proxy if external is not None else None,
            no_proxy=external.no_proxy if external is not None else None,
            ca_file=external.ssl_cert_file if external is not None else None,
        )

    def _registered_connector(self, kind: str) -> McpConnector:
        factory = self.connector_factory(kind)
        if factory is None or self.connector_context is None:
            raise ConfigurationError(
                f"no MCP connector reaches servers of transport {kind!r}",
                expected=f"a provider pack that registers an MCP connector for {kind!r}",
                actual="none registered",
                fix="install agentcraft2-aws" if kind == "agentcore" else "install the pack",
            )
        connector = factory(self.connector_context())
        if not callable(getattr(connector, "connect", None)) or not callable(
            getattr(connector, "credential_meta", None)
        ):
            raise ConfigurationError(
                f"the MCP connector registered for {kind!r} is not a connector",
                expected="an object with connect and credential_meta",
                actual=type(connector).__name__,
            )
        return cast("McpConnector", connector)

    async def aclose(self) -> None:
        """Close the connectors these bindings created; an injected one is left to its owner."""
        for connector in self._owned.by_kind.values():
            if isinstance(connector, SupportsAsyncClose):
                await connector.aclose()

    async def mcp_tools(self, server: str) -> list[BaseTool]:
        """Check discovery requirements and delegate governed tool construction.

        Resolve the registry entry, validate its transport, and require token
        exchange and schema pins outside local development. The public contract
        is documented on ``ServiceContainer.mcp_tools``.
        """
        entry = self._registered_server(server)
        local = self.environment is DeploymentEnv.LOCAL
        # A server that runs somewhere else is reached as the deployed ones are: over TLS, with
        # a token. Its tools are pinned when the registry pins them, and must be outside local.
        remote = local and _runs_elsewhere(entry)
        # Send only service or audience-bound exchanged tokens. Plain HTTP is for servers on
        # this machine, in local development or not. An AgentCore runtime's URL is derived
        # from its ARN and carries the qualifier as a query; the address before it is checked.
        checked_base_url(
            f"MCP server {server!r} in the tool registry",
            entry.url.split("?", 1)[0] if entry.transport == "agentcore" else entry.url,
            allow_http=False,
            hint="a token is sent only over TLS; plain http reaches only this machine",
        )
        # The development identity signs tokens with a key that is public, so nothing that runs
        # elsewhere accepts them: the developer's own token is presented instead.
        exchanger: TokenExchanger | None = (
            self.identity
            if isinstance(self.identity, TokenExchanger)
            and not (remote and isinstance(self.identity, StaticIdentityVerifier))
            else None
        )
        if exchanger is None and remote:
            exchanger = self._token_file(entry)
        if exchanger is None and not local and entry.auth != "sigv4":
            raise ConfigurationError(
                f"MCP server {server!r} cannot be called: the identity provider "
                f"{self.identity_provider!r} is not set up to obtain "
                "tokens for the services this one calls; configure its exchange options"
            )
        return await self.bindings().mcp_tools(
            entry,
            connector=self.mcp_connector(entry),
            exchanger=exchanger,
            require_pins=not local,
        )

    def _token_file(self, entry: ServerEntry) -> TokenFileExchanger:
        """Return the exchanger that presents the token a developer left for ``entry``."""
        folder = (
            self.token_dir if self.token_dir is not None else Path.cwd() / STATE_FOLDER / "tokens"
        )
        return TokenFileExchanger(folder / f"{entry.id}.token", entry.id)

    async def registered_mcp_tools(self, application: str) -> list[BaseTool]:
        """Return the governed tools of every MCP server the registry lists for an agent.

        An agent that is not in the agent registry has no servers, so it gets
        no tools. The public contract is documented on
        ``ServiceContainer.registered_mcp_tools``.
        """
        entry = self.registry.agents.get(application)
        tools: list[BaseTool] = []
        for server in entry.mcp_servers if entry is not None else ():
            tools += await self.mcp_tools(server)
        return tools

    def mcp_middleware(
        self,
        server: str,
        *,
        application: str | None = None,
        classification_ceiling: Classification = Classification.RESTRICTED,
    ) -> GovernedToolsMiddleware:
        """Assemble the incoming tool pipeline from the supplied ports.

        Caller-agent requirements depend on the deployment environment. This
        helper creates middleware but does not start the MCP server or own its
        resources. See ``ServiceContainer.mcp_middleware`` for application usage.
        """
        # Imported here so that an agent which serves no MCP tools does not load the SDK.
        from agentcraft2_core.integrations.mcp import GovernedToolsMiddleware, mcp_result_text

        entry = self._registered_server(server)
        pipeline: Pipeline[ToolStage, ToolCall, Any] = build_tool_pipeline(
            audit=self.audit,
            telemetry=self.telemetry,
            clock=self.clock,
            ids=self.ids,
            registry=self.registry,
            # The agent is somewhere else: it is whoever the caller's token names.
            registry_agent_check=(
                AgentCheck.CALLER
                if self.environment is DeploymentEnv.LOCAL
                else AgentCheck.CALLER_REQUIRED
            ),
            policy=self.policy,
            environment=self.environment.value,
            guardrails=self.guardrails,
            response_text=mcp_result_text,
            operations=self.operations,
        )
        return GovernedToolsMiddleware(
            server=entry.id,
            application=application if application is not None else entry.id,
            identity=self.identity,
            pipeline=pipeline,
            ids=self.ids,
            classification_ceiling=classification_ceiling,
        )

    def mcp_server_kwargs(
        self,
        server: str,
        *,
        application: str | None = None,
        classification_ceiling: Classification = Classification.RESTRICTED,
    ) -> dict[str, Any]:
        """Combine tool middleware with HTTP token-verification settings.

        Outside local development, the identity port must implement
        ``TokenAuthenticator``, or be a :class:`RequestVerifier` that trusts
        the hosting platform's perimeter: the platform then authenticated the
        caller before the request arrived, there is no token to check at the
        door, and the server gets the middleware alone; :meth:`perimeter_door`
        refuses requests the perimeter did not forward. Import the MCP
        integration only when needed. See ``ServiceContainer.mcp_server_kwargs``
        for arguments and usage.
        """
        from agentcraft2_core.integrations.mcp import DoorTokenVerifier, resource_server_settings

        entry = self._registered_server(server)
        name = application if application is not None else entry.id
        kwargs: dict[str, Any] = {
            "middleware": [
                self.mcp_middleware(
                    server, application=name, classification_ceiling=classification_ceiling
                )
            ]
        }
        identity = self.identity
        local = self.environment is DeploymentEnv.LOCAL
        if not isinstance(identity, TokenAuthenticator):
            if not local and not self._trusts_perimeter():
                raise ConfigurationError(
                    f"MCP server {server!r} cannot check tokens at its door: the identity "
                    f"provider {self.identity_provider!r} does not "
                    "verify tokens from an issuer, and does not trust a perimeter"
                )
            return kwargs
        kwargs["auth"] = resource_server_settings(identity.issuer, entry)
        kwargs["token_verifier"] = DoorTokenVerifier(
            identity=identity,
            server=entry,
            application=name,
            agents=self.registry.agents,
            require_agent=not local,
            audit=self.audit,
            telemetry=self.telemetry,
            clock=self.clock,
            ids=self.ids,
        )
        return kwargs

    def perimeter_door(self, app: Any) -> Any:
        """Wrap an MCP server's ASGI app in the perimeter door its identity needs, if any.

        When the identity is a :class:`RequestVerifier` that trusts the
        perimeter, a request to any path but the health routes that carries
        neither an ``Authorization`` header nor every header the identity
        requires (its ``required_headers``, when it has them) is refused with
        HTTP 401. Otherwise the app is returned unchanged. The public contract
        is documented on ``ServiceContainer.mcp_door``.
        """
        if not self._trusts_perimeter():
            return app
        from agentcraft2_core.integrations.mcp import PerimeterDoor

        required: tuple[str, ...] = tuple(getattr(self.identity, "required_headers", ()))
        return PerimeterDoor(app, required_headers=required)

    def _trusts_perimeter(self) -> bool:
        identity = self.identity
        return isinstance(identity, RequestVerifier) and bool(identity.trusts_perimeter)

    def _registered_server(self, server: str) -> ServerEntry:
        entry = self.registry.tools.get(server)
        if entry is None:
            known = ", ".join(e.id for e in self.registry.tools.entries()) or "none"
            raise ConfigurationError(
                f"MCP server {server!r} is not in the tool registry",
                expected=f"{server!r} among the registered servers",
                actual=f"registered servers: {known}",
                fix=(
                    f"register it (agentcraft2 new mcp does this), or check "
                    f"{variable_for(options_key(Section.REGISTRY))} points at the right registry"
                ),
            )
        return entry

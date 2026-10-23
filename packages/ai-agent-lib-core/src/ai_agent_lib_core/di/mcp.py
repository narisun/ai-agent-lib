"""MCP client and server assembly from explicit ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ai_agent_lib_core.adapters.http_support import checked_base_url
from ai_agent_lib_core.config import options_key, variable_for
from ai_agent_lib_core.contracts import (
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
    Telemetry,
    TokenAuthenticator,
    TokenExchanger,
)
from ai_agent_lib_core.pipeline import (
    AgentCheck,
    Operations,
    Pipeline,
    ToolCall,
    ToolStage,
    build_tool_pipeline,
)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from ai_agent_lib_core.integrations.langgraph import LangGraphBindings
    from ai_agent_lib_core.integrations.mcp import GovernedToolsMiddleware
    from ai_agent_lib_core.integrations.mcp.client import HttpMcpConnector, McpConnector


class _OwnedConnector:
    """The one connector these bindings create themselves, once it is needed.

    Kept in its own object so that the frozen bindings can create it on first
    use, and so that only a connector created here is closed here: an injected
    connector belongs to whoever injected it.
    """

    def __init__(self) -> None:
        self.connector: HttpMcpConnector | None = None


@dataclass(frozen=True, slots=True)
class McpBindings:
    """Compose governed MCP clients and servers without owning service lifecycle.

    The one resource these bindings do own is the HTTP connector they create
    when none is injected; ``aclose`` releases it. Its timeout is the configured
    tool call limit, ``limits.tool.timeout_seconds``.
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
    _owned: _OwnedConnector = field(default_factory=_OwnedConnector, init=False, repr=False)

    def mcp_connector(self) -> McpConnector:
        """Return the connector that reaches MCP servers: the injected one, or one HTTP connector.

        The HTTP connector is created on first use, so that a container which
        serves MCP tools but calls none never loads the agent side, and it is
        shared by every server and every call for as long as these bindings live.
        """
        if self.connector is not None:
            return self.connector
        if self._owned.connector is None:
            # Imported here so that an agent which uses no MCP server does not load the SDK.
            from ai_agent_lib_core.integrations.mcp.client import HttpMcpConnector

            self._owned.connector = HttpMcpConnector(
                timeout_seconds=self.operations.limits.tool.timeout_seconds
            )
        return self._owned.connector

    async def aclose(self) -> None:
        """Close the connector these bindings created; an injected one is left to its owner."""
        if self._owned.connector is not None:
            await self._owned.connector.aclose()

    async def mcp_tools(self, server: str) -> list[BaseTool]:
        """Check discovery requirements and delegate governed tool construction.

        Resolve the registry entry, validate its transport, and require token
        exchange and schema pins outside local development. The public contract
        is documented on ``ServiceContainer.mcp_tools``.
        """
        entry = self._registered_server(server)
        local = self.environment is DeploymentEnv.LOCAL
        # Send only service or audience-bound exchanged tokens. HTTP is allowed
        # on loopback, and on other hosts only during local development.
        checked_base_url(
            f"MCP server {server!r} in the tool registry",
            entry.url,
            allow_http=local,
            hint="outside local development a caller's token is sent only over TLS",
        )
        exchanger = self.identity if isinstance(self.identity, TokenExchanger) else None
        if exchanger is None and not local:
            raise ConfigurationError(
                f"MCP server {server!r} cannot be called: the identity provider "
                f"{self.identity_provider!r} is not set up to obtain "
                "tokens for the services this one calls; configure its exchange options"
            )
        return await self.bindings().mcp_tools(
            entry,
            connector=self.mcp_connector(),
            exchanger=exchanger,
            require_pins=not local,
        )

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
        from ai_agent_lib_core.integrations.mcp import GovernedToolsMiddleware, mcp_result_text

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
        from ai_agent_lib_core.integrations.mcp import DoorTokenVerifier, resource_server_settings

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
        from ai_agent_lib_core.integrations.mcp import PerimeterDoor

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
                    f"register it (agentlib new mcp does this), or check "
                    f"{variable_for(options_key(Section.REGISTRY))} points at the right registry"
                ),
            )
        return entry

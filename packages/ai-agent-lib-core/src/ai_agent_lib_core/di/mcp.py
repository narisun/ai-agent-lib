"""MCP client and server assembly from explicit ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    from ai_agent_lib_core.integrations.mcp.client import McpConnector


@dataclass(frozen=True, slots=True)
class McpBindings:
    """Compose governed MCP clients and servers without owning service lifecycle."""

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
            connector=self.connector,
            exchanger=exchanger,
            require_pins=not local,
        )

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
        ``TokenAuthenticator``. Import the MCP integration only when needed.
        See ``ServiceContainer.mcp_server_kwargs`` for arguments and usage.
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
            if not local:
                raise ConfigurationError(
                    f"MCP server {server!r} cannot check tokens at its door: the identity "
                    f"provider {self.identity_provider!r} does not "
                    "verify tokens from an issuer"
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

"""Assemble configured adapters and own their lifetime for one service.

The public facade keeps the accessors and the framework entry points. The
work is done by focused collaborators in this package: ``selection`` decides
what the configuration asks for and whether its options parse, ``AdapterGraph``
builds and closes the adapters in dependency order, ``RequestAuthenticator``
signs callers in, and ``McpBindings`` assembles MCP tools and middleware.
Framework bindings are loaded on demand; business rules belong in the
pipeline and adapters.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast, overload

from pydantic import ConfigDict, StrictBool

from ai_agent_lib_core.adapters.guardrails_patterns import GuardrailsOptions
from ai_agent_lib_core.adapters.system import SystemClock, UuidGenerator
from ai_agent_lib_core.adapters.telemetry import NullTelemetry, OpenTelemetryTelemetry
from ai_agent_lib_core.config import (
    Key,
    load_service_config,
    options_key,
    telemetry_enabled,
    variable_for,
)
from ai_agent_lib_core.contracts import (
    DEFAULT_MODEL_ALIAS,
    AuditSink,
    ChatModelProvider,
    CheckpointBackend,
    CheckResult,
    Classification,
    Clock,
    ConfigurationError,
    DataSource,
    GuardrailCheck,
    IdentityVerifier,
    IdGenerator,
    ModelRef,
    ModelTraceSink,
    PolicyDecisionPoint,
    RegistrySource,
    RequestContext,
    SecretsProvider,
    Section,
    ServiceConfig,
    Telemetry,
    generic_fix,
    shown_value,
)
from ai_agent_lib_core.di.graph import AdapterGraph
from ai_agent_lib_core.di.providers import DATA_PORT, MODEL_PORT, ServicePort, ServiceProviders
from ai_agent_lib_core.observability.diagnostics import Diagnostics
from ai_agent_lib_core.observability.model_trace import JsonlModelTraceSink
from ai_agent_lib_core.pipeline import (
    BudgetLedger,
    GovernedDataSource,
    Operations,
    build_data_pipeline,
)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from ai_agent_lib_core.di.mcp import McpBindings
    from ai_agent_lib_core.integrations.langgraph import (
        GovernedChatModel,
        LangGraphBindings,
        LoopBridge,
    )
    from ai_agent_lib_core.integrations.mcp import GovernedToolsMiddleware
    from ai_agent_lib_core.integrations.mcp.client import McpConnector

__all__ = ["ServiceContainer", "fix_for"]

_ServiceT = TypeVar("_ServiceT")


def fix_for(problem: BaseException) -> str:
    """Return what a person does about a failed check: the error's own fix, or one by its kind."""
    return generic_fix(problem)


class ServiceContainer:
    """Build configured adapters once and close them in reverse dependency order.

    Entering the context starts the container. Readiness checks are a separate,
    explicit step: call ``validate`` before accepting application requests.
    Use one container per service lifetime, not one per incoming request.

    Use it as an async context manager::

        async with ServiceContainer.from_env() as services:
            await services.validate()
            ...

    Args:
        config: The resolved configuration.
        providers: Factory definitions. Defaults to core adapters and installed
            first-party provider packs; live instances belong to this container.
        clock: The clock port. Defaults to the system clock.
        ids: The identifier port. Defaults to random identifiers.
        telemetry: The telemetry port. By default, resolved telemetry settings
            or the legacy audit ``tracing`` option enable OpenTelemetry;
            otherwise a no-op implementation is used.
        mcp_connector: Opens connections to MCP servers. Defaults to
            Streamable HTTP; a test passes an in-process connector.
        sleep: Waits between retries. A test passes one that does not wait.
        model_trace: Optional diagnostic sink for model exchanges. Overrides the
            configured file destination. An injected sink remains caller-owned.
        diagnostics: Optional metadata recorder, independent of content tracing.
            The caller owns its sink and shares it with the HTTP entry point.
    """

    def __init__(
        self,
        config: ServiceConfig,
        providers: ServiceProviders | None = None,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        telemetry: Telemetry | None = None,
        mcp_connector: McpConnector | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        model_trace: ModelTraceSink | None = None,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        self._config = config
        self._providers = providers if providers is not None else ServiceProviders.default()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdGenerator = ids if ids is not None else UuidGenerator()
        self._telemetry: Telemetry = (
            telemetry if telemetry is not None else self._default_telemetry(config)
        )
        self._model_trace = model_trace
        self._diagnostics = diagnostics
        if self._model_trace is None and config.model_trace_path is not None:
            self._model_trace = JsonlModelTraceSink(config.model_trace_path)
        self._graph = AdapterGraph(config, self._providers, clock=self._clock, ids=self._ids)
        self._langgraph: LangGraphBindings | None = None
        self._mcp: McpBindings | None = None
        self._governed_sources: dict[str, GovernedDataSource] = {}
        self._mcp_connector = mcp_connector
        self._operations = Operations(
            limits=config.limits,
            ledger=BudgetLedger(),
            variable=variable_for(Key.LIMITS),
            sleep=sleep if sleep is not None else asyncio.sleep,
        )

    @classmethod
    def from_env(
        cls,
        dotenv_path: Path | None = Path(".env"),
        providers: ServiceProviders | None = None,
    ) -> Self:
        """Resolve environment settings and return an unstarted container.

        ``start`` or ``async with`` constructs the selected adapters.
        """
        return cls(load_service_config(dotenv_path), providers)

    # ----------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Validate selections and dependencies, then construct the selected adapters.

        Options and dependency ordering are checked before any factory runs.
        Each adapter is created and closed in its own lifetime task. If startup
        fails or is cancelled, already-owned resources are cleaned up before
        the failure reaches the caller. This does not run ``validate``.

        Raises:
            ConfigurationError: If a selected adapter is not registered, or a
                local-only adapter is selected outside local development, options
                are invalid, or declared dependencies cannot be resolved.
            RuntimeError: If the container was already started or closed.
        """
        await self._graph.start()

    async def validate(self) -> None:
        """Run readiness checks on adapters that implement ``SupportsValidation``.

        Call after ``start``. Checks may contact external services. Configuration
        failures are collected; unexpected exceptions propagate immediately.

        Raises:
            ConfigurationError: Summarizing the adapters that reported a
                configuration failure.
        """
        await self._graph.validate()

    async def check(self) -> tuple[CheckResult, ...]:
        """Return diagnostic results for the built adapters.

        Unlike ``validate``, expected library errors become failed results with
        suggested fixes. Unexpected exceptions still propagate. An adapter with
        no readiness check is reported as built, not independently verified.
        """
        return await self._graph.check()

    async def aclose(self) -> None:
        """Close adapters in reverse build order. Safe to call more than once.

        Cleanup is attempted for every owned adapter even if an earlier close
        fails or is cancelled. Concurrent callers await the same teardown.
        Cancelling a caller waits for teardown before propagating cancellation.

        Raises:
            BaseExceptionGroup: Collected cleanup failures when no cancellation
                takes precedence; ordinary errors form an ``ExceptionGroup``.
            asyncio.CancelledError: After cleanup completes if the caller or an
                adapter close was cancelled. Cleanup errors are chained to it.
        """
        mcp, self._mcp = self._mcp, None
        try:
            # The MCP bindings own the HTTP client they created; it goes before the adapters.
            if mcp is not None:
                await mcp.aclose()
        finally:
            try:
                await self._graph.aclose()
            finally:
                self._langgraph = None
                self._governed_sources.clear()

    @property
    def closed(self) -> bool:
        """Whether the adapters have been closed; a closed container cannot be reused."""
        return self._graph.closed

    # ------------------------------------------------------------------ accessors

    @property
    def config(self) -> ServiceConfig:
        """The resolved configuration."""
        return self._config

    @property
    def clock(self) -> Clock:
        """The clock port."""
        return self._clock

    @property
    def ids(self) -> IdGenerator:
        """The identifier port."""
        return self._ids

    @property
    def telemetry(self) -> Telemetry:
        """The telemetry port."""
        return self._telemetry

    @property
    def secrets(self) -> SecretsProvider:
        """The secrets port."""
        return cast(SecretsProvider, self.get(Section.SECRETS))

    @property
    def audit(self) -> AuditSink:
        """The audit port."""
        return cast(AuditSink, self.get(Section.AUDIT))

    @property
    def identity(self) -> IdentityVerifier:
        """The identity port."""
        return cast(IdentityVerifier, self.get(Section.IDENTITY))

    @property
    def checkpoint_backend(self) -> CheckpointBackend:
        """The checkpoint port."""
        return cast(CheckpointBackend, self.get(Section.CHECKPOINT))

    @property
    def registry(self) -> RegistrySource:
        """The agent and tool registries."""
        return cast(RegistrySource, self.get(Section.REGISTRY))

    @property
    def policy(self) -> PolicyDecisionPoint:
        """The policy decision point."""
        return cast(PolicyDecisionPoint, self.get(Section.POLICY))

    @property
    def guardrails(self) -> GuardrailCheck:
        """The guardrail port."""
        return cast(GuardrailCheck, self.get(Section.GUARDRAILS))

    @overload
    def get(self, section: ServicePort[_ServiceT]) -> _ServiceT: ...

    @overload
    def get(self, section: Section) -> object: ...

    def get(self, section: Section | ServicePort[Any]) -> object:
        """Return a built singleton adapter, checking a typed port when supplied.

        The container must be started. Use ``model_provider`` for model provider
        names and ``data_source`` for named, governed data sources.
        """
        self._graph.require_started()
        if isinstance(section, ServicePort):
            return section.checked(self._graph.resolve_built(section.name))
        selection = self._config.section(section)
        return self._graph.get((section.value, selection.provider, ""))

    def model_provider(self, name: str) -> ChatModelProvider:
        """Return the chat model provider called ``name``.

        This is the configured provider name, such as ``anthropic``, not a model
        alias. Use ``model(alias)`` for a governed LangGraph model.

        Raises:
            ConfigurationError: If no configured alias uses that provider.
        """
        self._graph.require_started()
        try:
            return cast(ChatModelProvider, self._graph.get((MODEL_PORT, name, "")))
        except KeyError:
            raise ConfigurationError(
                f"model provider {name!r} is not used by any configured alias"
            ) from None

    def resolve_model(self, alias: str) -> tuple[ModelRef, ChatModelProvider]:
        """Return the concrete model behind ``alias`` and the provider that serves it."""
        try:
            ref = self._config.model.resolve(alias)
        except ConfigurationError as error:
            # The resolver knows the alias table; the container knows the variables.
            if alias == DEFAULT_MODEL_ALIAS:
                fix = (
                    f"set {variable_for(Key.MODEL_ID)} to a model ID, for example in the "
                    "service's .env; use 'fake-model' with the fake provider"
                )
            else:
                fix = (
                    f'add "{alias}" to {variable_for(Key.MODEL_ALIASES)}, for example '
                    f'{{"{alias}": {{"model_id": "..."}}}}'
                )
            raise ConfigurationError(
                error.message, expected=error.expected, actual=error.actual, fix=fix
            ) from error
        return ref, self.model_provider(ref.provider)

    def data_source(self, name: str) -> GovernedDataSource:
        """Return the governed data source called ``name``.

        Every query made through it is put to the policy, narrowed by the
        policy's obligations and written to the audit log.

        Raises:
            ConfigurationError: If no data source of that name is configured.
        """
        self._graph.require_started()
        governed = self._governed_sources.get(name)
        if governed is not None:
            return governed
        selection = self._config.data_sources.get(name)
        if selection is None:
            known = ", ".join(sorted(self._config.data_sources)) or "none"
            raise ConfigurationError(
                f"data source {name!r} is not configured",
                expected=f"{name!r} among the configured data sources",
                actual=f"configured data sources: {known}",
                fix=(
                    f'add {{"{name}": {{"kind": "...", ...}}}} to {variable_for(Key.DATA_SOURCES)}'
                ),
            )
        adapter = cast(DataSource, self._graph.get((DATA_PORT, selection.provider, name)))
        pipeline = build_data_pipeline(
            audit=self.audit,
            telemetry=self._telemetry,
            clock=self._clock,
            ids=self._ids,
            policy=self.policy,
            environment=self._config.deployment_env.value,
            operations=self._operations,
        )
        governed = GovernedDataSource(name, adapter, pipeline=pipeline)
        self._governed_sources[name] = governed
        return governed

    # ------------------------------------------------------------------ LangGraph

    def model(self, alias: str = DEFAULT_MODEL_ALIAS) -> GovernedChatModel:
        """Return the governed chat model behind ``alias``. It is a ``BaseChatModel``."""
        return self._bindings().model(alias)

    def tools(
        self,
        tools: Sequence[BaseTool | Callable[..., Any]],
        *,
        read_only: Collection[str] = (),
    ) -> list[BaseTool]:
        """Return governed versions of ``tools``.

        Args:
            tools: Tools, or functions with type hints and a docstring.
            read_only: Names of tools that have no write effects and are safe
                to retry. The resilience stage may repeat these calls.
        """
        return self._bindings().tools(tools, read_only=read_only)

    def compile_kwargs(self) -> dict[str, Any]:
        """Return ``StateGraph.compile`` arguments with a scoped checkpointer."""
        return self._bindings().compile_kwargs()

    def invocation(self, context: RequestContext) -> dict[str, Any]:
        """Return ``ainvoke`` or ``astream`` arguments for one authenticated request.

        Scope the thread ID to the caller and application, and pass the request
        as LangGraph runtime context rather than persisted graph state.
        """
        return self._bindings().invocation(context)

    async def authenticate(
        self,
        credential: str | None,
        *,
        application: str,
        thread_id: str,
        request_id: str | None = None,
        classification_ceiling: Classification | None = None,
        deadline: datetime | None = None,
    ) -> RequestContext:
        """Verify a caller's credential and return the context for their request.

        This is what the entry point of an agent calls once per request, with
        the bearer token it was sent. The credential is kept in the context
        only so that it can later be exchanged for a token bound to an MCP
        server; it is never forwarded.

        Args:
            credential: The token the caller presented, without the ``Bearer``
                prefix. ``None`` is accepted only by a verifier that supports
                tokenless access, such as the local development identity.
            application: This agent's name.
            thread_id: The conversation or workflow the request belongs to.
            request_id: Correlates logs. Defaults to a new identifier.
            classification_ceiling: The most sensitive data the request may
                touch. Defaults to the ceiling the agent registry gives this
                agent, or ``internal`` if it is not registered.
            deadline: When the request must be finished.

        Raises:
            PolicyDenied: If the configured identity verifier rejects the credential.
                The refusal is written to the audit log before it is raised.
            IntegrityError: If that audit record could not be written.
        """
        from ai_agent_lib_core.di.authentication import RequestAuthenticator

        return await RequestAuthenticator(
            identity=self.identity,
            registry=self.registry,
            audit=self.audit,
            telemetry=self.telemetry,
            clock=self.clock,
            ids=self.ids,
        ).authenticate(
            credential,
            application=application,
            thread_id=thread_id,
            request_id=request_id,
            classification_ceiling=classification_ceiling,
            deadline=deadline,
        )

    async def mcp_tools(self, server: str) -> list[BaseTool]:
        """Return the registered tools of an MCP server as governed tools.

        The server is looked up in the tool registry and each tool's live input
        schema is compared with its pin. Outside local development every tool
        must be pinned.

        Raises:
            ConfigurationError: If the server is unregistered or its URL is
                invalid. Outside local development, remote addresses require
                TLS and the identity provider must support token exchange.
            IntegrityError: If a registered tool is missing or its schema does
                not match its pin.
            TransientError: If the server cannot be reached.
        """
        return await self._mcp_bindings().mcp_tools(server)

    async def registered_mcp_tools(self, application: str) -> list[BaseTool]:
        """Return the governed tools of every MCP server registered for ``application``.

        The agent's entry names its servers; each is loaded as :meth:`mcp_tools`
        loads it, and the lists are joined in registry order. An agent that is
        not registered gets no tools.
        """
        return await self._mcp_bindings().registered_mcp_tools(application)

    def mcp_middleware(
        self,
        server: str,
        *,
        application: str | None = None,
        classification_ceiling: Classification = Classification.RESTRICTED,
    ) -> GovernedToolsMiddleware:
        """Return the middleware that governs the tool calls an MCP server receives.

        Pass it to ``MCPServer(..., middleware=[...])``. Every ``tools/call``
        then runs the tool pipeline: identity, registry, policy, guardrails
        and audit. The caller's token must name a registered agent that is
        listed for this server; in local development a call that names no
        agent is also accepted, so a developer can call a tool directly.

        Args:
            server: The server's ID in the tool registry.
            application: The server's own application name, as policy and audit
                see it. Defaults to the server's ID.
            classification_ceiling: The most sensitive data the server may handle.

        Raises:
            ConfigurationError: If the server is not in the tool registry.
        """
        return self._mcp_bindings().mcp_middleware(
            server, application=application, classification_ceiling=classification_ceiling
        )

    def mcp_server_kwargs(
        self,
        server: str,
        *,
        application: str | None = None,
        classification_ceiling: Classification = Classification.RESTRICTED,
    ) -> dict[str, Any]:
        """Return keyword arguments for ``MCPServer``: everything that governs the server.

        Use it as ``MCPServer(name, **services.mcp_server_kwargs("accounts"))``.
        It supplies the middleware of :meth:`mcp_middleware` and, when the
        identity provider verifies tokens from an issuer, the settings that
        make the server check a token at its door. A request over HTTP with
        no valid token is then answered with HTTP 401, whatever it asks for,
        and outside local development the token must come from a registered
        agent that is listed for this server.

        With the development identity on a developer's machine there is no
        door check: a caller that shows no token is the development principal.

        Args:
            server: The server's ID in the tool registry.
            application: The server's own application name, as policy and audit
                see it. Defaults to the server's ID.
            classification_ceiling: The most sensitive data the server may handle.

        Raises:
            ConfigurationError: If the server is not in the tool registry, or,
                outside local development, the identity provider cannot check
                tokens at the door.
        """
        return self._mcp_bindings().mcp_server_kwargs(
            server, application=application, classification_ceiling=classification_ceiling
        )

    def _mcp_bindings(self) -> McpBindings:
        from ai_agent_lib_core.di.mcp import McpBindings

        self._graph.require_started()
        if self._mcp is None:
            self._mcp = McpBindings(
                audit=self.audit,
                telemetry=self.telemetry,
                clock=self.clock,
                ids=self.ids,
                identity=self.identity,
                registry=self.registry,
                policy=self.policy,
                guardrails=self.guardrails,
                environment=self.config.deployment_env,
                identity_provider=self.config.section(Section.IDENTITY).provider,
                operations=self._operations,
                connector=self._mcp_connector,
                bindings=self._bindings,
            )
        return self._mcp

    def _bindings(self) -> LangGraphBindings:
        self._graph.require_started()
        if self._langgraph is None:
            # Imported here so that code which never builds a graph does not load LangGraph.
            from ai_agent_lib_core.integrations.langgraph import LangGraphBindings

            self._langgraph = LangGraphBindings(
                audit=self.audit,
                telemetry=self._telemetry,
                clock=self._clock,
                ids=self._ids,
                resolve_model=self.resolve_model,
                checkpoint_backend=self.checkpoint_backend,
                registry=self.registry,
                policy=self.policy,
                environment=self._config.deployment_env.value,
                guardrails=self.guardrails,
                frame_tool_results=self._frame_tool_results(),
                bridge=self._bridge,
                operations=self._operations,
                model_trace=self._model_trace,
                diagnostics=self._diagnostics,
            )
        return self._langgraph

    def _bridge(self) -> LoopBridge:
        from ai_agent_lib_core.integrations.langgraph import LoopBridge

        return LoopBridge(self._graph.loop)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _default_telemetry(config: ServiceConfig) -> Telemetry:
        return OpenTelemetryTelemetry() if telemetry_enabled(config) else NullTelemetry()

    def _frame_tool_results(self) -> bool:
        """Whether tool results are framed as untrusted: the shared guardrails option.

        The option is read through the options model, strictly, whatever
        provider is selected: a provider that declares no options model still
        honours the shared switch.

        Raises:
            ConfigurationError: If the option is not true or false.
        """
        selection = self._config.section(Section.GUARDRAILS)
        try:
            return selection.parse_options(_FramingOption).frame_tool_results
        except ConfigurationError as error:
            raise ConfigurationError(
                "the guardrails option 'frame_tool_results' is not true or false",
                expected="true or false",
                actual=shown_value(selection.options.get("frame_tool_results")),
                fix=(
                    'write "frame_tool_results": true in '
                    f"{variable_for(options_key(Section.GUARDRAILS))}"
                ),
            ) from error


class _FramingOption(GuardrailsOptions):
    """The shared guardrails option only; a provider's own keys are parsed at start."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    frame_tool_results: StrictBool = True

"""The service container: the composition root that owns lifecycle."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from pydantic import SecretStr

from ai_agent_lib_core.adapters.http_support import checked_base_url
from ai_agent_lib_core.adapters.system import SystemClock, UuidGenerator
from ai_agent_lib_core.adapters.telemetry import NullTelemetry, OpenTelemetryTelemetry
from ai_agent_lib_core.config import (
    Key,
    load_service_config,
    options_key,
    provider_key,
    variable_for,
)
from ai_agent_lib_core.contracts import (
    DEFAULT_MODEL_ALIAS,
    AgentLibError,
    AuditSink,
    ChatModelProvider,
    CheckpointBackend,
    CheckResult,
    Classification,
    Clock,
    ConfigurationError,
    DataSource,
    DeploymentEnv,
    GuardrailCheck,
    IdentityVerifier,
    IdGenerator,
    ModelRef,
    PolicyDecisionPoint,
    PolicyDenied,
    ProviderSelection,
    RegistrySource,
    RequestContext,
    SecretsProvider,
    Section,
    ServerEntry,
    ServiceConfig,
    SupportsAsyncClose,
    SupportsValidation,
    Telemetry,
    TelemetryMode,
    TokenAuthenticator,
    TokenExchanger,
    generic_fix,
    shown_value,
)
from ai_agent_lib_core.di.access import selected_models
from ai_agent_lib_core.di.providers import (
    DATA_PORT,
    MODEL_PORT,
    BuildContext,
    ProviderSpec,
    ServiceProviders,
)
from ai_agent_lib_core.pipeline import (
    AgentCheck,
    BudgetLedger,
    GovernedDataSource,
    Operations,
    Pipeline,
    ToolCall,
    ToolStage,
    build_tool_pipeline,
    record_refused_sign_in,
)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from ai_agent_lib_core.integrations.langgraph import (
        GovernedChatModel,
        LangGraphBindings,
        LoopBridge,
    )
    from ai_agent_lib_core.integrations.mcp import GovernedToolsMiddleware
    from ai_agent_lib_core.integrations.mcp.client import McpConnector

__all__ = ["ServiceContainer", "fix_for"]

_Key = tuple[str, str, str]
"""Identifies one built adapter: its port, its adapter name and its instance name."""


def fix_for(problem: BaseException) -> str:
    """Return what a person does about a failed check: the error's own fix, or one by its kind."""
    return generic_fix(problem)


class ServiceContainer:
    """Builds each adapter once, validates at startup and closes in reverse order.

    Use it as an async context manager::

        async with ServiceContainer.from_env() as services:
            await services.validate()
            ...

    Args:
        config: The resolved configuration.
        providers: The adapter registry. Defaults to the local adapters.
        clock: The clock port. Defaults to the system clock.
        ids: The identifier port. Defaults to random identifiers.
        telemetry: The telemetry port. By default it follows the ``tracing``
            option of the audit section: OpenTelemetry when on, a no-op when off.
        mcp_connector: Opens connections to MCP servers. Defaults to
            Streamable HTTP; a test passes an in-process connector.
        sleep: Waits between retries. A test passes one that does not wait.
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
    ) -> None:
        self._config = config
        self._providers = providers if providers is not None else ServiceProviders.default()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ids: IdGenerator = ids if ids is not None else UuidGenerator()
        self._telemetry: Telemetry = (
            telemetry if telemetry is not None else self._default_telemetry(config)
        )
        self._services: dict[_Key, object] = {}
        self._build_order: list[_Key] = []
        self._building: list[_Key] = []
        self._started = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._langgraph: LangGraphBindings | None = None
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
        """Build a container from the process environment and a ``.env`` file."""
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
        """Check the selections, freeze the registry and build every section adapter.

        Raises:
            ConfigurationError: If a selected adapter is not registered, or a
                local-only adapter is selected outside local development.
            RuntimeError: If the container was already started or closed.
        """
        if self._started or self._closed:
            raise RuntimeError("a container can be started only once")
        selected = self._selected()
        for port, name, _ in selected:
            self._check_allowed(self._providers.lookup(port, name))
        self._check_options(selected)
        self._providers.freeze()
        self._loop = asyncio.get_running_loop()
        self._started = True
        try:
            for key in selected:
                await self._build(key)
        except BaseException:
            await self.aclose()
            raise

    async def validate(self) -> None:
        """Ask every built adapter that can check itself to do so.

        Raises:
            ConfigurationError: Listing every adapter that is not ready.
        """
        self._require_started()
        problems: list[str] = []
        for key in self._build_order:
            service = self._services[key]
            if isinstance(service, SupportsValidation):
                try:
                    await service.validate()
                except ConfigurationError as exc:
                    problems.append(f"{self._label(key)}: {exc.summary}")
        if problems:
            count = len(problems)
            raise ConfigurationError(
                f"startup validation failed for {count} adapter{'s' if count != 1 else ''}",
                actual=" | ".join(problems),
                fix="run 'agentlib doctor' for one line per adapter with what to do",
            )

    async def check(self) -> tuple[CheckResult, ...]:
        """Check every built adapter and report each one, usable or not.

        ``validate`` stops a service that is not ready. This is for a person
        finding out why: one result per adapter, with what to do about a failure.
        """
        self._require_started()
        results: list[CheckResult] = []
        for key in self._build_order:
            service = self._services[key]
            label = self._label(key)
            if not isinstance(service, SupportsValidation):
                results.append(CheckResult(label, ok=True, detail="built"))
                continue
            try:
                await service.validate()
            except AgentLibError as problem:
                results.append(CheckResult.failed(label, problem))
            else:
                results.append(CheckResult(label, ok=True, detail="checked"))
        return tuple(results)

    async def aclose(self) -> None:
        """Close adapters in reverse build order. Safe to call more than once.

        Every adapter is closed even if an earlier close fails.

        Raises:
            ExceptionGroup: Holding each error raised while closing.
        """
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for key in reversed(self._build_order):
            service = self._services[key]
            if isinstance(service, SupportsAsyncClose):
                try:
                    await service.aclose()
                except Exception as exc:  # noqa: BLE001 - every adapter must get its turn
                    errors.append(exc)
        self._services.clear()
        self._build_order.clear()
        self._langgraph = None
        self._governed_sources.clear()
        if errors:
            raise ExceptionGroup("errors while closing the service container", errors)

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

    def get(self, section: Section) -> object:
        """Return the adapter that serves ``section``."""
        self._require_started()
        selection = self._config.section(section)
        return self._services[(section.value, selection.provider, "")]

    def model_provider(self, name: str) -> ChatModelProvider:
        """Return the chat model provider called ``name``.

        Raises:
            ConfigurationError: If no configured alias uses that provider.
        """
        self._require_started()
        try:
            return cast(ChatModelProvider, self._services[(MODEL_PORT, name, "")])
        except KeyError:
            raise ConfigurationError(
                f"model provider {name!r} is not used by any configured alias"
            ) from None

    def resolve_model(self, alias: str) -> tuple[ModelRef, ChatModelProvider]:
        """Return the concrete model behind ``alias`` and the provider that serves it."""
        try:
            ref = self._config.model.resolve(alias)
        except ConfigurationError as error:
            if alias == DEFAULT_MODEL_ALIAS:
                error.fix = (
                    f"set {variable_for(Key.MODEL_ID)} to a model ID, for example in the "
                    "service's .env; use 'fake-model' with the fake provider"
                )
            else:
                error.fix = (
                    f'add "{alias}" to {variable_for(Key.MODEL_ALIASES)}, for example '
                    f'{{"{alias}": {{"model_id": "..."}}}}'
                )
            raise
        return ref, self.model_provider(ref.provider)

    def data_source(self, name: str) -> GovernedDataSource:
        """Return the governed data source called ``name``.

        Every query made through it is put to the policy, narrowed by the
        policy's obligations and written to the audit log.

        Raises:
            ConfigurationError: If no data source of that name is configured.
        """
        self._require_started()
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
        adapter = cast(DataSource, self._services[(DATA_PORT, selection.provider, name)])
        governed = GovernedDataSource(
            name,
            adapter,
            policy=self.policy,
            audit=self.audit,
            telemetry=self._telemetry,
            clock=self._clock,
            ids=self._ids,
            environment=self._config.deployment_env.value,
        )
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
            read_only: Names of tools that only read.
        """
        return self._bindings().tools(tools, read_only=read_only)

    def compile_kwargs(self) -> dict[str, Any]:
        """Return keyword arguments for ``StateGraph.compile``."""
        return self._bindings().compile_kwargs()

    def invocation(self, context: RequestContext) -> dict[str, Any]:
        """Return keyword arguments for ``ainvoke`` or ``astream`` for one request."""
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
            credential: The token the caller presented, without the ``Bearer`` prefix.
            application: This agent's name.
            thread_id: The conversation or workflow the request belongs to.
            request_id: Correlates logs. Defaults to a new identifier.
            classification_ceiling: The most sensitive data the request may
                touch. Defaults to the ceiling the agent registry gives this
                agent, or ``internal`` if it is not registered.
            deadline: When the request must be finished.

        Raises:
            PolicyDenied: If the credential is missing, expired or not trusted.
                The refusal is written to the audit log before it is raised.
            IntegrityError: If that audit record could not be written.
        """
        request_id = request_id if request_id is not None else self._ids.new_id()
        try:
            principal = await self.identity.verify(credential)
        except PolicyDenied as refusal:
            await record_refused_sign_in(
                self.audit,
                self._telemetry,
                self._clock,
                self._ids,
                refusal=refusal,
                application=application,
                request_id=request_id,
                thread_id=thread_id,
            )
            raise
        if classification_ceiling is None:
            entry = self.registry.agents.get(application)
            classification_ceiling = (
                entry.classification_ceiling if entry is not None else Classification.INTERNAL
            )
        return RequestContext(
            principal=principal,
            application=application,
            request_id=request_id,
            thread_id=thread_id,
            classification_ceiling=classification_ceiling,
            deadline=deadline,
            credential=SecretStr(credential) if credential else None,
        )

    async def mcp_tools(self, server: str) -> list[BaseTool]:
        """Return the registered tools of an MCP server as governed tools.

        The server is looked up in the tool registry and each tool's live input
        schema is compared with its pin. Outside local development every tool
        must be pinned.

        Raises:
            ConfigurationError: If the server is not in the tool registry.
            ConfigurationError: Also if, outside local development, the server
                is registered with a plain ``http`` address on another host,
                or the identity provider cannot issue tokens for the server.
                A token is sent only over TLS, and a call with no token would
                be refused by the server.
            IntegrityError: If a registered tool is missing or its schema does
                not match its pin.
            TransientError: If the server cannot be reached.
        """
        entry = self._registered_server(server)
        local = self._config.deployment_env is DeploymentEnv.LOCAL
        # The caller's token travels to this address. Plain http is for this machine only.
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
                f"{self._config.section(Section.IDENTITY).provider!r} is not set up to obtain "
                "tokens for the services this one calls; configure its exchange options"
            )
        return await self._bindings().mcp_tools(
            entry,
            connector=self._mcp_connector,
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
        # Imported here so that an agent which serves no MCP tools does not load the SDK.
        from ai_agent_lib_core.integrations.mcp import GovernedToolsMiddleware, mcp_result_text

        entry = self._registered_server(server)
        pipeline: Pipeline[ToolStage, ToolCall, Any] = build_tool_pipeline(
            audit=self.audit,
            telemetry=self._telemetry,
            clock=self._clock,
            ids=self._ids,
            registry=self.registry,
            # The agent is somewhere else: it is whoever the caller's token names.
            registry_agent_check=(
                AgentCheck.CALLER
                if self._config.deployment_env is DeploymentEnv.LOCAL
                else AgentCheck.CALLER_REQUIRED
            ),
            policy=self.policy,
            environment=self._config.deployment_env.value,
            guardrails=self.guardrails,
            response_text=mcp_result_text,
            operations=self._operations,
        )
        return GovernedToolsMiddleware(
            server=entry.id,
            application=application if application is not None else entry.id,
            identity=self.identity,
            pipeline=pipeline,
            ids=self._ids,
            classification_ceiling=classification_ceiling,
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
        local = self._config.deployment_env is DeploymentEnv.LOCAL
        if not isinstance(identity, TokenAuthenticator):
            if not local:
                raise ConfigurationError(
                    f"MCP server {server!r} cannot check tokens at its door: the identity "
                    f"provider {self._config.section(Section.IDENTITY).provider!r} does not "
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
            telemetry=self._telemetry,
            clock=self._clock,
            ids=self._ids,
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

    def _bindings(self) -> LangGraphBindings:
        self._require_started()
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
            )
        return self._langgraph

    def _bridge(self) -> LoopBridge:
        from ai_agent_lib_core.integrations.langgraph import LoopBridge

        if self._loop is None:
            raise RuntimeError(
                "the service container is not started: use it as "
                "'async with ServiceContainer(config) as services:' or await start() first"
            )
        return LoopBridge(self._loop)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _default_telemetry(config: ServiceConfig) -> Telemetry:
        tracing = config.section(Section.AUDIT).options.get("tracing", False)
        if not isinstance(tracing, bool):
            raise ConfigurationError(
                "the audit option 'tracing' is not true or false",
                expected="true or false",
                actual=shown_value(tracing),
                fix=(
                    'write "tracing": true or "tracing": false in '
                    f"{variable_for(options_key(Section.AUDIT))}, or set "
                    f"{variable_for(Key.TELEMETRY)}=opentelemetry instead"
                ),
            )
        wanted = tracing or config.telemetry is TelemetryMode.OPENTELEMETRY
        return OpenTelemetryTelemetry() if wanted else NullTelemetry()

    def _frame_tool_results(self) -> bool:
        framing = self._config.section(Section.GUARDRAILS).options.get("frame_tool_results", True)
        if not isinstance(framing, bool):
            raise ConfigurationError(
                "the guardrails option 'frame_tool_results' is not true or false",
                expected="true or false",
                actual=shown_value(framing),
                fix=(
                    'write "frame_tool_results": true in '
                    f"{variable_for(options_key(Section.GUARDRAILS))}"
                ),
            )
        return framing

    def _require_started(self) -> None:
        if self._closed:
            raise RuntimeError("the container is closed")
        if not self._started:
            raise RuntimeError(
                "the service container is not started: use it as "
                "'async with ServiceContainer(config) as services:' or await start() first"
            )

    def _selected(self) -> list[_Key]:
        """Return every adapter the configuration selects, in build order."""
        selected: list[_Key] = [
            (section.value, self._config.section(section).provider, "") for section in Section
        ]
        # The same choice the permission plan makes: no model configured, no provider.
        selected.extend((MODEL_PORT, name, "") for name in selected_models(self._config))
        selected.extend(
            (DATA_PORT, selection.provider, source)
            for source, selection in self._config.data_sources.items()
        )
        return selected

    @staticmethod
    def _label(key: _Key) -> str:
        port, name, instance = key
        return f"{port} {instance!r} ({name})" if instance else f"{port} ({name})"

    def _check_allowed(self, spec: ProviderSpec) -> None:
        env = self._config.deployment_env
        if spec.local_only and env is not DeploymentEnv.LOCAL:
            raise ConfigurationError(
                f"provider {spec.name!r} for {spec.port} is for local development only "
                f"and cannot be used when the deployment environment is {env.value!r}",
                expected=f"a {spec.port} provider meant for a deployed service",
                actual=f"{spec.name!r}, which is local only",
                fix=(
                    f"select a server-side provider for {spec.port} (the aws profile does), "
                    f"or set {variable_for(Key.DEPLOYMENT_ENV)}=local on a developer's machine"
                ),
            )

    def _selection_for(self, key: _Key) -> ProviderSelection:
        port, name, instance = key
        if port == MODEL_PORT:
            return ProviderSelection(provider=name)
        if port == DATA_PORT:
            return self._config.data_sources[instance]
        return self._config.section(Section(port))

    async def _build(self, key: _Key) -> object:
        port, name, instance = key
        if key in self._services:
            return self._services[key]
        if key in self._building:
            cycle = " -> ".join(self._label(k) for k in [*self._building, key])
            raise ConfigurationError(f"adapters depend on each other in a cycle: {cycle}")
        spec = self._providers.lookup(port, name)
        context = BuildContext(
            port=port,
            selection=self._selection_for(key),
            deployment_env=self._config.deployment_env,
            tls_ca_bundle=self._config.tls_ca_bundle,
            external=self._config.external,
            clock=self._clock,
            ids=self._ids,
            secret_values=self._config.secrets if port == Section.SECRETS.value else {},
            resolver=self._resolve_built,
            instance=instance,
        )
        self._building.append(key)
        try:
            service = spec.factory(context)
            if inspect.isawaitable(service):
                service = await service
        except AgentLibError as error:
            error.add_note(self._where_configured(key))
            raise
        finally:
            self._building.pop()
        self._services[key] = service
        self._build_order.append(key)
        return service

    def _check_options(self, selected: Sequence[_Key]) -> None:
        """Check every selected adapter's options before building any of them.

        So a mistyped option stops startup at once, with every such problem
        listed, rather than after a database or a stream has been reached.
        """
        problems: list[ConfigurationError] = []
        for key in selected:
            port, name, _ = key
            model = self._providers.lookup(port, name).options
            if model is None:
                continue
            try:
                self._selection_for(key).parse_options(model)
            except ConfigurationError as error:
                error.add_note(self._where_configured(key))
                problems.append(error)
        if len(problems) == 1:
            raise problems[0]
        if problems:
            raise ConfigurationError(
                f"the options of {len(problems)} adapters are not valid",
                actual=" | ".join(f"{error.summary} [{error.__notes__[-1]}]" for error in problems),
                fix="correct each; the developer guide lists every option with its default",
            )

    @staticmethod
    def _where_configured(key: _Key) -> str:
        """Name the variables that chose and configured the adapter being built."""
        port, name, instance = key
        if port == MODEL_PORT:
            chosen = f"{variable_for(Key.MODEL_PROVIDER)} or {variable_for(Key.MODEL_ALIASES)}"
            return f"while building the model provider {name!r}, chosen by {chosen}"
        if port == DATA_PORT:
            return (
                f"while building the data source {instance!r} of kind {name!r}, "
                f"configured in {variable_for(Key.DATA_SOURCES)}"
            )
        section = Section(port)
        return (
            f"while building the {port} adapter {name!r}, chosen by "
            f"{variable_for(provider_key(section))} with options from "
            f"{variable_for(options_key(section))}"
        )

    def _resolve_built(self, port: str) -> object:
        """Give a factory a dependency that was built before it."""
        section = Section(port)
        key = (section.value, self._config.section(section).provider, "")
        if key not in self._services:
            raise ConfigurationError(
                f"{port} is not built yet; an adapter may depend only on sections built before it"
            )
        return self._services[key]

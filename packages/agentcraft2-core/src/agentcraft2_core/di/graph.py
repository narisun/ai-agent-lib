"""Build the selected adapters in dependency order and close them in reverse.

The graph owns what the container used to do inline: ordering, construction
in isolated lifetime tasks, readiness checks, and teardown that gives every
owned resource its turn. The container keeps the public accessors and the
framework facades, and asks the graph for built adapters.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from typing import Any

from agentcraft2_core.contracts import (
    AgentCraft2Error,
    CheckResult,
    Clock,
    ConfigurationError,
    IdGenerator,
    Section,
    ServiceConfig,
    SupportsAsyncClose,
    SupportsValidation,
)
from agentcraft2_core.di.dependencies import build_order
from agentcraft2_core.di.lifetime import AdapterLifetime
from agentcraft2_core.di.providers import BuildContext, BuildResources, ServiceProviders
from agentcraft2_core.di.selection import (
    AdapterKey,
    check_allowed,
    check_options,
    label,
    selected_adapters,
    selection_for,
    where_configured,
)

__all__ = ["AdapterGraph"]


class AdapterGraph:
    """The built adapters of one service, from first factory to last close.

    Args:
        config: The resolved configuration.
        providers: Factory definitions; live instances belong to this graph.
        clock: The clock port, given to every factory.
        ids: The identifier port, given to every factory.
    """

    def __init__(
        self,
        config: ServiceConfig,
        providers: ServiceProviders,
        *,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._config = config
        self._providers = providers
        self._clock = clock
        self._ids = ids
        self._services: dict[AdapterKey, object] = {}
        self._build_order: list[AdapterKey] = []
        self._building: list[AdapterKey] = []
        self._started = False
        self._closed = False
        self._closing: asyncio.Task[None] | None = None
        self._owner: asyncio.Task[None] | None = None
        self._background_failure = False
        self._stop = asyncio.Event()
        self._resources = BuildResources()
        self._lifetimes: list[AdapterLifetime] = []
        self._claimed_adapters: set[int] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------- state

    @property
    def started(self) -> bool:
        """Whether ``start`` has run."""
        return self._started

    @property
    def closed(self) -> bool:
        """Whether every adapter has been closed."""
        return self._closed

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The event loop the adapters were built on.

        Raises:
            RuntimeError: If the graph is not started.
        """
        if self._loop is None:
            raise RuntimeError(
                "the service container is not started: use it as "
                "'async with ServiceContainer(config) as services:' or await start() first"
            )
        return self._loop

    @property
    def resources(self) -> BuildResources:
        """The resources the factories share, closed after the adapters that use them.

        The MCP bindings hand them to a connector factory, so a provider pack's
        connector shares the pack's session factory with the pack's adapters.
        """
        return self._resources

    def require_started(self) -> None:
        """Refuse use before ``start`` or after ``aclose``.

        Raises:
            RuntimeError: If the graph is closed or was never started.
        """
        if self._closed or self._closing is not None:
            raise RuntimeError("the container is closed")
        if not self._started:
            raise RuntimeError(
                "the service container is not started: use it as "
                "'async with ServiceContainer(config) as services:' or await start() first"
            )

    def get(self, key: AdapterKey) -> object:
        """Return the built adapter behind ``key``.

        Raises:
            KeyError: If no such adapter was built.
        """
        return self._services[key]

    def resolve_built(self, port: str) -> object:
        """Return the one singleton adapter built for ``port``.

        Raises:
            ConfigurationError: If none or more than one was built.
        """
        matches = [value for key, value in self._services.items() if key[0] == port and not key[2]]
        if len(matches) != 1:
            raise ConfigurationError(
                f"{port} is not built yet or is ambiguous; declare a singleton dependency "
                "when registering the adapter"
            )
        return matches[0]

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Validate selections and dependencies, then construct the selected adapters.

        Options and dependency ordering are checked before any factory runs.
        Each adapter is created and closed in its own lifetime task. If startup
        fails or is cancelled, already-owned resources are cleaned up before
        the failure reaches the caller.

        Raises:
            ConfigurationError: If a selected adapter is not registered, or a
                local-only adapter is selected outside local development, options
                are invalid, or declared dependencies cannot be resolved.
            RuntimeError: If the graph was already started or closed.
        """
        if self._started or self._closed or self._closing is not None:
            raise RuntimeError("a container can be started only once")
        selected = selected_adapters(self._config)
        for port, name, _ in selected:
            check_allowed(self._providers.lookup(port, name), self._config.deployment_env)
        check_options(self._config, self._providers, selected)
        selected = build_order(selected, self._providers)
        self._providers.freeze()
        self._loop = asyncio.get_running_loop()
        self._started = True
        ready: asyncio.Future[None] = self._loop.create_future()
        self._owner = asyncio.create_task(self._own_lifecycle(selected, ready))
        try:
            await asyncio.shield(ready)
        except asyncio.CancelledError:
            self._owner.cancel()
            try:
                await self.aclose()
            finally:
                if ready.done():
                    ready.exception()  # retrieve a construction failure after caller cancellation
            raise

    async def _own_lifecycle(
        self, selected: Sequence[AdapterKey], ready: asyncio.Future[None]
    ) -> None:
        """Order isolated adapter lifetimes and collect their failures."""
        try:
            for key in selected:
                await self._build(key)
        except BaseException as startup_error:  # noqa: BLE001 - delivered through startup future
            try:
                await self._close_all()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve both failures
                if self._background_failure and isinstance(startup_error, asyncio.CancelledError):
                    startup_error = cleanup_error
                else:
                    startup_error = BaseExceptionGroup(
                        "startup failed and cleanup also failed", [startup_error, cleanup_error]
                    )
            ready.set_exception(startup_error)
            return
        ready.set_result(None)
        try:
            await self._stop.wait()
        finally:
            await self._close_all()

    def _adapter_failed(self) -> None:
        """Interrupt pending construction when an already-built adapter fails."""
        if not self._stop.is_set():
            self._background_failure = True
            self._stop.set()
            if self._owner is not None:
                self._owner.cancel()

    async def validate(self) -> None:
        """Run readiness checks on adapters that implement ``SupportsValidation``.

        Raises:
            ConfigurationError: Summarizing the adapters that reported a
                configuration failure.
        """
        self.require_started()
        problems: list[str] = []
        for key in self._build_order:
            service = self._services[key]
            if isinstance(service, SupportsValidation):
                try:
                    await service.validate()
                except ConfigurationError as exc:
                    problems.append(f"{label(key)}: {exc.summary}")
        if problems:
            count = len(problems)
            raise ConfigurationError(
                f"startup validation failed for {count} adapter{'s' if count != 1 else ''}",
                actual=" | ".join(problems),
                fix="run 'agentcraft2 doctor' for one line per adapter with what to do",
            )

    async def check(self) -> tuple[CheckResult, ...]:
        """Return diagnostic results for the built adapters.

        Unlike ``validate``, expected library errors become failed results with
        suggested fixes. Unexpected exceptions still propagate.
        """
        self.require_started()
        results: list[CheckResult] = []
        for key in self._build_order:
            service = self._services[key]
            name = label(key)
            if not isinstance(service, SupportsValidation):
                results.append(CheckResult(name, ok=True, detail="built"))
                continue
            try:
                await service.validate()
            except AgentCraft2Error as problem:
                results.append(CheckResult.failed(name, problem))
            else:
                results.append(CheckResult(name, ok=True, detail="checked"))
        return tuple(results)

    async def aclose(self) -> None:
        """Close adapters in reverse build order. Safe to call more than once.

        Cleanup is attempted for every owned adapter even if an earlier close
        fails or is cancelled. Shared build resources are closed afterwards.
        Concurrent callers await the same teardown. Cancelling a caller waits
        for teardown before propagating cancellation.

        Raises:
            BaseExceptionGroup: Collected cleanup failures when no cancellation
                takes precedence; ordinary errors form an ``ExceptionGroup``.
            asyncio.CancelledError: After cleanup completes if the caller or an
                adapter close was cancelled. Cleanup errors are chained to it.
        """
        if self._closed and (self._owner is None or self._closing is not None):
            return
        if self._closing is None:
            self._stop.set()
            self._closing = (
                self._owner if self._owner is not None else asyncio.create_task(self._close_all())
            )
        cancelled: asyncio.CancelledError | None = None
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except BaseException:  # noqa: BLE001 - retrieved and raised below
                break  # retrieve the error below, preserving a caller's cancellation
        try:
            self._closing.result()
        except BaseException as error:
            if cancelled is not None:
                raise cancelled from error
            raise
        if cancelled is not None:
            raise cancelled

    async def _close_all(self) -> None:
        self._stop.set()  # a later background failure must not interrupt teardown
        errors: list[BaseException] = []
        services: list[object] = list(reversed(self._lifetimes))
        services.extend(
            resource
            for resource in self._resources.take()
            if id(resource) not in self._claimed_adapters
        )
        closed: set[int] = set()
        for service in services:
            if id(service) in closed:
                continue
            closed.add(id(service))
            if isinstance(service, SupportsAsyncClose):
                try:
                    await service.aclose()
                except BaseException as exc:  # noqa: BLE001 - all resources must get their turn
                    errors.append(exc)
        self._services.clear()
        self._build_order.clear()
        self._lifetimes.clear()
        self._claimed_adapters.clear()
        self._closed = True
        if errors:
            cancellation = next((e for e in errors if isinstance(e, asyncio.CancelledError)), None)
            if cancellation is not None:
                raise cancellation from BaseExceptionGroup("errors while closing", errors)
            raise BaseExceptionGroup("errors while closing the service container", errors)

    # ------------------------------------------------------------ construction

    async def _build(self, key: AdapterKey) -> object:
        port, name, instance = key
        if key in self._services:
            return self._services[key]
        if key in self._building:
            cycle = " -> ".join(label(k) for k in [*self._building, key])
            raise ConfigurationError(f"adapters depend on each other in a cycle: {cycle}")
        spec = self._providers.lookup(port, name)

        def resolve(dependency: str) -> object:
            if spec.dependencies is not None and dependency not in spec.dependencies:
                raise ConfigurationError(
                    f"{port} ({name}) requested undeclared dependency {dependency!r}"
                )
            return self.resolve_built(dependency)

        context = BuildContext(
            port=port,
            selection=selection_for(self._config, key),
            deployment_env=self._config.deployment_env,
            tls_ca_bundle=self._config.tls_ca_bundle,
            external=self._config.external,
            clock=self._clock,
            ids=self._ids,
            secret_values=self._config.secrets if port == Section.SECRETS.value else {},
            resolver=resolve,
            instance=instance,
            resources=self._resources,
        )
        self._building.append(key)

        async def create() -> object:
            value: Any = spec.factory(context)
            return await value if inspect.isawaitable(value) else value

        lifetime = AdapterLifetime(create, self._claimed_adapters, self._adapter_failed)
        self._lifetimes.append(lifetime)
        try:
            service = await lifetime.start()
        except AgentCraft2Error as error:
            error.add_note(where_configured(key))
            raise
        finally:
            self._building.pop()
        self._services[key] = service
        self._build_order.append(key)
        if spec.contract is not None:
            spec.contract.checked(service)
        return service

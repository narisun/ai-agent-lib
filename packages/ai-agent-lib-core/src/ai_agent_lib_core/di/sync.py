"""A synchronous front to a service container, for code that is not async.

The library is async first. A script, a notebook cell or a WSGI application
that cannot await uses this instead: it starts the container on an event loop
of its own, in a background thread, and waits for each call::

    with SyncServices(load_service_config(ENV_FILE)) as services:
        graph = build_graph(services.container)
        context = services.authenticate(None, application=APPLICATION, thread_id="t-1")
        result = services.invoke(graph, {"messages": [HumanMessage("Hi")]}, context)
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self, TypeVar

from langchain_core.tools import BaseTool

from ai_agent_lib_core.contracts import RequestContext, ServiceConfig
from ai_agent_lib_core.di.container import ServiceContainer
from ai_agent_lib_core.di.providers import ServiceProviders

__all__ = ["SyncServices"]

T = TypeVar("T")


class SyncServices:
    """Runs a :class:`ServiceContainer` on a loop of its own and waits for each call.

    Args:
        config: The resolved configuration.
        providers: The adapter registry. Defaults to the local adapters.
        **container: Anything else :class:`ServiceContainer` takes, such as a clock.
    """

    def __init__(
        self,
        config: ServiceConfig,
        providers: ServiceProviders | None = None,
        **container: Any,
    ) -> None:
        self._config = config
        self._providers = providers
        self._options = container
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="agentlib-services", daemon=True
        )
        self._container: ServiceContainer | None = None

    @property
    def container(self) -> ServiceContainer:
        """The started container: build graphs and tools from it as usual.

        Raises:
            RuntimeError: If the services are not started.
        """
        if self._container is None:
            raise RuntimeError("the services are not started: use 'with SyncServices(...) as s:'")
        return self._container

    def run(self, make_awaitable: Callable[[], Awaitable[T]]) -> T:
        """Run the awaitable that ``make_awaitable`` returns on the services' loop and wait."""

        async def on_loop() -> T:
            return await make_awaitable()

        return asyncio.run_coroutine_threadsafe(on_loop(), self._loop).result()

    def authenticate(self, credential: str | None, **arguments: Any) -> RequestContext:
        """Verify a caller's credential, as :meth:`ServiceContainer.authenticate` does."""
        return self.run(lambda: self.container.authenticate(credential, **arguments))

    def mcp_tools(self, server: str) -> list[BaseTool]:
        """Return the governed tools of a registered MCP server."""
        return self.run(lambda: self.container.mcp_tools(server))

    def invoke(self, graph: Any, given: Any, context: RequestContext) -> Any:
        """Run a compiled graph for one request on behalf of ``context`` and return its result."""
        return self.run(lambda: graph.ainvoke(given, **self.container.invocation(context)))

    def validate(self) -> None:
        """Ask every adapter that can check itself to do so."""
        self.run(self.container.validate)

    def __enter__(self) -> Self:
        self._thread.start()
        container = ServiceContainer(self._config, self._providers, **self._options)
        try:
            self.run(container.start)
        except BaseException:
            self._stop()
            raise
        self._container = container
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._container is not None:
                self.run(self._container.aclose)
        finally:
            self._container = None
            self._stop()

    def _stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

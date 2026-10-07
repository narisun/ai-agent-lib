"""The contract suite for checkpoint backends.

It lives apart from the other suites because it needs LangGraph. Use it the
same way: subclass it in a test module, name the subclass ``Test...`` and
implement :meth:`CheckpointBackendContract.make_backend`.
"""

from __future__ import annotations

import abc
import operator
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ai_agent_lib_core.contracts import (
    CheckpointBackend,
    PolicyDenied,
    Principal,
    RequestContext,
    SupportsAsyncClose,
)
from ai_agent_lib_core.integrations.langgraph import ScopedCheckpointer, scoped_thread_id

__all__ = ["CheckpointBackendContract"]

_ROLE_MARKER = "role-marker-7f3a91"


class _Counter(TypedDict):
    visits: Annotated[int, operator.add]


async def _visit(state: _Counter) -> _Counter:  # noqa: ARG001 - a node receives the state
    return {"visits": 1}


def _context(
    tenant: str = "t-1", subject: str = "u-1", application: str = "app-1"
) -> RequestContext:
    return RequestContext(
        principal=Principal(subject=subject, tenant=tenant, roles=frozenset({_ROLE_MARKER})),
        application=application,
        request_id="r-1",
        thread_id="shared-thread-name",
    )


def _run_args(context: RequestContext) -> dict[str, Any]:
    thread_id = scoped_thread_id(context.scope, context.thread_id)
    return {"config": {"configurable": {"thread_id": thread_id}}, "context": context}


@pytest.mark.asyncio
class CheckpointBackendContract(abc.ABC):
    """What every :class:`CheckpointBackend` must do."""

    @abc.abstractmethod
    async def make_backend(self, tmp_path: Path) -> CheckpointBackend:
        """Return a started, empty backend. ``tmp_path`` is a private directory."""

    @pytest.fixture
    async def backend(self, tmp_path: Path) -> AsyncIterator[CheckpointBackend]:
        """A fresh backend, closed after the test if it can be closed."""
        backend = await self.make_backend(tmp_path)
        yield backend
        if isinstance(backend, SupportsAsyncClose):
            await backend.aclose()

    @staticmethod
    def _graph(backend: CheckpointBackend) -> CompiledStateGraph[Any, Any, Any, Any]:
        checkpointer = backend.checkpointer
        assert isinstance(checkpointer, BaseCheckpointSaver)
        builder = StateGraph(_Counter, context_schema=RequestContext)
        builder.add_node("visit", _visit)
        builder.add_edge(START, "visit")
        builder.add_edge("visit", END)
        return builder.compile(checkpointer=ScopedCheckpointer(checkpointer))

    async def test_a_thread_resumes_with_its_own_state(self, backend: CheckpointBackend) -> None:
        graph = self._graph(backend)
        context = _context()
        assert (await graph.ainvoke({"visits": 0}, **_run_args(context)))["visits"] == 1
        assert (await graph.ainvoke({"visits": 0}, **_run_args(context)))["visits"] == 2

    @pytest.mark.parametrize(
        "other",
        [
            {"tenant": "t-2"},
            {"subject": "u-2"},
            {"application": "app-2"},
            # Identifiers crafted to collide with ("t-1", "u-1") if keys were naively joined.
            {"tenant": "t-1/u-1", "subject": "x"},
        ],
    )
    async def test_state_in_one_scope_is_invisible_from_another(
        self, backend: CheckpointBackend, other: dict[str, str]
    ) -> None:
        graph = self._graph(backend)
        await graph.ainvoke({"visits": 0}, **_run_args(_context()))
        await graph.ainvoke({"visits": 0}, **_run_args(_context()))

        elsewhere = _run_args(_context(**other))
        assert await graph.aget_state(elsewhere["config"]) is not None
        assert (await graph.aget_state(elsewhere["config"])).values == {}
        assert (await graph.ainvoke({"visits": 0}, **elsewhere))["visits"] == 1
        assert (await graph.aget_state(_run_args(_context())["config"])).values == {"visits": 2}

    async def test_an_unscoped_thread_id_is_refused(self, backend: CheckpointBackend) -> None:
        graph = self._graph(backend)
        raw: RunnableConfig = {"configurable": {"thread_id": "shared-thread-name"}}
        with pytest.raises(PolicyDenied) as caught:
            await graph.ainvoke({"visits": 0}, raw, context=_context())
        assert caught.value.reason_code == "unscoped_thread"

    async def test_the_request_context_is_not_stored(self, backend: CheckpointBackend) -> None:
        graph = self._graph(backend)
        args = _run_args(_context())
        await graph.ainvoke({"visits": 0}, **args)
        checkpointer = backend.checkpointer
        assert isinstance(checkpointer, BaseCheckpointSaver)
        stored = await checkpointer.aget_tuple(args["config"])
        assert stored is not None
        assert _ROLE_MARKER not in repr(stored.checkpoint)
        assert _ROLE_MARKER not in repr(stored.metadata)
        assert _ROLE_MARKER not in repr(stored.pending_writes)

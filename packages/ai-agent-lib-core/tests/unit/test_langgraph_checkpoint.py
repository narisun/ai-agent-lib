"""The scoped checkpointer and the SQLite backend."""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ai_agent_lib_core.contracts import (
    ConfigurationError,
    PolicyDenied,
    Principal,
    ProviderSelection,
    RequestContext,
    Scope,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import ServiceContainer, ServiceProviders
from ai_agent_lib_core.integrations.langgraph import (
    ScopedCheckpointer,
    SqliteCheckpointBackend,
    SqliteCheckpointOptions,
    scoped_thread_id,
)
from ai_agent_lib_core.testing import Fakes

SCOPE = Scope(tenant="t-1", subject="u-1", application="app")
ROLE_MARKER = "role-marker-51c0de"


class Counter(TypedDict):
    visits: Annotated[int, operator.add]


async def visit(state: Counter) -> Counter:
    return {"visits": 1}


def context(tenant: str = "t-1") -> RequestContext:
    return RequestContext(
        principal=Principal(subject="u-1", tenant=tenant, roles=frozenset({ROLE_MARKER})),
        application="app",
        request_id="r-1",
        thread_id="conversation",
    )


def counter_graph() -> StateGraph[Counter, RequestContext, Counter, Counter]:
    builder = StateGraph(Counter, context_schema=RequestContext)
    builder.add_node("visit", visit)
    builder.add_edge(START, "visit")
    builder.add_edge("visit", END)
    return builder


# ------------------------------------------------------------- scoped wrapper


def test_the_wrapper_overrides_every_public_checkpointer_method() -> None:
    """A LangGraph upgrade that adds a method must not bypass the scope check."""
    public = {
        name
        for name, member in vars(BaseCheckpointSaver).items()
        if not name.startswith("_") and (callable(member) or isinstance(member, property))
    }
    missing = sorted(public - set(vars(ScopedCheckpointer)))
    assert not missing, f"ScopedCheckpointer must override: {missing}"


def test_scoped_thread_ids_embed_the_scope_and_round_trip() -> None:
    thread_id = scoped_thread_id(SCOPE, "conversation/1")
    assert Scope.parse_key(thread_id) == (SCOPE, ("thread", "conversation/1"))


@pytest.mark.parametrize(
    "thread_id",
    ["conversation", "", None, 42, "t-1/u-1/app", "t-1/u-1/app/memory/conversation"],
)
async def test_anything_but_a_scoped_thread_id_is_refused(thread_id: object) -> None:
    scoped = ScopedCheckpointer(InMemorySaver())
    config = {"configurable": {"thread_id": thread_id}}
    with pytest.raises(PolicyDenied):
        await scoped.aget_tuple(config)  # type: ignore[arg-type]
    with pytest.raises(PolicyDenied):
        scoped.get_tuple(config)  # type: ignore[arg-type]
    with pytest.raises(PolicyDenied):
        await scoped.adelete_thread(thread_id)  # type: ignore[arg-type]


async def test_listing_across_threads_is_refused() -> None:
    scoped = ScopedCheckpointer(InMemorySaver())
    with pytest.raises(PolicyDenied) as caught:
        [item async for item in scoped.alist(None)]
    assert caught.value.reason_code == "unscoped_listing"
    with pytest.raises(PolicyDenied):
        list(scoped.list(None))


async def test_scoped_operations_are_delegated() -> None:
    inner = InMemorySaver()
    scoped = ScopedCheckpointer(inner)
    assert scoped.inner is inner
    assert scoped.serde is inner.serde
    graph = counter_graph().compile(checkpointer=scoped)
    thread_id = scoped_thread_id(SCOPE, "conversation")
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke({"visits": 0}, config, context=context())
    history = [item async for item in scoped.alist(config)]
    assert len(history) >= 2
    await scoped.adelete_thread(thread_id)
    assert await scoped.aget_tuple(config) is None


# -------------------------------------------------------------------- SQLite


async def test_sqlite_state_survives_a_restart_and_never_holds_the_context(
    tmp_path: Path,
) -> None:
    path = tmp_path / "checkpoints.sqlite"
    ctx = context()
    run: dict[str, Any] = {
        "config": {"configurable": {"thread_id": scoped_thread_id(ctx.scope, ctx.thread_id)}},
        "context": ctx,
    }

    first = SqliteCheckpointBackend(SqliteCheckpointOptions(path=path))
    await first.start()
    graph = counter_graph().compile(checkpointer=ScopedCheckpointer(first.checkpointer))
    assert (await graph.ainvoke({"visits": 0}, **run))["visits"] == 1
    await first.aclose()
    await first.aclose()

    second = SqliteCheckpointBackend(SqliteCheckpointOptions(path=path))
    await second.start()
    graph = counter_graph().compile(checkpointer=ScopedCheckpointer(second.checkpointer))
    assert (await graph.ainvoke({"visits": 0}, **run))["visits"] == 2
    await second.aclose()

    stored_bytes = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert ROLE_MARKER.encode() not in stored_bytes


async def test_sqlite_backend_must_be_started_and_reports_a_bad_path(tmp_path: Path) -> None:
    backend = SqliteCheckpointBackend(SqliteCheckpointOptions(path=tmp_path / "db.sqlite"))
    assert backend.path == tmp_path / "db.sqlite"
    with pytest.raises(RuntimeError, match="not started"):
        _ = backend.checkpointer

    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory", encoding="utf-8")
    bad = SqliteCheckpointBackend(SqliteCheckpointOptions(path=blocked / "db.sqlite"))
    with pytest.raises(ConfigurationError, match="cannot be opened"):
        await bad.start()


async def test_the_default_registry_serves_sqlite_checkpoints_locally_only(tmp_path: Path) -> None:
    assert ServiceProviders.default().lookup(Section.CHECKPOINT, "sqlite").local_only is True

    providers = ServiceProviders.default()
    fake_specs = Fakes().providers()
    for section in Section:
        providers.register(section, "fake", fake_specs.lookup(section, "fake").factory)
    config = ServiceConfig.for_testing().with_section(
        Section.CHECKPOINT, ProviderSelection("sqlite", {"path": str(tmp_path / "cp.sqlite")})
    )
    async with ServiceContainer(config, providers) as services:
        graph = counter_graph().compile(**services.compile_kwargs())
        result = await graph.ainvoke({"visits": 0}, **services.invocation(context()))
        assert result["visits"] == 1
    assert (tmp_path / "cp.sqlite").exists()

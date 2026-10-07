"""Every checkpointer method is delegated for scoped threads and refused otherwise."""

from __future__ import annotations

import inspect
from typing import Any
from unittest import mock

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver

from ai_agent_lib_core.contracts import PolicyDenied, Scope
from ai_agent_lib_core.integrations.langgraph import ScopedCheckpointer, scoped_thread_id

SCOPED = scoped_thread_id(Scope("t-1", "u-1", "app"), "conversation")
OTHER = scoped_thread_id(Scope("t-1", "u-1", "app"), "copy")
RAW = "conversation"


def config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


# method name -> (positional arguments, keyword arguments) for a given thread ID.
CALLS: dict[str, Any] = {
    "get": lambda t: ((config(t),), {}),
    "get_tuple": lambda t: ((config(t),), {}),
    "put": lambda t: ((config(t), {"id": "c"}, {"step": 1}, {"messages": 1}), {}),
    "put_writes": lambda t: ((config(t), [("messages", "x")], "task-1", "path"), {}),
    "delete_thread": lambda t: ((t,), {}),
    "copy_thread": lambda t: ((t, OTHER), {}),
    "prune": lambda t: (([t],), {"strategy": "keep_latest"}),
    "get_delta_channel_history": lambda t: ((), {"config": config(t), "channels": ["messages"]}),
}
CALLS |= {f"a{name}": build for name, build in list(CALLS.items())}


def stub() -> mock.Mock:
    inner = mock.create_autospec(BaseCheckpointSaver, instance=True)
    inner.serde = object()
    return inner  # type: ignore[no-any-return]


async def call(target: object, name: str, thread_id: str) -> Any:
    args, kwargs = CALLS[name](thread_id)
    result = getattr(target, name)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_a_scoped_call_is_delegated_with_the_same_arguments(name: str) -> None:
    inner = stub()
    await call(ScopedCheckpointer(inner), name, SCOPED)
    args, kwargs = CALLS[name](SCOPED)
    getattr(inner, name).assert_called_once_with(*args, **kwargs)


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_an_unscoped_call_is_refused_and_never_reaches_the_store(name: str) -> None:
    inner = stub()
    with pytest.raises(PolicyDenied) as caught:
        await call(ScopedCheckpointer(inner), name, RAW)
    assert caught.value.reason_code == "unscoped_thread"
    getattr(inner, name).assert_not_called()


async def test_copying_into_an_unscoped_thread_is_refused() -> None:
    inner = stub()
    scoped = ScopedCheckpointer(inner)
    with pytest.raises(PolicyDenied):
        scoped.copy_thread(SCOPED, RAW)
    with pytest.raises(PolicyDenied):
        await scoped.acopy_thread(SCOPED, RAW)
    inner.copy_thread.assert_not_called()
    inner.acopy_thread.assert_not_called()


async def test_run_scoped_and_versioning_calls_are_passed_through() -> None:
    inner = stub()
    scoped = ScopedCheckpointer(inner)

    scoped.delete_for_runs(["run-1"])
    await scoped.adelete_for_runs(["run-2"])
    inner.delete_for_runs.assert_called_once_with(["run-1"])
    inner.adelete_for_runs.assert_called_once_with(["run-2"])

    inner.get_next_version.return_value = 2
    assert scoped.get_next_version(1, None) == 2
    inner.get_next_version.assert_called_once_with(1, None)

    assert scoped.config_specs is inner.config_specs


def test_a_wider_allowlist_stays_scoped() -> None:
    inner = stub()
    widened = ScopedCheckpointer(inner).with_allowlist([("my", "module")])
    assert isinstance(widened, ScopedCheckpointer)
    assert widened.inner is inner.with_allowlist.return_value
    inner.with_allowlist.assert_called_once_with([("my", "module")])


def test_the_delegation_table_covers_every_thread_addressed_method() -> None:
    """A new thread-addressed method on the base class must be added to the table."""
    thread_addressed = {
        name
        for name, member in vars(BaseCheckpointSaver).items()
        if not name.startswith("_")
        and callable(member)
        and {"config", "thread_id", "thread_ids", "source_thread_id"}
        & set(inspect.signature(member).parameters)
    }
    listing = {"list", "alist"}  # covered by the listing tests
    assert thread_addressed - listing == set(CALLS)

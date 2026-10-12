"""Prove that cancelling a UI waiter cannot cancel an admitted HTTP task."""

import asyncio

import httpx
import pytest
from spike.application import Runs
from spike.settings import Settings
from spike.store import Store


async def test_cancelled_waiter_preserves_task_and_persists_artifacts(tmp_path, monkeypatch):
    store = Store(tmp_path / "task.sqlite")
    store.update_thread("demo/alice", "thread", metadata={"chat_profile": "rmprep-demo"})
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "output": {
                    "answer": "Ready",
                    "memo": "# Memo",
                    "metrics": {"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0},
                }
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "spike.application.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    runs = Runs(store, Settings.local(tmp_path))
    waiter = asyncio.create_task(runs.submit("demo/alice", "thread", "run", "Question"))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not runs.tasks["run"].cancelled()
    release.set()
    await runs.wait("demo/alice", "thread")
    assert store.run("demo/alice", "run")["status"] == "completed"
    assert len(store.thread("demo/alice", "thread")["elements"]) == 2
    await runs.submit("demo/alice", "thread", "run", "Question")
    assert len(calls) == 1

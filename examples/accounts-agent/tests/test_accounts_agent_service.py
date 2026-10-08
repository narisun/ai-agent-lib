"""The reference agent behind its HTTP entry point, tested offline.

Most tests call the application in process. The last one serves it for real on
a Unix socket and stops it while a request is running.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from accounts_agent import APPLICATION
from accounts_agent.graph import build_graph
from accounts_agent.service import build_app
from ai_agent_lib_core import Principal, RequestContext
from ai_agent_lib_core.integrations.http import ServiceLifecycle, ServiceState, serve
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import FakeChatModelProvider, FakeIdentityVerifier, Fakes, calls_tool

ANN = {"authorization": "Bearer ann-token"}
BO = {"authorization": "Bearer bo-token"}


def fakes(*replies: Any) -> Fakes:
    return Fakes(
        model=FakeChatModelProvider(list(replies)),
        identity=FakeIdentityVerifier(
            {
                "ann-token": Principal(subject="ann", tenant="t-9"),
                "bo-token": Principal(subject="bo", tenant="t-9"),
            }
        ),
    )


def ask(question: str, thread: str = "th-1") -> dict[str, Any]:
    return {"input": {"question": question}, "thread_id": thread}


async def test_a_signed_in_caller_gets_an_answer_and_the_run_is_audited_as_theirs() -> None:
    lookup = calls_tool("lookup_balance", account="4411")
    kit = fakes(lookup, "It is 1,250.00 USD.")
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
            reply = await http.post("/invoke", json=ask("Balance of 4411?"), headers=ANN)
            refused = await http.post("/invoke", json=ask("Balance of 4411?"))
            malformed = await http.post("/invoke", json={"input": "just text"}, headers=ANN)

    assert reply.status_code == 200
    assert reply.json()["output"] == {"answer": "It is 1,250.00 USD."}
    assert (refused.status_code, refused.json()["reason"]) == (401, "identity_untrusted")
    assert (malformed.status_code, malformed.json()["error"]) == (400, "invalid")
    events = [(record.event, record.subject) for record in kit.audit.records]
    assert events[:3] == [("model.call", "ann"), ("tool.call", "ann"), ("model.call", "ann")]
    # The refused request is on record too, with no caller to name.
    assert events[3] == ("request.authenticate", "unknown")
    assert {record.application for record in kit.audit.records} == {APPLICATION}


async def test_a_conversation_continues_per_caller_across_requests() -> None:
    kit = fakes("one", "two", "three")
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
            await http.post("/invoke", json=ask("first question"), headers=ANN)
            await http.post("/invoke", json=ask("second question"), headers=ANN)
            # The same thread name from another caller is another conversation.
            await http.post("/invoke", json=ask("someone else's question"), headers=BO)

    prompts = kit.model.models[0].calls
    assert [m.content for m in prompts[1][1:]] == ["first question", "one", "second question"]
    assert [m.content for m in prompts[2][1:]] == ["someone else's question"]


async def test_a_streamed_run_sends_each_step_then_the_end() -> None:
    lookup = calls_tool("lookup_balance", account="4411")
    kit = fakes(lookup, "It is 1,250.00 USD.")
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
            reply = await http.post("/invoke/stream", json=ask("Balance of 4411?"), headers=ANN)

    assert reply.headers["content-type"].startswith("text/event-stream")
    events = [
        (block.split("\n")[0].removeprefix("event: "), json.loads(block.split("data: ", 1)[1]))
        for block in reply.text.strip().split("\n\n")
    ]
    names = [name for name, _ in events]
    assert names[-1] == "end"
    assert set(names[:-1]) == {"update"}
    nodes = [data["node"] for name, data in events if name == "update"]
    assert "tools" in nodes
    assert events[-2][1]["messages"][-1]["content"] == "It is 1,250.00 USD."
    assert events[-1][1]["thread_id"] == "th-1"


@pytest.fixture
def socket_path() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="eap-agent-")
    yield str(Path(directory) / "s")
    shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="needs Unix sockets")
async def test_a_run_in_flight_finishes_and_is_checkpointed_before_the_service_stops(
    socket_path: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    kit = fakes("a late answer")
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        stop = asyncio.Event()
        serving = asyncio.ensure_future(
            serve(app, lifecycle, uds=socket_path, stop=stop, drain_seconds=0)
        )
        while not lifecycle.ready:
            await asyncio.sleep(0.01)

        original = services.audit.write

        async def slow_write(record: Any) -> None:
            # Hold the run at its first audit record, as a slow model call would.
            if record.event == "model.call" and not entered.is_set():
                entered.set()
                await release.wait()
            await original(record)

        services.audit.write = slow_write  # type: ignore[method-assign]

        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
            running = asyncio.ensure_future(
                http.post("/invoke", json=ask("a slow question", "th-slow"), headers=ANN)
            )
            await entered.wait()
            stop.set()
            while lifecycle.state is not ServiceState.STOPPING:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            assert not serving.done()

            release.set()
            reply = await running
            await asyncio.wait_for(serving, 10)

        assert reply.json()["output"] == {"answer": "a late answer"}
        # The run reached its checkpoint: the whole turn is in the thread's state.
        context = RequestContext(
            principal=Principal(subject="ann", tenant="t-9"),
            application=APPLICATION,
            request_id="r-check",
            thread_id="th-slow",
        )
        # Reading state outside a run names the caller; only their own thread is readable.
        with bind_request_context(context):
            state = await build_graph(services).aget_state(services.invocation(context)["config"])
        assert [m.content for m in state.values["messages"]] == [
            "a slow question",
            "a late answer",
        ]

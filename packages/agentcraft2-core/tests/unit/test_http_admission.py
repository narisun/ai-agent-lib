"""Independent HTTP callers cannot fork one thread or outlive its admission guard."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import START, MessagesState, StateGraph
from starlette.requests import ClientDisconnect
from starlette.types import Message

from agentcraft2_core import RequestContext, ServiceConfig
from agentcraft2_core.contracts import Principal
from agentcraft2_core.integrations.http import InMemoryThreadAdmission, ServiceLifecycle, agent_app
from agentcraft2_core.pipeline import bind_request_context
from agentcraft2_core.testing import FakeIdentityVerifier, Fakes

PRINCIPAL = Principal(subject="ann", tenant="local")
CONTEXT = RequestContext(principal=PRINCIPAL, application="test", thread_id="one", request_id="a")
TRANSPORTS = ["json", "live", "legacy"]


async def ready() -> None:
    pass


async def post(client: httpx.AsyncClient, transport: str, request_id: str) -> httpx.Response:
    return await client.post(
        "/invoke/stream" if transport == "legacy" else "/invoke",
        json={"input": request_id, "thread_id": "one"},
        headers={
            "authorization": "Bearer test-token",
            "x-request-id": request_id,
            "accept": "text/event-stream" if transport == "live" else "application/json",
        },
    )


class Journal:
    """In-memory journal with an optional barrier at the durable-write boundary."""

    def __init__(self) -> None:
        self.begun: set[str] = set()
        self.saved: dict[str, dict[str, object]] = {}
        self.finishing = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def begin(self, context: RequestContext) -> bool:
        if context.request_id in self.begun:
            return False
        self.begun.add(context.request_id)
        return True

    async def finish(self, context: RequestContext, envelope: dict[str, object]) -> None:
        self.finishing.set()
        await self.release.wait()
        self.saved[context.request_id] = envelope

    async def read(self, context: RequestContext, request_id: str) -> dict[str, object] | None:
        return self.saved.get(request_id)


@pytest.mark.parametrize("first_transport", TRANSPORTS)
@pytest.mark.parametrize("second_transport", TRANSPORTS)
async def test_overlapping_turn_rejected_without_reservation_then_preserves_history(
    first_transport: str,
    second_transport: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    journal = Journal()
    fakes = Fakes(identity=FakeIdentityVerifier({"test-token": PRINCIPAL}))
    async with fakes.container(ServiceConfig.for_testing()) as services:

        async def answer(state: MessagesState) -> dict[str, Any]:
            if state["messages"][-1].content == "a":
                entered.set()
                await release.wait()
            return {"messages": [AIMessage(content="accepted")]}

        builder = StateGraph(MessagesState)
        builder.add_node("answer", answer)
        builder.add_edge(START, "answer")
        graph = builder.compile(**services.compile_kwargs())

        async def run(context: RequestContext, given: Any) -> dict[str, str]:
            await graph.ainvoke(
                {"messages": [HumanMessage(content=given)]}, **services.invocation(context)
            )
            return {"answer": "accepted"}

        async def stream(context: RequestContext, given: Any) -> AsyncIterator[Any]:
            yield await run(context, given)

        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(
            services,
            run,
            application="test",
            lifecycle=lifecycle,
            stream=stream,
            live_updates=True,
            outcomes=journal,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(post(client, first_transport, "a"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                busy = await asyncio.wait_for(post(client, second_transport, "b"), 3)
                assert busy.status_code == 409
                assert busy.json() == {"error": "thread_busy", "request_id": "b"}
                assert journal.begun == {"a"}
            finally:
                release.set()
                first = await asyncio.wait_for(task, 3)
            assert first.status_code == 200
            assert (await post(client, second_transport, "b")).status_code == 200
            duplicate = await post(client, first_transport, "a")
            assert duplicate.status_code == 503
            with bind_request_context(CONTEXT):
                state = await graph.aget_state(services.invocation(CONTEXT)["config"])
            assert [m.content for m in state.values["messages"]] == [
                "a",
                "accepted",
                "b",
                "accepted",
            ]


@pytest.mark.parametrize("transport", ["json", "live"])
async def test_guard_covers_outcome_commit(transport: str) -> None:
    journal = Journal()
    journal.release.clear()
    fakes = Fakes(identity=FakeIdentityVerifier({"test-token": PRINCIPAL}))
    async with fakes.container() as services:

        async def run(context: RequestContext, given: Any) -> dict[str, str]:
            return {"answer": "accepted"}

        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(
            services,
            run,
            application="test",
            lifecycle=lifecycle,
            live_updates=True,
            outcomes=journal,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            task = asyncio.create_task(post(client, transport, "a"))
            try:
                await asyncio.wait_for(journal.finishing.wait(), 3)
                assert (await post(client, "json", "b")).status_code == 409
            finally:
                journal.release.set()
                await asyncio.wait_for(task, 3)
            assert journal.saved["a"]["output"] == {"answer": "accepted"}
            assert (await post(client, "json", "b")).status_code == 200


async def test_scope_isolates_owners_but_role_and_request_changes_do_not_bypass_guard() -> None:
    admission = InMemoryThreadAdmission()
    variants = [
        replace(CONTEXT, thread_id="two"),
        replace(CONTEXT, application="other"),
        replace(CONTEXT, principal=Principal(subject="bo", tenant="local")),
        replace(CONTEXT, principal=Principal(subject="ann", tenant="other")),
    ]
    async with admission.hold(CONTEXT) as acquired:
        assert acquired
        for other in variants:
            async with admission.hold(other) as allowed:
                assert allowed
        changed = replace(
            CONTEXT,
            request_id="b",
            principal=Principal(subject="ann", tenant="local", roles=frozenset({"admin"})),
        )
        async with admission.hold(changed) as allowed:
            assert not allowed
    async with admission.hold(CONTEXT) as acquired:
        assert acquired


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_injected_guard_coordinates_apps_and_waits_for_cancel_cleanup(transport: str) -> None:
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    admission = InMemoryThreadAdmission()
    fakes = Fakes(identity=FakeIdentityVerifier({"test-token": PRINCIPAL}))
    async with fakes.container() as services:

        async def run(context: RequestContext, given: Any) -> dict[str, str]:
            if given == "a":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return {"answer": "accepted"}

        async def stream(context: RequestContext, given: Any) -> AsyncIterator[Any]:
            yield await run(context, given)

        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        apps = [
            agent_app(
                services,
                run,
                application="test",
                lifecycle=lifecycle,
                live_updates=True,
                stream=stream,
                admission=admission,
            )
            for _ in range(2)
        ]
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(apps[0]), base_url="http://test"
            ) as one,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(apps[1]), base_url="http://test"
            ) as two,
        ):
            task = asyncio.create_task(post(one, transport, "a"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert (await post(two, "json", "b")).status_code == 409
                task.cancel()
                await asyncio.wait_for(cleaning.wait(), 3)
                assert (await post(two, "json", "b")).status_code == 409
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            assert (await post(two, "json", "b")).status_code == 200


@pytest.mark.parametrize("transport", ["live", "legacy"])
@pytest.mark.parametrize("fail_at_start", [False, True])
@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_broken_stream_send_closes_graph_before_releasing_admission(
    transport: str,
    fail_at_start: bool,
    spec_version: str,
) -> None:
    admission = InMemoryThreadAdmission()
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fakes = Fakes(identity=FakeIdentityVerifier({"test-token": PRINCIPAL}))
    async with fakes.container() as services:

        async def run(context: RequestContext, given: Any) -> dict[str, str]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
            return {"answer": "unreachable"}

        async def stream(context: RequestContext, given: Any) -> AsyncIterator[Any]:
            entered.set()
            try:
                yield {"partial": "draft"}
            finally:
                cleaning.set()
                await release.wait()

        received = False

        async def receive() -> Message:
            nonlocal received
            if received:
                await asyncio.Event().wait()
            received = True
            return {
                "type": "http.request",
                "body": json.dumps({"input": "a", "thread_id": "one"}).encode(),
                "more_body": False,
            }

        async def send(message: Message) -> None:
            target = "http.response.start" if fail_at_start else "http.response.body"
            if message["type"] == target:
                if not fail_at_start:
                    await asyncio.wait_for(entered.wait(), 3)
                raise OSError("connection closed")

        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(
            services,
            run,
            application="test",
            lifecycle=lifecycle,
            live_updates=True,
            stream=stream,
            admission=admission,
        )
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/invoke" if transport == "live" else "/invoke/stream",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer test-token"),
                (b"x-request-id", b"a"),
                (b"accept", b"text/event-stream"),
            ],
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            if not fail_at_start:
                await asyncio.wait_for(cleaning.wait(), 3)
                async with admission.hold(CONTEXT) as allowed:
                    assert not allowed
        finally:
            release.set()
            with pytest.raises((OSError, ClientDisconnect)):
                await asyncio.wait_for(task, 3)
        assert entered.is_set() != fail_at_start
        async with admission.hold(CONTEXT) as allowed:
            assert allowed

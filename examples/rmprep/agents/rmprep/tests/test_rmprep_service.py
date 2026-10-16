"""The agent behind its HTTP entry point, called in process with no network."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ai_agent_lib_core import Principal
from ai_agent_lib_core.integrations.http import ServiceLifecycle
from ai_agent_lib_core.testing import FakeChatModelProvider, FakeIdentityVerifier, Fakes
from langchain_core.messages import AIMessage

from rmprep.service import build_app

ADA = {"authorization": "Bearer ada-token"}


async def test_inspector_can_be_composed_before_container_startup() -> None:
    kit = fakes("Hello there.")
    services = kit.container()

    async def start() -> None:
        await services.start()
        await services.validate()

    lifecycle = ServiceLifecycle(start)
    try:
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://agent.test"
        ) as client:
            answer = await client.post("/invoke", json=question("Hello?"), headers=ADA)
            request_id = answer.json()["request_id"]
            inspected = await client.get(
                f"/requests/{request_id}/inspection?thread_id=th-1", headers=ADA
            )
            assert inspected.status_code == 200
            assert inspected.json()["checkpoints"]["record_count"] > 0
            assert inspected.json()["model_inputs"]["availability"] == "disabled"
    finally:
        await services.aclose()


def fakes(*replies: Any) -> Fakes:
    return Fakes(
        model=FakeChatModelProvider(list(replies)),
        identity=FakeIdentityVerifier({"ada-token": Principal(subject="ada", tenant="t-1")}),
    )


def question(text: str, thread: str = "th-1") -> dict[str, Any]:
    return {"input": {"question": text}, "thread_id": thread}


async def test_a_signed_in_caller_gets_an_answer() -> None:
    kit = fakes("Hello there.")
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
            reply = await http.post("/invoke", json=question("Hello?"), headers=ADA)
            ready = await http.get("/readyz")

    assert reply.status_code == 200
    assert reply.json()["output"] == {"answer": "Hello there."}
    assert ready.status_code == 200
    assert kit.audit.records[0].subject == "ada"


async def test_a_streamed_run_sends_each_step_then_the_end() -> None:
    kit = fakes("Hello there.")
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
            reply = await http.post("/invoke/stream", json=question("Hello?"), headers=ADA)

    # Server-sent events: "event: <name>" then "data: <json>", a blank line between.
    events = [block.split("\n", 1) for block in reply.text.strip().split("\n\n")]
    names = [name.removeprefix("event: ") for name, _ in events]
    data = [json.loads(line.removeprefix("data: ")) for _, line in events]
    assert names == ["update", "update", "end"]
    assert data[0]["node"] == "router"
    assert data[0]["route"] == "qnaagent"
    assert data[1]["node"] == "qnaagent/model"
    assert data[1]["messages"][-1]["content"] == "Hello there."
    assert data[2]["thread_id"] == "th-1"


async def test_a_caller_without_a_valid_token_is_refused_and_nothing_runs() -> None:
    kit = fakes("never used")
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
            no_token = await http.post("/invoke", json=question("Hello?"))
            malformed = await http.post("/invoke", json={"input": "just text"}, headers=ADA)

    assert no_token.status_code == 401
    assert (malformed.status_code, malformed.json()["error"]) == (400, "invalid")
    assert all(not model.calls for model in kit.model.models)


@pytest.mark.parametrize("live", [False, True])
async def test_text_blocks_render_as_prose_and_json_envelope_stays_compatible(live: bool) -> None:
    kit = fakes(AIMessage(content=[{"type": "text", "text": "A readable answer."}]))
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(services.validate)
        app = await build_app(services, lifecycle)
        await lifecycle.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://agent.test"
        ) as http:
            response = await http.post(
                "/invoke",
                json=question("Hello?"),
                headers={**ADA, "accept": "text/event-stream" if live else "application/json"},
            )
    if live:
        frame = next(
            frame for frame in response.text.split("\n\n") if frame.startswith("event: result")
        )
        body = json.loads(frame.split("data: ")[1])
        assert body["usage"]["kind"] == "simulated"
    else:
        body = response.json()
        assert set(body) == {"output", "thread_id", "request_id"}
    assert body["output"]["answer"] == "A readable answer."

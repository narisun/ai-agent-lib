"""Live drafts retain policy, audit, tracing and a distinct accepted result."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from ai_agent_lib_core.adapters.model_fake import FakeChatModel, FakeChatModelProvider
from ai_agent_lib_core.contracts import Principal, RequestContext, ServiceConfig, ValidationFailed
from ai_agent_lib_core.contracts.diagnostics import TokenUsage
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app
from ai_agent_lib_core.observability.live import LiveRun, bind_live_run, live_run
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import FakeIdentityVerifier, Fakes

CONTEXT = RequestContext(
    principal=Principal(subject="developer", tenant="local"),
    application="demo",
    thread_id="thread",
    request_id="request",
)


class StreamingModel(FakeChatModel):
    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        yield ChatGenerationChunk(message=AIMessageChunk(content="First "))
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=[
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "text", "text": "second", "index": 0},
                ]
            )
        )
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "input_token_details": {"cache_read": 3, "cache_creation": 1},
                },
                tool_call_chunks=[
                    {"name": "lookup", "args": '{"id":42}', "id": "tool-1", "index": 0}
                ],
            )
        )


class StreamingProvider(FakeChatModelProvider):
    def create(self, model_id: str) -> FakeChatModel:
        return StreamingModel(model_id=model_id)


async def test_native_chunks_preserve_tools_usage_trace_and_governance(tmp_path: Path) -> None:
    seen: list[tuple[int, str, bool]] = []

    async def sink(call: int, text: str, *, reset: bool = False) -> None:
        seen.append((call, text, reset))

    fakes = Fakes(model=StreamingProvider())
    trace = tmp_path / "trace.jsonl"
    live = LiveRun(CONTEXT, sink)
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=trace)) as services:
        with bind_request_context(CONTEXT), bind_live_run(live):
            reply = await services.model().ainvoke([HumanMessage("question")])
    assert seen == [(1, "", True), (1, "First second", False)]
    assert reply.tool_calls[0]["args"] == {"id": 42}
    assert live.usage().input_tokens == 10
    assert live.usage().output_tokens == 2
    assert live.usage().cache_read_tokens == 3
    assert live.usage().kind == "simulated"
    assert len(fakes.audit.records) == 1
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [event["event"] for event in events] == ["model.request", "model.response"]
    assert events[1]["response"]["tool_calls"] == reply.tool_calls
    assert live_run(CONTEXT) is None


@pytest.mark.parametrize("fail", [False, True])
async def test_negotiated_stream_has_only_one_final_envelope_and_aggregates_revisions(
    fail: bool,
) -> None:
    fakes = Fakes(
        model=StreamingProvider(), identity=FakeIdentityVerifier({"test-token": CONTEXT.principal})
    )
    async with fakes.container() as services:

        async def run(context: RequestContext, given: Any) -> dict[str, str]:
            await services.model().ainvoke([HumanMessage("first draft")])
            await services.model().ainvoke([HumanMessage("revise draft")])
            if fail:
                raise ValidationFailed("private failure detail")
            return {"answer": "The application accepted this final answer."}

        async def ready() -> None:
            pass

        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(services, run, application="demo", lifecycle=lifecycle, live_updates=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/invoke",
                json={"input": {}, "thread_id": "thread"},
                headers={
                    "accept": "text/event-stream",
                    "x-request-id": "request",
                    "authorization": "Bearer test-token",
                },
            )
    frames = [part for part in response.text.split("\n\n") if part.startswith("event:")]
    assert len(frames) == 5
    terminal = json.loads(frames[-1].split("data: ")[1])
    assert frames[-1].startswith("event: error" if fail else "event: result")
    assert terminal["usage"]["input_tokens"] == 20
    assert terminal["usage"]["output_tokens"] == 4
    assert terminal["model_calls"] == 2
    assert terminal["thread_id"] == "thread"
    assert "private failure" not in response.text
    assert "private reasoning" not in response.text
    assert "lookup" not in response.text
    assert ("output" in terminal) is not fail


async def test_drafts_arrive_before_completion() -> None:
    from starlette.responses import JSONResponse

    from ai_agent_lib_core.integrations.http.live import live_response

    entered = asyncio.Event()
    release = asyncio.Event()

    async def invoke() -> JSONResponse:
        active = live_run(CONTEXT)
        assert active
        number = await active.begin()
        await active.sink(number, "visible before completion")
        entered.set()
        await release.wait()
        return JSONResponse({"output": {"answer": "final"}})

    response = live_response(CONTEXT, invoke)
    iterator = aiter(response.body_iterator)
    assert await anext(iterator) == ": connected\n\n"
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert "draft" in str(await anext(iterator))
    assert "visible before completion" in str(await anext(iterator))
    release.set()
    assert "result" in str(await anext(iterator))
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


async def test_disconnect_cancels_and_joins_invoke_task() -> None:
    from starlette.responses import JSONResponse

    from ai_agent_lib_core.integrations.http.live import live_response

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def invoke() -> JSONResponse:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return JSONResponse({})

    iterator = aiter(live_response(CONTEXT, invoke).body_iterator)
    await anext(iterator)
    await entered.wait()
    await iterator.aclose()  # type: ignore[attr-defined]
    assert cancelled.is_set()


async def test_usage_keeps_missing_attempts_unknown_and_contexts_separate() -> None:
    async def sink(call: int, text: str, *, reset: bool = False) -> None:
        pass

    live = LiveRun(CONTEXT, sink)
    assert live.usage().input_tokens is None
    await live.begin()
    live.usages.append(
        TokenUsage(source="langchain", kind="reported", input_tokens=10, output_tokens=3)
    )
    assert live.usage().input_tokens == 10
    await live.begin()
    live.usages.append(TokenUsage())
    assert live.usage().input_tokens is None
    with bind_live_run(live):
        assert live_run(CONTEXT) is live
        assert (
            live_run(
                RequestContext(
                    principal=CONTEXT.principal,
                    application="other",
                    thread_id="thread",
                    request_id="request",
                )
            )
            is None
        )


async def test_blocked_output_is_never_delivered_to_live_sink() -> None:
    from ai_agent_lib_core import PolicyDenied
    from ai_agent_lib_core.contracts import GuardrailPoint
    from ai_agent_lib_core.testing import FakeGuardrails

    seen = []

    async def sink(call: int, text: str, *, reset: bool = False) -> None:
        seen.append(text)

    fakes = Fakes(
        model=StreamingProvider(),
        guardrails=FakeGuardrails(
            lambda point, text: "blocked" if point == GuardrailPoint.MODEL_OUTPUT else None
        ),
    )
    live = LiveRun(CONTEXT, sink)
    async with fakes.container() as services:
        with bind_request_context(CONTEXT), bind_live_run(live), pytest.raises(PolicyDenied):
            await services.model().ainvoke([HumanMessage("synthetic")])
    assert not "".join(seen)
    assert live.usage().input_tokens == 10  # Rejected output still costs tokens.


async def test_model_without_output_checks_delivers_tokens_before_provider_finishes() -> None:
    from ai_agent_lib_core.contracts import ModelRef
    from ai_agent_lib_core.integrations.langgraph.model import GovernedChatModel
    from ai_agent_lib_core.pipeline import Pipeline

    visible, release = asyncio.Event(), asyncio.Event()

    class WaitingModel(FakeChatModel):
        async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
            yield ChatGenerationChunk(message=AIMessageChunk(content="Immediate token"))
            await release.wait()
            yield ChatGenerationChunk(message=AIMessageChunk(content=" and final"))

    async def sink(call: int, text: str, *, reset: bool = False) -> None:
        if text == "Immediate token":
            visible.set()

    def unused_bridge() -> Any:
        raise AssertionError("this invocation is asynchronous")

    model = GovernedChatModel.wrap(
        alias="test",
        ref=ModelRef(provider="fake", model_id="test"),
        provider=FakeChatModelProvider(),
        pipeline=Pipeline(),
        bridge=unused_bridge,
        inner=WaitingModel(model_id="test"),
    )
    with bind_request_context(CONTEXT), bind_live_run(LiveRun(CONTEXT, sink)):
        task = asyncio.create_task(model.ainvoke([HumanMessage("synthetic")]))
        try:
            await asyncio.wait_for(visible.wait(), timeout=2)
            assert not task.done()
        finally:
            release.set()
            reply = await task
    assert reply.content == "Immediate token and final"


@pytest.mark.parametrize("pii", ["flag", "off"])
async def test_default_guardrails_stream_before_provider_finishes(pii: str) -> None:
    from ai_agent_lib_core.adapters.guardrails_patterns import (
        PatternGuardrails,
        PatternGuardrailsOptions,
    )
    from ai_agent_lib_core.contracts import Section
    from ai_agent_lib_core.di import ServiceContainer
    from ai_agent_lib_core.pipeline.guardrails import output_preview_check

    visible, release = asyncio.Event(), asyncio.Event()
    seen = []

    class WaitingModel(FakeChatModel):
        async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
            yield ChatGenerationChunk(message=AIMessageChunk(content="Immediate token"))
            await release.wait()
            yield ChatGenerationChunk(message=AIMessageChunk(content=" and final"))

    class WaitingProvider(FakeChatModelProvider):
        def create(self, model_id: str) -> FakeChatModel:
            return WaitingModel(model_id=model_id)

    async def sink(call: int, text: str, *, reset: bool = False) -> None:
        seen.append(text)
        if text == "Immediate token":
            visible.set()

    fakes = Fakes(model=WaitingProvider())
    guard = PatternGuardrails(PatternGuardrailsOptions.model_validate({"pii": pii}))
    providers = fakes.providers().register(
        Section.GUARDRAILS, "fake", lambda _: guard, replace=True
    )
    async with ServiceContainer(fakes.config(), providers) as services:
        with bind_request_context(CONTEXT), bind_live_run(LiveRun(CONTEXT, sink)):
            task = asyncio.create_task(services.model().ainvoke([HumanMessage("synthetic")]))
            try:
                await asyncio.wait_for(visible.wait(), timeout=2)
                assert not task.done()
                assert seen == ["", "Immediate token"]
            finally:
                release.set()
                reply = await task
    assert reply.content == "Immediate token and final"
    assert "".join(seen) == reply.content
    assert output_preview_check() is None
    assert len(fakes.audit.records) == 1


@pytest.mark.parametrize(("pii", "limit", "expected"), [("block", 100, ""), ("flag", 6, "First ")])
async def test_streaming_keeps_blocking_checks_and_size_limits(
    pii: str, limit: int, expected: str
) -> None:
    from ai_agent_lib_core import PolicyDenied
    from ai_agent_lib_core.adapters.guardrails_patterns import (
        PatternGuardrails,
        PatternGuardrailsOptions,
    )
    from ai_agent_lib_core.contracts import Section
    from ai_agent_lib_core.di import ServiceContainer
    from ai_agent_lib_core.pipeline.guardrails import output_preview_check

    class PersonalModel(FakeChatModel):
        async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
            for text in ["First ", "alice@", "example.com"]:
                yield ChatGenerationChunk(message=AIMessageChunk(content=text))

    class PersonalProvider(FakeChatModelProvider):
        def create(self, model_id: str) -> FakeChatModel:
            return PersonalModel(model_id=model_id)

    seen = []

    async def sink(call: int, text: str, *, reset: bool = False) -> None:
        seen.append(text)

    fakes = Fakes(model=PersonalProvider())
    guard = PatternGuardrails(
        PatternGuardrailsOptions.model_validate(
            {
                "pii": pii,
                "max_model_output_chars": limit,
            }
        )
    )
    providers = fakes.providers().register(
        Section.GUARDRAILS, "fake", lambda _: guard, replace=True
    )
    async with ServiceContainer(fakes.config(), providers) as services:
        with (
            bind_request_context(CONTEXT),
            bind_live_run(LiveRun(CONTEXT, sink)),
            pytest.raises(PolicyDenied),
        ):
            await services.model().ainvoke([HumanMessage("synthetic")])
    assert "".join(seen) == expected
    assert output_preview_check() is None


async def test_live_stream_logs_an_unexpected_failure_and_sends_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fakes = Fakes(identity=FakeIdentityVerifier({"test-token": CONTEXT.principal}))
    async with fakes.container() as services:

        async def run(context: RequestContext, given: Any) -> dict[str, str]:
            raise KeyError("private key name")

        async def ready() -> None:
            pass

        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(services, run, application="demo", lifecycle=lifecycle, live_updates=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            with caplog.at_level("ERROR"):
                response = await client.post(
                    "/invoke",
                    json={"input": {}, "thread_id": "thread"},
                    headers={
                        "accept": "text/event-stream",
                        "x-request-id": "request",
                        "authorization": "Bearer test-token",
                    },
                )
    frames = [part for part in response.text.split("\n\n") if part.startswith("event:")]
    assert frames[-1].startswith("event: error")
    terminal = json.loads(frames[-1].split("data: ")[1])
    assert terminal["error"] == "internal"
    assert "private key name" not in response.text
    # The run's own handler logs a bug once; the stream must not swallow it silently either.
    failures = [record for record in caplog.records if record.levelname == "ERROR"]
    assert failures, "an unexpected failure must reach the log"
    assert all(getattr(record, "request_id", None) == "request" for record in failures)

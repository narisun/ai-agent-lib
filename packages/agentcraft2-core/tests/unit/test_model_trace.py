"""Opt-in model diagnostics preserve exchanges without changing governed behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult
from pydantic import BaseModel

from agentcraft2_core.adapters.model_fake import FakeChatModel
from agentcraft2_core.config import ConfigResolver, MappingConfigSource, load_service_config
from agentcraft2_core.contracts import (
    PolicyDenied,
    Principal,
    RequestContext,
    ServiceConfig,
    TransientError,
    ValidationFailed,
)
from agentcraft2_core.di import ServiceContainer
from agentcraft2_core.observability import JsonlModelTraceSink
from agentcraft2_core.pipeline import bind_request_context
from agentcraft2_core.testing import FakeChatModelProvider, Fakes, calls_tool

CONTEXT = RequestContext(
    principal=Principal(subject="ada", tenant="tutorial"),
    application="helper",
    request_id="request-1",
    thread_id="conversation-1",
)


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_trace_configuration_is_opt_in_and_relative_to_the_service_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AC2_MODEL_TRACE_PATH", raising=False)
    assert ConfigResolver(MappingConfigSource({})).resolve().model_trace_path is None
    blank = MappingConfigSource({"AC2_MODEL_TRACE_PATH": "  "})
    assert ConfigResolver(blank).resolve().model_trace_path is None
    dotenv = tmp_path / ".env"
    dotenv.write_text("AC2_MODEL_TRACE_PATH=.agentcraft2/model-trace.jsonl\n", encoding="utf-8")
    assert (
        load_service_config(dotenv).model_trace_path == tmp_path / ".agentcraft2/model-trace.jsonl"
    )
    assert not (tmp_path / ".agentcraft2").exists()


async def test_trace_is_disabled_by_default_and_audit_keeps_content_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fakes = Fakes(model=FakeChatModelProvider(["private reply"]))
    async with fakes.container() as services:
        with bind_request_context(CONTEXT):
            await services.model().ainvoke([HumanMessage("private question")])
        assert services.config.model_trace_path is None
    assert not list(tmp_path.iterdir())
    assert "private question" not in str(fakes.audit.records) + caplog.text
    assert "private reply" not in str(fakes.audit.records) + caplog.text


async def test_trace_captures_complete_messages_tools_and_provider_reply(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    reply = calls_tool("lookup", account="42")
    reply.response_metadata = {"finish_reason": "tool_use"}
    fakes = Fakes(model=FakeChatModelProvider([reply]))
    long_question = "Ada asks: " + "context " * 1000

    def lookup(account: str) -> str:
        """Find an account."""
        return account

    messages = [
        SystemMessage("Use the lookup tool."),
        HumanMessage(long_question),
        AIMessage(
            content="", tool_calls=[{"name": "lookup", "args": {"account": "1"}, "id": "tool-1"}]
        ),
        ToolMessage("previous result", tool_call_id="tool-1"),
    ]
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        model = services.model().bind_tools([lookup], tool_choice="any")
        with bind_request_context(CONTEXT):
            actual = await model.ainvoke(messages, stop=["STOP"])
    assert actual.tool_calls == reply.tool_calls
    request, response = records(path)
    assert request["event"] == "model.request"
    assert request["messages"] == [message.model_dump() for message in messages]
    assert request["tools"][0]["function"]["name"] == "lookup"
    assert (
        request["tools"][0]["function"]["parameters"]["properties"]["account"]["type"] == "string"
    )
    assert request["binding_options"] == {"tool_choice": "any"}
    assert request["invocation_options"] == {"stop": ["STOP"]}
    assert response["event"] == "model.response"
    assert response["response"] == actual.model_dump()
    for key in (
        "call_id",
        "attempt",
        "request_id",
        "thread_id",
        "application",
        "tenant",
        "subject",
    ):
        assert request[key] == response[key]
    assert request["attempt"] == 1
    assert request["thread_id"] == CONTEXT.thread_id
    assert long_question not in str(fakes.audit.records)


async def test_content_redaction_preserves_json_and_token_usage(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    reply = AIMessage(
        content="An answer for Ada.",
        additional_kwargs={
            "api_key": "key-in-metadata",
            "nested": {"Authorization": "Bearer abcdefghijkl"},
        },
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    fakes = Fakes(model=FakeChatModelProvider([reply]))
    prompt = HumanMessage('password="private-pass" and Bearer abcdefghijkl; Ada asks a question.')
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        with bind_request_context(CONTEXT):
            await services.model().ainvoke([prompt])
    text = path.read_text(encoding="utf-8")
    for secret in ("key-in-metadata", "abcdefghijkl", "private-pass"):
        assert secret not in text
    request, response = records(path)
    assert "Ada asks a question." in request["messages"][0]["content"]
    assert response["response"]["additional_kwargs"]["api_key"] == "[redacted]"
    assert response["response"]["usage_metadata"]["input_tokens"] == 10
    assert isinstance(prompt.content, str)
    assert prompt.content.startswith('password="private-pass"')


async def test_retries_have_correlated_requests_and_metadata_only_errors(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    fakes = Fakes(model=FakeChatModelProvider([TransientError("private failure body"), "done"]))
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        with bind_request_context(CONTEXT):
            await services.model().ainvoke([HumanMessage("hello")])
    captured = records(path)
    assert [item["event"] for item in captured] == [
        "model.request",
        "model.error",
        "model.request",
        "model.response",
    ]
    assert [item["attempt"] for item in captured] == [1, 1, 2, 2]
    assert len({item["call_id"] for item in captured}) == 1
    assert captured[1]["error_type"] == "TransientError"
    assert "private failure body" not in path.read_text(encoding="utf-8")
    assert len(fakes.audit.records) == 1


async def test_identity_denial_does_not_capture_a_provider_request(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    async with Fakes().container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        with pytest.raises(PolicyDenied):
            await services.model().ainvoke([HumanMessage("denied prompt")])
    assert not path.exists()


async def test_structured_output_is_captured_before_validation(tmp_path: Path) -> None:
    class Answer(BaseModel):
        total: int

    path = tmp_path / "trace.jsonl"
    fakes = Fakes(model=FakeChatModelProvider([calls_tool("Answer", total="not-an-integer")]))
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        model = services.model().with_structured_output(Answer)
        with bind_request_context(CONTEXT), pytest.raises(ValidationFailed):
            await model.ainvoke([HumanMessage("Count the items.")])
    request, response = records(path)
    assert request["tools"][0]["function"]["name"] == "Answer"
    assert response["response"]["tool_calls"][0]["args"] == {"total": "not-an-integer"}


async def test_an_unwritable_trace_does_not_fail_or_retry_the_model(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    fakes = Fakes(model=FakeChatModelProvider(["done"]))
    async with fakes.container(
        ServiceConfig.for_testing(model_trace_path=blocked / "trace.jsonl")
    ) as services:
        with bind_request_context(CONTEXT):
            reply = await services.model().ainvoke([HumanMessage("private question")])
    assert reply.content == "done"
    assert len(fakes.model.models[0].calls) == 1
    assert caplog.text.count("model_trace.disabled") == 1
    assert "private question" not in caplog.text


async def test_injected_sink_overrides_file_and_supports_synchronous_calls(tmp_path: Path) -> None:
    class Capture:
        def __init__(self) -> None:
            self.events: list[Mapping[str, object]] = []

        async def write(self, record: Mapping[str, object]) -> None:
            self.events.append(record)

    capture = Capture()
    path = tmp_path / "unused.jsonl"
    fakes = Fakes(model=FakeChatModelProvider(["sync reply"]))
    async with ServiceContainer(
        ServiceConfig.for_testing(model_trace_path=path),
        fakes.providers(),
        model_trace=capture,
    ) as services:
        model = services.model()

        def ask() -> AIMessage:
            with bind_request_context(CONTEXT):
                return model.invoke([HumanMessage("hello")])

        assert (await asyncio.to_thread(ask)).content == "sync reply"
    assert [event["event"] for event in capture.events] == ["model.request", "model.response"]
    assert not path.exists()


async def test_file_sink_appends_concurrent_unicode_records(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "trace.jsonl"
    sink = JsonlModelTraceSink(path)
    payload = "\nAda's question: 日本語 " * 500
    await asyncio.gather(*(sink.write({"index": index, "content": payload}) for index in range(20)))
    await JsonlModelTraceSink(path).write({"index": 20, "content": payload})
    captured = records(path)
    assert sorted(item["index"] for item in captured) == list(range(21))
    assert all(item["content"] == payload for item in captured)


async def test_cancellation_is_recorded_and_propagated(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    entered = asyncio.Event()

    class WaitingModel(FakeChatModel):
        async def _agenerate(self, *args: object, **kwargs: object) -> ChatResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("the model should have been cancelled")

    class WaitingProvider(FakeChatModelProvider):
        def create(self, model_id: str) -> FakeChatModel:
            return WaitingModel(model_id=model_id)

    fakes = Fakes(model=WaitingProvider())
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        with bind_request_context(CONTEXT):
            task = asyncio.create_task(services.model().ainvoke([HumanMessage("hello")]))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    request, error = records(path)
    assert request["attempt"] == error["attempt"] == 1
    assert error["event"] == "model.error"
    assert error["error_type"] == "CancelledError"


async def test_concurrent_calls_keep_their_own_context_and_correlation(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    async with Fakes().container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        model = services.model()

        async def ask(index: int) -> None:
            context = replace(CONTEXT, request_id=f"request-{index}", thread_id=f"thread-{index}")
            with bind_request_context(context):
                await model.ainvoke([HumanMessage(f"question-{index}")])

        await asyncio.gather(*(ask(index) for index in range(8)))
    captured = records(path)
    assert len(captured) == 16
    assert len({record["call_id"] for record in captured}) == 8
    for index in range(8):
        request, response = [
            record for record in captured if record["request_id"] == f"request-{index}"
        ]
        assert request["call_id"] == response["call_id"]
        assert request["thread_id"] == response["thread_id"] == f"thread-{index}"
        assert request["messages"][0]["content"] == f"question-{index}"
        assert response["response"]["content"] == f"fake: question-{index}"


async def test_diagnostic_serialization_failure_preserves_provider_behavior(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "trace.jsonl"
    fakes = Fakes(model=FakeChatModelProvider(["done"]))
    async with fakes.container(ServiceConfig.for_testing(model_trace_path=path)) as services:
        # This provider accepts a definition that the diagnostic schema converter cannot read.
        model = services.model().bind_tools([{"vendor_extension": "private definition"}])
        with bind_request_context(CONTEXT):
            assert (await model.ainvoke([HumanMessage("private prompt")])).content == "done"
    assert len(fakes.model.models[0].calls) == 1
    assert "model_trace.disabled" in caplog.text
    assert "private" not in caplog.text
    assert not path.exists()

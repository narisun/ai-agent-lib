"""Usage survives discarded drafts, SDK failures and disabled content capture."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from ai_agent_lib_core.contracts import (
    Principal,
    RequestContext,
    ServiceConfig,
    TransientError,
    ValidationFailed,
)
from ai_agent_lib_core.contracts.diagnostics import (
    AttemptRef,
    DiagnosticEvent,
    DiagnosticLinks,
    ModelAttempt,
    OperationDecision,
    OperationSummary,
    RequestOutcome,
    TokenUsage,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app
from ai_agent_lib_core.observability import Diagnostics, bind_diagnostic_links
from ai_agent_lib_core.observability.usage import normalize_usage
from ai_agent_lib_core.pipeline import bind_request_context
from ai_agent_lib_core.testing import (
    FakeChatModelProvider,
    Fakes,
    FrozenClock,
    SequentialIds,
    calls_tool,
)

CONTEXT = RequestContext(
    principal=Principal(subject="ada", tenant="bank"),
    application="rmprep",
    request_id="request-1",
    thread_id="thread-1",
)


class Capture:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []
        self.fail_attempts = False

    async def write(self, event: DiagnosticEvent) -> None:
        if self.fail_attempts and isinstance(event, ModelAttempt):
            raise OSError("private sink failure")
        self.events.append(event)


def recorder(capture: Capture) -> Diagnostics:
    return Diagnostics(capture, FrozenClock(), SequentialIds())


def test_anthropic_and_langchain_are_normalized_without_double_counting() -> None:
    raw = normalize_usage(
        {
            "input_tokens": 20,
            "output_tokens": 5,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 20,
            "secret": "private",
        },
        source="anthropic",
    )
    normalized = normalize_usage(
        {
            "input_tokens": 100,
            "output_tokens": 5,
            "total_tokens": 105,
            "input_token_details": {"cache_read": 60, "cache_creation": 20},
        },
        source="langchain",
    )
    assert raw.input_tokens == normalized.input_tokens == 100
    assert raw.cache_read_tokens == normalized.cache_read_tokens == 60
    assert raw.cache_write_tokens == normalized.cache_write_tokens == 20
    assert "private" not in raw.model_dump_json()
    assert raw.output_tokens == 5


def test_missing_cache_is_not_zero_and_bad_counts_are_unknown() -> None:
    missing = normalize_usage({"input_tokens": 10, "output_tokens": 2}, source="langchain")
    assert missing.input_tokens == 10
    assert missing.cache_read_tokens is None
    zero = normalize_usage(
        {"input_tokens": 10, "input_token_details": {"cache_read": 0}}, source="langchain"
    )
    assert zero.cache_read_tokens == 0
    assert normalize_usage(None, source="langchain") == TokenUsage()
    bad = normalize_usage({"input_tokens": True, "output_tokens": -1}, source="langchain")
    assert bad.input_tokens is None
    assert bad.output_tokens is None
    assert normalize_usage({"input_tokens": 5}, source="anthropic").input_tokens is None


def test_anthropic_cache_ttl_breakdown_is_not_added_twice() -> None:
    fields: dict[str, object] = {
        "input_tokens": 100,
        "output_tokens": 5,
        "input_token_details": {
            "cache_read": 60,
            "cache_creation": 0,
            "ephemeral_5m_input_tokens": 15,
            "ephemeral_1h_input_tokens": 5,
        },
    }
    usage = normalize_usage(fields, source="langchain")
    assert usage.input_tokens == 100
    assert usage.cache_write_tokens == 20
    assert usage.cache_read_tokens == 60
    assert any(
        item.name == "input_token_details.ephemeral_5m_input_tokens" for item in usage.source_fields
    )


async def test_usage_without_raw_traces_has_explicit_links_and_accepted_operation() -> None:
    capture = Capture()
    diagnostics = recorder(capture)
    reply = calls_tool("lookup", account="private account")
    reply.usage_metadata = {
        "input_tokens": 100,
        "output_tokens": 5,
        "total_tokens": 105,
        "input_token_details": {"cache_read": 60, "cache_creation": 20},
    }
    fakes = Fakes(model=FakeChatModelProvider([reply]))
    async with ServiceContainer(
        ServiceConfig.for_testing(), fakes.providers(), diagnostics=diagnostics
    ) as services:
        with (
            bind_request_context(CONTEXT),
            bind_diagnostic_links(DiagnosticLinks(node="memoagent", checkpoint_id="checkpoint-1")),
        ):
            async with diagnostics.operation(CONTEXT):
                result = await services.model().ainvoke([HumanMessage("private question")])
        assert result.tool_calls
        assert services.config.model_trace_path is None
    usage, decision, summary = capture.events
    assert isinstance(usage, ModelAttempt)
    assert usage.usage.input_tokens == 100
    assert usage.usage.kind == "simulated"
    assert usage.links.node == "memoagent"
    assert usage.links.checkpoint_id == "checkpoint-1"
    assert usage.links.tool_call_ids == (reply.tool_calls[0]["id"],)
    assert isinstance(decision, OperationDecision)
    assert decision.stage == "model_pipeline"
    assert isinstance(summary, OperationSummary)
    assert summary.accepted_output == "invoke.output"
    assert summary.expected_attempts == (usage.ref,)
    assert summary.capture_complete
    assert "private" not in "".join(item.model_dump_json() for item in capture.events)


async def test_rejected_draft_spends_tokens_but_is_not_an_accepted_answer() -> None:
    class Answer(BaseModel):
        total: int

    capture = Capture()
    diagnostics = recorder(capture)
    draft = calls_tool("Answer", total="invalid private draft")
    draft.usage_metadata = {"input_tokens": 40, "output_tokens": 8, "total_tokens": 48}
    fakes = Fakes(model=FakeChatModelProvider([draft]))
    async with ServiceContainer(
        ServiceConfig.for_testing(), fakes.providers(), diagnostics=diagnostics
    ) as services:
        with bind_request_context(CONTEXT), pytest.raises(ValidationFailed):
            async with diagnostics.operation(CONTEXT):
                await services.model().with_structured_output(Answer).ainvoke("private question")
    usage, decision, summary = capture.events
    assert isinstance(usage, ModelAttempt)
    assert usage.outcome == "returned"
    assert usage.usage.output_tokens == 8
    assert isinstance(decision, OperationDecision)
    assert decision.outcome == "rejected"
    assert isinstance(summary, OperationSummary)
    assert summary.outcome == "failed"
    assert summary.accepted_output is None


async def test_sdk_retry_is_a_distinct_attempt_with_unavailable_usage(tmp_path: Path) -> None:
    capture = Capture()
    diagnostics = recorder(capture)
    trace = tmp_path / "trace.jsonl"
    fakes = Fakes(model=FakeChatModelProvider([TransientError("private provider error"), "done"]))
    async with ServiceContainer(
        ServiceConfig.for_testing(model_trace_path=trace),
        fakes.providers(),
        diagnostics=diagnostics,
        sleep=lambda _: asyncio.sleep(0),
    ) as services:
        with bind_request_context(CONTEXT):
            async with diagnostics.operation(CONTEXT):
                await services.model().ainvoke("private question")
    attempts = [event for event in capture.events if isinstance(event, ModelAttempt)]
    assert [event.ref.attempt for event in attempts] == [1, 2]
    assert len({event.ref.call_id for event in attempts}) == 1
    assert attempts[0].outcome == "failed"
    assert attempts[0].usage.input_tokens is None
    assert attempts[0].usage.kind == "unavailable"
    legacy = [json.loads(line) for line in trace.read_text().splitlines()]
    assert {item["schema"] for item in legacy} == {"agentlib.model-trace/v1"}
    assert {item["call_id"] for item in legacy} == {attempts[0].ref.call_id}
    assert "private" not in "".join(item.model_dump_json() for item in capture.events)


async def test_sink_failure_preserves_chat_and_marks_capture_incomplete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture = Capture()
    capture.fail_attempts = True
    diagnostics = recorder(capture)
    fakes = Fakes(model=FakeChatModelProvider(["done"]))
    async with ServiceContainer(
        ServiceConfig.for_testing(), fakes.providers(), diagnostics=diagnostics
    ) as services:
        with bind_request_context(CONTEXT):
            async with diagnostics.operation(CONTEXT):
                assert (await services.model().ainvoke("private question")).content == "done"
    summary = capture.events[-1]
    assert isinstance(summary, OperationSummary)
    assert not summary.capture_complete
    assert len(summary.expected_attempts) == 1
    assert "private" not in caplog.text
    failed = [
        record for record in caplog.records if record.getMessage() == "diagnostics.write_failed"
    ]
    assert failed, "a failed diagnostics write must be logged"
    assert all(record.exc_info is None for record in failed), "sink text must not be logged"
    assert all(getattr(record, "error_type", None) for record in failed)
    assert all(getattr(record, "request_id", None) == CONTEXT.request_id for record in failed)


async def test_parallel_operations_and_sync_bridge_keep_scopes_separate() -> None:
    capture = Capture()
    diagnostics = recorder(capture)
    async with ServiceContainer(
        ServiceConfig.for_testing(), Fakes().providers(), diagnostics=diagnostics
    ) as services:

        async def invoke(index: int) -> None:
            context = replace(CONTEXT, request_id=f"request-{index}", thread_id=f"thread-{index}")
            with bind_request_context(context):
                async with diagnostics.operation(context):
                    await asyncio.to_thread(services.model().invoke, f"question {index}")

        await asyncio.gather(*(invoke(index) for index in range(4)))
    summaries = [item for item in capture.events if isinstance(item, OperationSummary)]
    assert len(summaries) == 4
    for summary in summaries:
        attempts = [
            item.ref
            for item in capture.events
            if isinstance(item, ModelAttempt) and item.request_id == summary.request_id
        ]
        assert summary.expected_attempts == tuple(attempts)
        assert summary.capture_complete


async def test_cancellation_yields_unknown_outcome() -> None:
    capture = Capture()
    diagnostics = recorder(capture)
    with pytest.raises(asyncio.CancelledError):
        async with diagnostics.operation(CONTEXT):
            raise asyncio.CancelledError
    summary = capture.events[-1]
    assert isinstance(summary, OperationSummary)
    assert summary.outcome == "unknown"
    assert summary.accepted_output is None


async def test_cancellation_during_content_capture_preserves_spent_tokens() -> None:
    ready = asyncio.Event()

    class WaitingTrace:
        async def write(self, record: Mapping[str, object]) -> None:
            if record["event"] == "model.response":
                ready.set()
                await asyncio.Event().wait()

    capture = Capture()
    diagnostics = recorder(capture)
    reply = AIMessage(
        content="private",
        usage_metadata={"input_tokens": 40, "output_tokens": 8, "total_tokens": 48},
    )
    fakes = Fakes(model=FakeChatModelProvider([reply]))
    async with ServiceContainer(
        ServiceConfig.for_testing(),
        fakes.providers(),
        diagnostics=diagnostics,
        model_trace=WaitingTrace(),
    ) as services:

        async def invoke() -> None:
            with bind_request_context(CONTEXT):
                async with diagnostics.operation(CONTEXT):
                    await services.model().ainvoke("private question")

        task = asyncio.create_task(invoke())
        await asyncio.wait_for(ready.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    usage, summary = capture.events
    assert isinstance(usage, ModelAttempt)
    assert usage.outcome == "cancelled"
    assert usage.usage.output_tokens == 8
    assert isinstance(summary, OperationSummary)
    assert summary.outcome == "unknown"


async def test_http_legacy_envelope_is_unchanged_with_terminal_capture() -> None:
    class Services:
        ids = SequentialIds()

        async def authenticate(
            self,
            credential: str | None,
            *,
            application: str,
            thread_id: str,
            request_id: str | None = None,
        ) -> RequestContext:
            return replace(
                CONTEXT,
                application=application,
                thread_id=thread_id,
                request_id=request_id or "request-1",
            )

    async def run(context: RequestContext, given: Any) -> Any:
        return {"answer": "done"}

    async def validate() -> None:
        pass

    capture = Capture()
    lifecycle = ServiceLifecycle(validate)
    await lifecycle.start()
    app = agent_app(
        Services(), run, application="rmprep", lifecycle=lifecycle, diagnostics=recorder(capture)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as client:
        reply = await client.post(
            "/invoke", json={"input": {"question": "hello"}, "thread_id": "thread-1"}
        )
    assert reply.json() == {
        "output": {"answer": "done"},
        "request_id": "request-1",
        "thread_id": "thread-1",
    }
    assert reply.status_code == 200
    summary = capture.events[-1]
    assert isinstance(summary, OperationSummary)
    assert summary.accepted_output == "invoke.output"


async def test_unserializable_output_is_not_accepted() -> None:
    from ai_agent_lib_core.integrations.http.agent import _observed_run

    async def run(context: RequestContext, given: Any) -> Any:
        return {"answer": object()}

    capture = Capture()
    with pytest.raises(TypeError):
        await _observed_run(run, recorder(capture))(CONTEXT, {})
    summary = capture.events[-1]
    assert isinstance(summary, OperationSummary)
    assert summary.outcome == "failed"
    assert summary.accepted_output is None


async def test_unresponsive_sink_has_bounded_wait_and_captures_gap() -> None:
    class SlowCapture(Capture):
        async def write(self, event: DiagnosticEvent) -> None:
            if isinstance(event, ModelAttempt):
                await asyncio.Event().wait()
            await super().write(event)

    capture = SlowCapture()
    diagnostics = Diagnostics(capture, FrozenClock(), SequentialIds(), write_timeout_seconds=0.01)
    async with ServiceContainer(
        ServiceConfig.for_testing(), Fakes().providers(), diagnostics=diagnostics
    ) as services:
        with bind_request_context(CONTEXT):
            async with diagnostics.operation(CONTEXT):
                await asyncio.wait_for(services.model().ainvoke("hello"), timeout=3)
    summary = capture.events[-1]
    assert isinstance(summary, OperationSummary)
    assert not summary.capture_complete
    assert summary.outcome == "completed"


async def test_graph_node_is_captured_from_framework_context() -> None:
    from langgraph.graph import END, START, MessagesState, StateGraph

    capture = Capture()
    diagnostics = recorder(capture)
    async with ServiceContainer(
        ServiceConfig.for_testing(), Fakes().providers(), diagnostics=diagnostics
    ) as services:

        async def memoagent(state: MessagesState) -> dict[str, list[AIMessage]]:
            return {"messages": [await services.model().ainvoke(state["messages"])]}

        builder = StateGraph(MessagesState)
        builder.add_node("memoagent", memoagent)
        builder.add_edge(START, "memoagent")
        builder.add_edge("memoagent", END)
        with bind_request_context(CONTEXT):
            async with diagnostics.operation(CONTEXT):
                await builder.compile().ainvoke({"messages": [HumanMessage("hello")]})
    attempt = capture.events[0]
    assert isinstance(attempt, ModelAttempt)
    assert attempt.links.node == "memoagent"
    assert attempt.links.checkpoint_id is None


def test_public_runtime_exports_preserve_their_existing_identity() -> None:
    import ai_agent_lib_core as public
    from ai_agent_lib_core.di import ServiceProviders, SyncServices

    assert public.ServiceContainer is ServiceContainer
    assert public.ServiceProviders is ServiceProviders
    assert public.SyncServices is SyncServices
    assert public.bind_request_context is bind_request_context
    assert set(public.__all__) <= set(dir(public))
    with pytest.raises(AttributeError):
        _ = public.unknown_export


def test_wire_versions_and_journal_crash_uncertainty() -> None:
    outcome = RequestOutcome(
        scope=CONTEXT.scope,
        thread_id=CONTEXT.thread_id,
        request_id=CONTEXT.request_id,
        state="unknown",
        updated_at=FrozenClock().now(),
    )
    assert RequestOutcome.model_validate_json(outcome.model_dump_json(by_alias=True)) == outcome
    with pytest.raises(ValidationError):
        RequestOutcome.model_validate(
            {**outcome.model_dump(by_alias=True), "schema": "agentlib.request-outcome/v2"}
        )
    with pytest.raises(ValidationError):
        AttemptRef(call_id="call-1", attempt=True)

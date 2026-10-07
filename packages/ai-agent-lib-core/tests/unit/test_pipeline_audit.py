"""The audit and identity stages: evidence for every outcome, fail closed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ai_agent_lib_core.contracts import (
    AuditOutcome,
    BudgetExceeded,
    ExecutionPaused,
    IntegrityError,
    PolicyDenied,
    Principal,
    PrincipalKind,
    RequestContext,
)
from ai_agent_lib_core.pipeline import (
    AuditInterceptor,
    Handler,
    IdentityInterceptor,
    ModelCall,
    ModelStage,
    Pipeline,
    ToolCall,
    record_refused_sign_in,
)
from ai_agent_lib_core.testing import (
    FrozenClock,
    InMemoryAuditSink,
    RecordingTelemetry,
    SequentialIds,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def context(deadline: datetime | None = None) -> RequestContext:
    return RequestContext(
        principal=Principal(subject="u-1", tenant="t-9"),
        application="accounts-agent",
        request_id="r-1",
        thread_id="th-1",
        deadline=deadline,
    )


def model_call(ctx: RequestContext | None) -> ModelCall:
    return ModelCall(
        context=ctx,
        alias="default",
        provider="fake",
        model_id="m-1",
        payload=["a confidential prompt"],
    )


class Harness:
    """A model pipeline with the audit and identity stages over fakes."""

    def __init__(self) -> None:
        self.clock = FrozenClock(NOW)
        self.sink = InMemoryAuditSink()
        self.telemetry = RecordingTelemetry()
        audit: AuditInterceptor[ModelCall, str] = AuditInterceptor(
            self.sink,
            self.telemetry,
            self.clock,
            SequentialIds("rec"),
            describe_response=lambda response: {"response_chars": len(response)},
        )
        identity: IdentityInterceptor[ModelCall, str] = IdentityInterceptor(self.clock)
        self.pipeline = (
            Pipeline[ModelStage, ModelCall, str]()
            .with_stage(ModelStage.IDENTITY, identity)
            .with_stage(ModelStage.AUDIT, audit)
        )

    async def run(self, call: ModelCall, provider: Handler[ModelCall, str]) -> str:
        return await self.pipeline.bind(provider)(call)


async def answer(call: ModelCall) -> str:
    return "ok"


async def test_a_successful_call_writes_one_metadata_only_record() -> None:
    harness = Harness()

    async def slow(call: ModelCall) -> str:
        harness.clock.advance(0.25)
        return "answer"

    assert await harness.run(model_call(context()), slow) == "answer"

    (record,) = harness.sink.records
    assert record.record_id == "rec-1"
    assert record.event == "model.call"
    assert record.outcome is AuditOutcome.SUCCESS
    assert record.timestamp == NOW + timedelta(seconds=0.25)
    assert (record.tenant, record.subject, record.application) == ("t-9", "u-1", "accounts-agent")
    assert (record.request_id, record.thread_id) == ("r-1", "th-1")
    assert dict(record.attributes) == {
        "model_alias": "default",
        "model_provider": "fake",
        "model_id": "m-1",
        "response_chars": 6,
        "duration_ms": 250.0,
        "principal_kind": "user",
    }
    assert record.error_type is None
    assert "confidential" not in str(record.to_dict())


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PolicyDenied("no", reason_code="role_missing"), "role_missing"),
        (BudgetExceeded("limit"), None),
        (ExecutionPaused("paused"), None),
    ],
)
async def test_a_refusal_is_recorded_as_denied_and_still_raised(
    error: Exception, reason: str | None
) -> None:
    harness = Harness()

    async def refuse(call: ModelCall) -> str:
        raise error

    with pytest.raises(type(error)):
        await harness.run(model_call(context()), refuse)

    (record,) = harness.sink.records
    assert record.outcome is AuditOutcome.DENIED
    assert record.error_type == type(error).__name__
    assert record.attributes.get("reason_code") == reason


async def test_an_exception_is_recorded_as_failed_and_still_raised() -> None:
    harness = Harness()

    async def explode(call: ModelCall) -> str:
        raise TimeoutError("upstream timed out with secret detail")

    with pytest.raises(TimeoutError):
        await harness.run(model_call(context()), explode)

    (record,) = harness.sink.records
    assert record.outcome is AuditOutcome.FAILED
    assert record.error_type == "TimeoutError"
    assert "secret detail" not in str(record.to_dict())


async def test_a_successful_call_fails_if_its_audit_record_cannot_be_written() -> None:
    harness = Harness()
    harness.sink.fail_with = OSError("disk full")
    with pytest.raises(IntegrityError, match=r"audit record for model\.call"):
        await harness.run(model_call(context()), answer)
    assert harness.telemetry.events == []


async def test_a_failed_call_whose_audit_also_fails_raises_integrity_error() -> None:
    harness = Harness()
    harness.sink.fail_with = OSError("disk full")

    async def explode(call: ModelCall) -> str:
        raise TimeoutError("upstream")

    with pytest.raises(IntegrityError) as caught:
        await harness.run(model_call(context()), explode)
    assert "TimeoutError" in "".join(caught.value.__notes__)


async def test_a_call_without_a_request_context_is_denied_and_audited() -> None:
    harness = Harness()
    reached: list[ModelCall] = []

    async def provider(call: ModelCall) -> str:
        reached.append(call)
        return "ok"

    with pytest.raises(PolicyDenied) as caught:
        await harness.run(model_call(None), provider)

    assert caught.value.reason_code == "identity_missing"
    assert reached == []
    (record,) = harness.sink.records
    assert record.outcome is AuditOutcome.DENIED
    assert (record.tenant, record.subject) == ("unknown", "unknown")
    assert record.attributes["reason_code"] == "identity_missing"


async def test_a_call_past_its_deadline_is_denied() -> None:
    harness = Harness()
    with pytest.raises(PolicyDenied) as caught:
        await harness.run(model_call(context(deadline=NOW)), answer)
    assert caught.value.reason_code == "deadline_exceeded"
    assert await harness.run(model_call(context(deadline=NOW + timedelta(seconds=1))), answer)


async def test_telemetry_gets_an_event_and_a_duration_without_content() -> None:
    harness = Harness()
    await harness.run(model_call(context()), answer)
    expected = {
        "outcome": "success",
        "model_alias": "default",
        "model_provider": "fake",
        "model_id": "m-1",
    }
    assert harness.telemetry.events == [("model.call", expected)]
    assert harness.telemetry.durations == [("model.call", 0.0, expected)]


async def test_a_cancelled_call_is_recorded_and_stays_cancelled() -> None:
    harness = Harness()

    async def cancelled(call: ModelCall) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await harness.run(model_call(context()), cancelled)
    (record,) = harness.sink.records
    assert record.outcome is AuditOutcome.FAILED
    assert record.error_type == "CancelledError"


async def test_cancellation_is_not_masked_by_an_audit_failure() -> None:
    harness = Harness()
    harness.sink.fail_with = OSError("disk full")

    async def cancelled(call: ModelCall) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await harness.run(model_call(context()), cancelled)


async def test_tool_calls_are_audited_without_their_arguments() -> None:
    sink = InMemoryAuditSink()
    audit: AuditInterceptor[ToolCall, str] = AuditInterceptor(
        sink, RecordingTelemetry(), FrozenClock(NOW), SequentialIds()
    )
    call = ToolCall(
        context=context(),
        tool="accounts.lookup",
        arguments={"account": "12345678"},
        read_only=True,
    )

    async def tool(request: ToolCall) -> str:
        return "balance"

    assert await audit(call, tool) == "balance"
    (record,) = sink.records
    assert record.event == "tool.call"
    assert record.attributes["tool"] == "accounts.lookup"
    assert record.attributes["read_only"] is True
    assert "12345678" not in str(record.to_dict())
    with pytest.raises(TypeError):
        call.arguments["account"] = "other"  # type: ignore[index]


async def test_the_record_says_what_kind_of_caller_it_was_and_who_presented_the_request() -> None:
    harness = Harness()
    through_agents = RequestContext(
        principal=Principal(
            subject="svc-9",
            tenant="t-9",
            kind=PrincipalKind.SERVICE,
            delegation_chain=("chat-ui", "agent-a"),
        ),
        application="accounts-mcp",
        request_id="r-1",
        thread_id="th-1",
    )

    await harness.run(model_call(through_agents), answer)
    await harness.run(model_call(context()), answer)

    presented, direct = harness.sink.records
    assert presented.attributes["principal_kind"] == "service"
    assert presented.attributes["actors"] == "chat-ui,agent-a"
    assert direct.attributes["principal_kind"] == "user"
    assert "actors" not in direct.attributes
    # Who presented a request is evidence, not a label for metrics.
    assert all("actors" not in labels for _, labels in harness.telemetry.events)


async def test_a_refused_sign_in_fails_closed_when_it_cannot_be_recorded() -> None:
    sink = InMemoryAuditSink()
    sink.fail_with = OSError("disk full")
    with pytest.raises(IntegrityError, match=r"request\.authenticate"):
        await record_refused_sign_in(
            sink,
            RecordingTelemetry(),
            FrozenClock(NOW),
            SequentialIds("rec"),
            refusal=PolicyDenied("the token has expired", reason_code="credential_expired"),
            application="accounts-agent",
            request_id="r-1",
            thread_id="th-1",
        )

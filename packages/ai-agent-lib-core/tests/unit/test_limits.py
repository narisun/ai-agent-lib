"""Timeouts, retries and budgets: what keeps one request from hanging or looping."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ai_agent_lib_core import Principal, RequestContext, ServiceConfig, bind_request_context
from ai_agent_lib_core.adapters import SystemClock
from ai_agent_lib_core.contracts import (
    BudgetExceeded,
    BudgetLimits,
    CallLimits,
    GuardrailPoint,
    Limits,
    PolicyDenied,
    TransientError,
)
from ai_agent_lib_core.pipeline import (
    BudgetInterceptor,
    BudgetLedger,
    DeadlineInterceptor,
    ModelCall,
    ResilienceInterceptor,
    ToolCall,
)
from ai_agent_lib_core.testing import FakeChatModelProvider, FakeGuardrails, Fakes, FrozenClock


def context(request_id: str = "r-1", deadline: Any = None) -> RequestContext:
    return RequestContext(
        principal=Principal(subject="u-1", tenant="t-1"),
        application="accounts-agent",
        request_id=request_id,
        thread_id="th-1",
        deadline=deadline,
    )


class Flaky:
    """Fails with a transient error the first ``failures`` times, then answers."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def __call__(self, call: object) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientError("throttled")
        return "done"


async def no_wait(seconds: float) -> None:
    waited.append(seconds)


waited: list[float] = []


def resilience(limits: CallLimits, *, tool: bool = False) -> ResilienceInterceptor[Any, Any]:
    return ResilienceInterceptor(
        limits,
        FrozenClock(),
        what="tool call" if tool else "model call",
        variable="EAP_LIMITS",
        may_retry=(lambda call: call.read_only) if tool else None,
        sleep=no_wait,
        jitter=lambda: 1.0,
    )


def model_call(request: RequestContext | None = None) -> ModelCall:
    return ModelCall(context=request or context(), alias="default", provider="p", model_id="m")


async def test_a_transient_failure_is_tried_again_with_growing_waits() -> None:
    waited.clear()
    flaky = Flaky(failures=2)
    stage = resilience(CallLimits(retries=2, backoff_seconds=0.5, max_backoff_seconds=8))
    call = model_call()
    assert await stage(call, flaky) == "done"
    assert flaky.calls == 3
    assert waited == [0.5, 1.0]
    assert call.evidence.facts()["attempts"] == 3


async def test_the_waits_never_exceed_the_longest_wait() -> None:
    waited.clear()
    stage = resilience(CallLimits(retries=4, backoff_seconds=1, max_backoff_seconds=2.5))
    await stage(model_call(), Flaky(failures=4))
    assert waited == [1.0, 2.0, 2.5, 2.5]


async def test_a_tool_that_changes_something_is_never_called_twice() -> None:
    flaky = Flaky(failures=1)
    stage = resilience(CallLimits(retries=3), tool=True)
    with pytest.raises(TransientError):
        await stage(ToolCall(context=context(), tool="transfer", read_only=False), flaky)
    assert flaky.calls == 1
    reader = Flaky(failures=1)
    assert await stage(ToolCall(context=context(), tool="lookup", read_only=True), reader) == "done"


async def test_nothing_is_sent_once_the_requests_deadline_has_passed() -> None:
    clock = FrozenClock()
    flaky = Flaky(failures=5)
    stage: ResilienceInterceptor[Any, Any] = ResilienceInterceptor(
        CallLimits(retries=5), clock, what="model call", sleep=no_wait
    )
    with pytest.raises(PolicyDenied) as caught:
        await stage(model_call(context(deadline=clock.now() - timedelta(seconds=1))), flaky)
    assert caught.value.reason_code == "deadline_exceeded"
    assert flaky.calls == 0


async def test_r7_no_retry_that_would_wait_past_the_deadline() -> None:
    clock = FrozenClock()
    flaky = Flaky(failures=5)
    stage: ResilienceInterceptor[Any, Any] = ResilienceInterceptor(
        CallLimits(retries=5, backoff_seconds=2),
        clock,
        what="model call",
        sleep=no_wait,
        jitter=lambda: 1.0,
    )
    # One second left, and the first wait would be two: the first failure is the answer.
    with pytest.raises(TransientError) as caught:
        await stage(model_call(context(deadline=clock.now() + timedelta(seconds=1))), flaky)
    assert flaky.calls == 1
    assert "the request's deadline left no time to try again" in caught.value.__notes__


async def test_r7_an_attempt_is_cut_at_the_deadline_not_at_its_own_longer_limit() -> None:
    clock = FrozenClock()

    async def slow(call: object) -> str:
        await asyncio.sleep(5)
        return "late"

    stage: ResilienceInterceptor[Any, Any] = ResilienceInterceptor(
        CallLimits(timeout_seconds=60, retries=0), clock, what="model call", sleep=no_wait
    )
    deadline = clock.now() + timedelta(seconds=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(PolicyDenied) as caught:
        await stage(model_call(context(deadline=deadline)), slow)
    assert caught.value.reason_code == "deadline_exceeded"
    assert loop.time() - started < 1


async def test_an_attempt_that_takes_too_long_is_a_transient_failure_that_says_so() -> None:
    async def slow(call: object) -> str:
        await asyncio.sleep(1)
        return "late"

    stage = resilience(CallLimits(timeout_seconds=0.01, retries=0))
    with pytest.raises(TransientError) as caught:
        await stage(model_call(), slow)
    error = caught.value
    assert error.message == "the model call 'm' took longer than its limit"
    assert (error.expected, error.actual) == (
        "an answer within 0.01 seconds",
        "none after 0.01 seconds",
    )
    assert error.fix is not None
    assert "timeout_seconds in EAP_LIMITS" in error.fix


async def done(call: object) -> str:
    return "done"


async def test_a_request_over_its_call_budget_is_stopped_and_told_how_to_raise_it() -> None:
    ledger = BudgetLedger()
    stage: BudgetInterceptor[Any, Any] = BudgetInterceptor(
        BudgetLimits(model_calls=2), ledger, kind="model", variable="EAP_LIMITS"
    )
    await stage(model_call(), done)
    await stage(model_call(), done)
    with pytest.raises(BudgetExceeded) as caught:
        await stage(model_call(), done)
    error = caught.value
    assert error.expected == "at most 2 model calls per request"
    assert error.actual == "2 used before this model call"
    assert error.fix is not None
    assert "budget.model_calls in EAP_LIMITS" in error.fix
    # Another request has a budget of its own.
    assert await stage(model_call(context("r-2")), done) == "done"


async def test_tokens_are_counted_from_each_reply() -> None:
    def usage(response: Any) -> dict[str, Any]:
        return {"input_tokens": 60, "output_tokens": 50}

    stage: BudgetInterceptor[Any, Any] = BudgetInterceptor(
        BudgetLimits(model_calls=None, tokens=100), BudgetLedger(), kind="model", usage=usage
    )
    await stage(model_call(), done)
    with pytest.raises(BudgetExceeded, match="budget of tokens"):
        await stage(model_call(), done)


def test_the_ledger_forgets_the_oldest_requests() -> None:
    ledger = BudgetLedger(max_requests=2)
    ledger.spent("a").model_calls = 5
    ledger.spent("b")
    ledger.spent("c")
    assert ledger.spent("a").model_calls == 0


async def test_a_looping_graph_is_stopped_by_the_budget_and_audited_as_denied() -> None:
    replies: list[Any] = [AIMessage("again") for _ in range(5)]
    config = ServiceConfig.for_testing(limits=Limits(budget=BudgetLimits(model_calls=3)))
    fakes = Fakes(model=FakeChatModelProvider(replies))
    async with fakes.container(config) as services:
        model = services.model()
        with bind_request_context(context()):
            for _ in range(3):
                await model.ainvoke([HumanMessage("go on")])
            with pytest.raises(BudgetExceeded):
                await model.ainvoke([HumanMessage("go on")])
    outcomes = [record.outcome.value for record in fakes.audit.records]
    assert outcomes == ["success", "success", "success", "denied"]
    assert fakes.audit.records[-1].error_type == "BudgetExceeded"


# ------------------------------- F3: nothing is released after the deadline


class SlowOutputCheck(FakeGuardrails):
    """Takes longer over the reply than the request has left."""

    async def check(self, point: GuardrailPoint, text: str, context: Any) -> Any:
        if point is GuardrailPoint.MODEL_OUTPUT:
            await asyncio.sleep(0.12)
        return await super().check(point, text, context)


async def test_f3_a_slow_output_check_cannot_release_a_late_answer() -> None:
    clock = SystemClock()
    fakes = Fakes(
        model=FakeChatModelProvider(["late answer"]),
        guardrails=SlowOutputCheck(),
        clock=clock,  # type: ignore[arg-type]  # the real clock, on purpose
    )
    async with fakes.container() as services:
        model = services.model()
        caller = context(deadline=clock.now() + timedelta(milliseconds=60))
        with bind_request_context(caller), pytest.raises(PolicyDenied) as caught:
            await model.ainvoke([HumanMessage("hello")])
    assert caught.value.reason_code == "deadline_exceeded"
    assert not caught.value.retryable
    (record,) = fakes.audit.records
    assert (record.outcome.value, record.error_type) == ("denied", "PolicyDenied")


async def test_f3_an_attempt_that_blocks_the_loop_past_the_deadline_is_not_a_success() -> None:
    clock = SystemClock()
    stage: ResilienceInterceptor[Any, Any] = ResilienceInterceptor(
        CallLimits(timeout_seconds=5, retries=2), clock, what="model call", sleep=no_wait
    )

    async def blocking(call: object) -> str:
        time.sleep(0.08)  # never yields, so no cancellation can reach it
        return "late"

    with pytest.raises(PolicyDenied) as caught:
        await stage(
            model_call(context(deadline=clock.now() + timedelta(milliseconds=30))), blocking
        )
    assert caught.value.reason_code == "deadline_exceeded"


async def test_f3_an_answer_that_arrives_after_the_deadline_on_the_injected_clock_is_refused() -> (
    None
):
    clock = FrozenClock()
    stage: DeadlineInterceptor[Any, Any] = DeadlineInterceptor(clock, "model call")

    async def answers_late(call: object) -> str:
        clock.advance(2)
        return "late"

    async def answers_in_time(call: object) -> str:
        clock.advance(0.5)
        return "in time"

    with pytest.raises(PolicyDenied) as caught:
        await stage(model_call(context(deadline=clock.now() + timedelta(seconds=1))), answers_late)
    assert caught.value.reason_code == "deadline_exceeded"
    in_time = model_call(context(deadline=clock.now() + timedelta(seconds=1)))
    assert await stage(in_time, answers_in_time) == "in time"
    assert await stage(model_call(context()), answers_late) == "late"  # no deadline, no limit


# --------------------- F5: a request ID a caller sends never shares a budget


async def test_f5_the_same_request_id_never_shares_a_budget_across_callers_or_requests() -> None:
    import httpx

    from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app
    from ai_agent_lib_core.testing import FakeIdentityVerifier

    config = ServiceConfig.for_testing(limits=Limits(budget=BudgetLimits(model_calls=2)))
    fakes = Fakes(
        model=FakeChatModelProvider([AIMessage("ok") for _ in range(10)]),
        identity=FakeIdentityVerifier(
            {
                "alice-token": Principal(subject="alice", tenant="tenant-a"),
                "bob-token": Principal(subject="bob", tenant="tenant-b"),
            }
        ),
    )
    async with fakes.container(config) as services:
        model = services.model()

        async def run(caller: RequestContext, given: Any) -> Any:
            with bind_request_context(caller):
                for _ in range(int(given)):  # the steps of one invocation share its budget
                    await model.ainvoke([HumanMessage("go on")])
            return "done"

        lifecycle = ServiceLifecycle(services.validate)
        await lifecycle.start()
        app = agent_app(services, run, application="accounts-agent", lifecycle=lifecycle)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:

            async def invoke(token: str, steps: int) -> int:
                reply = await http.post(
                    "/invoke",
                    json={"input": steps},
                    headers={"authorization": f"Bearer {token}", "x-request-id": "chosen-id"},
                )
                return reply.status_code

            assert await invoke("alice-token", 2) == 200  # Alice spends her whole allowance
            assert await invoke("bob-token", 2) == 200  # Bob, same ID: his own allowance
            assert await invoke("alice-token", 2) == 200  # Alice again: a new request, fresh
            assert await invoke("alice-token", 3) == 429  # but one request cannot exceed it


def test_f5_the_budget_is_counted_per_caller_and_invocation() -> None:
    def caller(subject: str, tenant: str, invocation: str | None = None) -> RequestContext:
        return RequestContext(
            principal=Principal(subject=subject, tenant=tenant),
            application="accounts-mcp",
            request_id="same-id",
            thread_id="th-1",
            invocation_id=invocation,
        )

    keys = {
        caller("alice", "a").budget_key,
        caller("bob", "b").budget_key,
        caller("alice", "b").budget_key,
        caller("alice", "a", "inv-1").budget_key,
        caller("alice", "a", "inv-2").budget_key,
    }
    assert len(keys) == 5
    # An MCP server counts per caller and the request ID the agent sends.
    assert caller("alice", "a").budget_key == caller("alice", "a").budget_key

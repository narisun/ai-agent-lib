"""Stages always run in the fixed order, however they were added."""

from __future__ import annotations

import enum
import random

import pytest

from ai_agent_lib_core.pipeline import (
    Handler,
    Interceptor,
    ModelStage,
    Pipeline,
    ToolStage,
    compose,
)


class Tracer:
    """An interceptor that records when it runs, before and after the call."""

    def __init__(self, name: str, trace: list[str]) -> None:
        self._name = name
        self._trace = trace

    async def __call__(self, request: str, call_next: Handler[str, str]) -> str:
        self._trace.append(f"{self._name} before")
        response = await call_next(request)
        self._trace.append(f"{self._name} after")
        return response


async def _run(stage_type: type[enum.IntEnum], seed: int) -> list[str]:
    trace: list[str] = []
    stages = list(stage_type)
    random.Random(seed).shuffle(stages)  # noqa: S311 - shuffling test input, not security
    pipeline: Pipeline[enum.IntEnum, str, str] = Pipeline()
    for stage in stages:
        pipeline = pipeline.with_stage(stage, Tracer(stage.name, trace))

    async def provider(request: str) -> str:
        trace.append("CALL")
        return request.upper()

    assert await pipeline.bind(provider)("hello") == "HELLO"
    return trace


# The golden orders. Changing either list is a change to the specification.
MODEL_ORDER = [
    "AUDIT before",
    "DEADLINE before",  # the whole call within the caller's deadline
    "BUDGET before",  # 1. execution pause check and budget reservation
    "IDENTITY before",  # 2. identity present and scope resolved
    "POLICY before",  # 3. policy decision on the model route
    "CONTEXT before",  # 4. context bounding
    "INPUT_GUARDRAILS before",  # 5. input guardrails
    "STRUCTURED_OUTPUT before",
    "OUTPUT_GUARDRAILS before",
    "RESILIENCE before",  # 6. retries and fallback
    "CALL",  # 7. provider call
    "RESILIENCE after",
    "OUTPUT_GUARDRAILS after",  # 8. output guardrails
    "STRUCTURED_OUTPUT after",  # 9. structured output validation
    "INPUT_GUARDRAILS after",
    "CONTEXT after",
    "POLICY after",
    "IDENTITY after",
    "BUDGET after",  # 10. budget settlement
    "DEADLINE after",  # a late answer is refused, not released
    "AUDIT after",
]

TOOL_ORDER = [
    "AUDIT before",
    "DEADLINE before",  # the whole call within the caller's deadline
    "BUDGET before",  # 1. execution pause check and budget reservation
    "REGISTRY before",  # 2. registry check
    "POLICY before",  # 3. policy decision
    "APPROVAL before",  # 4. human approval
    "INPUT_GUARDRAILS before",  # 5. input guardrails
    "IDEMPOTENCY before",  # 6. idempotency key
    "JUDGE before",
    "FRAMING before",
    "RESULT_GUARDRAILS before",
    "RESILIENCE before",  # 7. deadline and retries
    "CALL",  # 8. tool or data-source call
    "RESILIENCE after",
    "RESULT_GUARDRAILS after",  # 9. result size and guardrail checks
    "FRAMING after",  # 10. untrusted-result framing
    "JUDGE after",  # 11. optional judge
    "IDEMPOTENCY after",
    "INPUT_GUARDRAILS after",
    "APPROVAL after",
    "POLICY after",
    "REGISTRY after",
    "BUDGET after",
    "DEADLINE after",  # a late answer is refused, not released
    "AUDIT after",
]


@pytest.mark.parametrize("seed", range(5))
async def test_model_stages_run_in_the_golden_order(seed: int) -> None:
    assert await _run(ModelStage, seed) == MODEL_ORDER


@pytest.mark.parametrize("seed", range(5))
async def test_tool_stages_run_in_the_golden_order(seed: int) -> None:
    assert await _run(ToolStage, seed) == TOOL_ORDER


async def test_missing_stages_are_skipped_without_moving_the_others() -> None:
    trace: list[str] = []
    pipeline: Pipeline[ModelStage, str, str] = (
        Pipeline[ModelStage, str, str]()
        .with_stage(ModelStage.RESILIENCE, Tracer("RESILIENCE", trace))
        .with_stage(ModelStage.AUDIT, Tracer("AUDIT", trace))
    )
    assert pipeline.stages == (ModelStage.AUDIT, ModelStage.RESILIENCE)

    async def provider(request: str) -> str:
        return request

    await pipeline.bind(provider)("x")
    assert trace == ["AUDIT before", "RESILIENCE before", "RESILIENCE after", "AUDIT after"]


def test_a_stage_cannot_be_filled_twice() -> None:
    trace: list[str] = []
    pipeline = Pipeline[ModelStage, str, str]().with_stage(ModelStage.AUDIT, Tracer("a", trace))
    with pytest.raises(ValueError, match="AUDIT is already filled"):
        pipeline.with_stage(ModelStage.AUDIT, Tracer("b", trace))


def test_with_stage_returns_a_new_pipeline() -> None:
    empty = Pipeline[ModelStage, str, str]()
    filled = empty.with_stage(ModelStage.AUDIT, Tracer("a", []))
    assert empty.stages == ()
    assert filled.stages == (ModelStage.AUDIT,)


async def test_an_interceptor_can_refuse_without_calling_the_next_one() -> None:
    reached: list[str] = []

    class Refuse:
        async def __call__(self, request: str, call_next: Handler[str, str]) -> str:
            raise PermissionError("refused")

    interceptors: list[Interceptor[str, str]] = [Refuse(), Tracer("inner", reached)]

    async def provider(request: str) -> str:
        reached.append("CALL")
        return request

    with pytest.raises(PermissionError, match="refused"):
        await compose(interceptors, provider)("x")
    assert reached == []

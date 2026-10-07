"""The fixed stage order of the model and tool pipelines.

A pipeline is assembled from stages, and a stage's position comes from its
place in the enum below, never from the order in which stages were added or
from configuration. Options can switch a stage on or off; nothing can move it.

Stages are listed from the outside in. The pre-call work of a stage therefore
runs in listed order, and its post-call work runs in reverse. That is how the
specification's order is obtained: for a model call, output guardrails run
first after the provider returns, then structured output, then budget
settlement.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from types import MappingProxyType
from typing import Generic, Self, TypeVar

from ai_agent_lib_core.pipeline.interceptor import Handler, Interceptor, compose

__all__ = ["DataStage", "ModelStage", "Pipeline", "ToolStage"]


class ModelStage(enum.IntEnum):
    """Stages of a model call, outermost first."""

    AUDIT = 0
    BUDGET = 10  # execution pause check, reservation before and settlement after
    IDENTITY = 20
    POLICY = 30
    CONTEXT = 40
    INPUT_GUARDRAILS = 50
    STRUCTURED_OUTPUT = 60  # validates on the way back
    OUTPUT_GUARDRAILS = 70  # checks on the way back, before structured output
    RESILIENCE = 80  # retries and fallback, closest to the provider


class ToolStage(enum.IntEnum):
    """Stages of a tool call, outermost first."""

    AUDIT = 0
    BUDGET = 10  # execution pause check and reservation
    REGISTRY = 20
    POLICY = 30
    APPROVAL = 40
    INPUT_GUARDRAILS = 50
    IDEMPOTENCY = 60
    JUDGE = 70  # judges on the way back, last
    FRAMING = 80  # frames the result as untrusted on the way back
    RESULT_GUARDRAILS = 90  # checks the result on the way back, first
    RESILIENCE = 100  # deadline, and retries for read-only tools


class DataStage(enum.IntEnum):
    """Stages of a governed data query, outermost first."""

    AUDIT = 0
    POLICY = 30  # the decision, and the obligations the data layer must apply
    RESILIENCE = 100


StageT = TypeVar("StageT", bound=enum.IntEnum)
RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


class Pipeline(Generic[StageT, RequestT, ResponseT]):
    """An immutable set of stages that always runs in the fixed order."""

    def __init__(self, stages: Mapping[StageT, Interceptor[RequestT, ResponseT]] | None = None):
        ordered = sorted((stages or {}).items(), key=lambda item: item[0])
        self._stages: Mapping[StageT, Interceptor[RequestT, ResponseT]] = MappingProxyType(
            dict(ordered)
        )

    def with_stage(self, stage: StageT, interceptor: Interceptor[RequestT, ResponseT]) -> Self:
        """Return a pipeline that also runs ``interceptor`` at ``stage``.

        Raises:
            ValueError: If the stage is already filled.
        """
        if stage in self._stages:
            raise ValueError(f"stage {stage.name} is already filled")
        return type(self)({**self._stages, stage: interceptor})

    @property
    def stages(self) -> tuple[StageT, ...]:
        """The filled stages, in the order they run."""
        return tuple(self._stages)

    def bind(self, terminal: Handler[RequestT, ResponseT]) -> Handler[RequestT, ResponseT]:
        """Return a handler that runs every stage around ``terminal``."""
        return compose(tuple(self._stages.values()), terminal)

"""UI-owned threads and runs; checkpoints remain owned by the agent service."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ai_agent_lib_core.contracts import Scope
from ai_agent_lib_core.contracts.diagnostics import (
    DiagnosticValue,
    Identifier,
    RequestRecord,
    TokenUsage,
)
from ai_agent_lib_ui.contracts.artifacts import ScopedArtifact
from ai_agent_lib_ui.contracts.queries import Availability, ContractError, ErrorCode
from ai_agent_lib_ui.contracts.traces import TraceLink, tolerant_trace

RunState = Literal["accepted", "running", "completed", "failed", "unknown"]


class ThreadRef(DiagnosticValue):
    """An agent thread identity, always qualified by tenant, subject and agent."""

    scope: Scope
    thread_id: Identifier


class ThreadRecord(ThreadRef):
    """A durable conversation bound to one agent; changing agents starts a thread."""

    schema_version: Literal["agentlib.ui-thread/v1"] = Field(
        default="agentlib.ui-thread/v1", alias="schema"
    )
    title: Annotated[str, Field(min_length=1, max_length=256)]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    archived: bool = False


class TurnAdmission(ThreadRef):
    """Persist before dispatch; one idempotency key must bind exactly one question.

    A store atomically admits one active turn per scoped thread across replicas.
    Same key/content returns the existing run; changed content yields conflict.
    Unknown outcomes continue holding admission until authoritative reconciliation.
    """

    idempotency_key: Identifier
    question: Annotated[str, Field(min_length=1, max_length=65_536)]


class RunRecord(RequestRecord):
    """Durable UI state; a timeout after dispatch is unknown, not safely retryable."""

    schema_version: Literal["agentlib.ui-run/v1"] = Field(
        default="agentlib.ui-run/v1", alias="schema"
    )
    run_id: Identifier
    idempotency_key: Identifier
    state: RunState
    accepted_at: AwareDatetime
    updated_at: AwareDatetime
    answer: str | None = None
    artifacts: Annotated[tuple[ScopedArtifact, ...], Field(max_length=20)] = ()
    artifact_status: Availability = Availability.UNSUPPORTED
    artifact_warning: Annotated[str, Field(max_length=128)] | None = None
    elapsed_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model_calls: Annotated[int, Field(strict=True, ge=0)] | None = None
    trace: TraceLink | None = None
    failure_code: (
        Literal["invalid", "denied", "unauthorized", "budget_exceeded", "paused"] | None
    ) = None
    registry_revision: Identifier | None = None

    @field_validator("trace", mode="before")
    @classmethod
    def degrade_trace(cls, value: object) -> TraceLink | None:
        """Reload a stored row whose link no longer validates, keeping its answer and metrics."""
        return tolerant_trace(value)

    @model_validator(mode="after")
    def check_answer(self) -> Self:
        """Only terminal accepted answers become chat messages."""
        if self.answer is not None and self.state != "completed":
            raise ValueError("only completed runs have an accepted answer")
        for item in self.artifacts:
            if (
                self.state != "completed"
                or item.scope != self.scope
                or item.artifact.thread_id != self.thread_id
                or item.artifact.request_id != self.request_id
            ):
                raise ValueError("artifacts must belong to this completed invocation")
        return self


def require_transition(before: RunState, after: RunState, *, reconciled: bool = False) -> None:
    """Allow unknown resolution only with authoritative outcome evidence."""
    allowed: dict[RunState, frozenset[RunState]] = {
        "accepted": frozenset({"running", "failed"}),
        "running": frozenset({"completed", "failed", "unknown"}),
        "completed": frozenset(),
        "failed": frozenset(),
        "unknown": frozenset({"completed", "failed"}) if reconciled else frozenset(),
    }
    if after not in allowed[before]:
        raise ContractError(ErrorCode.CONFLICT)

"""Versioned metadata shared by agents and diagnostic clients.

These records carry identifiers and usage, never prompts, answers, tool arguments
or credentials. Content remains in the separately enabled model trace. A returned
model response is not necessarily the answer accepted by an agent application.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, Self

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from ai_agent_lib_core.contracts.schema_ids import current_schema
from ai_agent_lib_core.contracts.scope import Scope

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]
TokenCount = Annotated[int, Field(strict=True, ge=0)]


__all__ = [
    "AttemptRef",
    "DiagnosticLinks",
    "DiagnosticSink",
    "DiagnosticValue",
    "ModelAttempt",
    "OperationDecision",
    "OperationSummary",
    "RequestOutcome",
    "RequestOutcomeJournal",
    "RequestRecord",
    "TokenUsage",
    "UsageField",
]


class DiagnosticValue(BaseModel):
    """An immutable, explicitly versioned wire value with no unexpected fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class UsageField(DiagnosticValue):
    """One allowlisted numeric field from provider usage, retained for inspection."""

    name: Identifier
    value: TokenCount


class TokenUsage(DiagnosticValue):
    """Normalized counts; input includes cache reads and writes exactly once.

    ``None`` means the provider did not report a count. It must never be displayed
    as zero. SDK-internal retries are not reconstructed from these counts.
    """

    source: Literal["langchain", "anthropic", "unavailable"] = "unavailable"
    kind: Literal["reported", "simulated", "estimated", "unavailable"] = "unavailable"
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    cache_read_tokens: TokenCount | None = None
    cache_write_tokens: TokenCount | None = None
    source_fields: tuple[UsageField, ...] = ()

    @model_validator(mode="after")
    def check_cache_counts(self) -> Self:
        """Reject known cache counts that exceed the normalized input total."""
        cached = (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0)
        if self.input_tokens is not None and cached > self.input_tokens:
            raise ValueError("cache counts exceed input tokens")
        return self


class AttemptRef(DiagnosticValue):
    """Unique within a scoped request; combine all three for deduplication."""

    call_id: Identifier
    attempt: Annotated[int, Field(strict=True, ge=1)]


class DiagnosticLinks(DiagnosticValue):
    """Optional explicit links, supplied by trusted application/framework code.

    Missing links stay missing. In particular, a request ID alone cannot establish
    a link to an MCP service belonging to a different application scope.
    """

    node: Identifier | None = None
    checkpoint_id: Identifier | None = None
    tool_call_ids: tuple[Identifier, ...] = ()


class RequestRecord(DiagnosticValue):
    """Every diagnostic query and stored record includes the agent's full scope."""

    scope: Scope
    thread_id: Identifier
    request_id: Identifier


class ModelAttempt(RequestRecord):
    """One observed SDK invocation, including failed or later rejected attempts."""

    schema_version: Annotated[
        Literal["agentcraft2.model-usage/v1"], BeforeValidator(current_schema)
    ] = Field(default="agentcraft2.model-usage/v1", alias="schema")
    timestamp: AwareDatetime
    ref: AttemptRef
    model_alias: Identifier
    provider: Identifier
    model_id: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$")]
    outcome: Literal["returned", "failed", "cancelled"]
    duration_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    usage: TokenUsage = Field(default_factory=TokenUsage)
    links: DiagnosticLinks = Field(default_factory=DiagnosticLinks)


class OperationDecision(RequestRecord):
    """A model pipeline or application decision, with no discarded draft content."""

    schema_version: Annotated[
        Literal["agentcraft2.operation-decision/v1"], BeforeValidator(current_schema)
    ] = Field(default="agentcraft2.operation-decision/v1", alias="schema")
    timestamp: AwareDatetime
    ref: AttemptRef
    stage: Literal["model_pipeline", "application"]
    outcome: Literal["passed", "rejected"]
    reason_code: Identifier


class OperationSummary(RequestRecord):
    """Terminal manifest of observed attempts, used to detect ingestion gaps.

    ``accepted_output`` identifies the successful application return; it never
    identifies the last model response as the final answer. If this summary is
    absent, ingestion completeness is unknown. Detached tasks are not supported:
    an agent must await all model work before returning its terminal output.
    """

    schema_version: Annotated[
        Literal["agentcraft2.operation-summary/v1"], BeforeValidator(current_schema)
    ] = Field(default="agentcraft2.operation-summary/v1", alias="schema")
    timestamp: AwareDatetime
    outcome: Literal["completed", "failed", "unknown"]
    expected_attempts: tuple[AttemptRef, ...]
    capture_complete: bool
    accepted_output: Literal["invoke.output"] | None = None

    @model_validator(mode="after")
    def check_terminal(self) -> Self:
        """Require successful application completion before accepting output."""
        if self.accepted_output is not None and self.outcome != "completed":
            raise ValueError("only a completed operation can accept output")
        if len(set(self.expected_attempts)) != len(self.expected_attempts):
            raise ValueError("attempt references must be unique")
        return self


DiagnosticEvent = ModelAttempt | OperationDecision | OperationSummary


class DiagnosticSink(Protocol):
    """Store metadata events; failures must not replace the agent's result."""

    async def write(self, event: DiagnosticEvent) -> None:
        """Persist one event. Implementations must support concurrent requests."""
        ...


class RequestOutcome(RequestRecord):
    """Journal value distinct from checkpoints; crash uncertainty is explicit.

    A store persists admission before dispatch. Recovery of a running request with
    no authoritative terminal record yields unknown, never an automatic replay.
    Writing a checkpoint does not atomically commit this journal.
    """

    schema_version: Annotated[
        Literal["agentcraft2.request-outcome/v1"], BeforeValidator(current_schema)
    ] = Field(default="agentcraft2.request-outcome/v1", alias="schema")
    state: Literal["accepted", "running", "completed", "failed", "unknown"]
    updated_at: AwareDatetime
    checkpoint_ids: tuple[Identifier, ...] = ()


class RequestOutcomeJournal(Protocol):
    """Agent-owned outcome persistence, implemented with storage in later phases."""

    async def get(self, request: RequestRecord) -> RequestOutcome | None:
        """Read exactly this scope/thread/request; never search by ID alone."""
        ...

    async def compare_and_set(
        self, outcome: RequestOutcome, *, expected: RequestOutcome | None
    ) -> bool:
        """Atomically replace the expected value, or insert when expected is None."""
        ...

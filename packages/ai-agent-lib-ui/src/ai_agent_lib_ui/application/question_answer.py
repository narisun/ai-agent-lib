"""Project the stable /invoke envelope into chat without replaying its transcript."""

import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Annotated

from pydantic import Field, ValidationError

from ai_agent_lib_core.contracts import is_schema
from ai_agent_lib_core.contracts.diagnostics import DiagnosticValue, Identifier, TokenUsage
from ai_agent_lib_ui.contracts.artifacts import Artifact, ScopedArtifact
from ai_agent_lib_ui.contracts.catalog import PresentationManifest
from ai_agent_lib_ui.contracts.conversations import TurnAdmission
from ai_agent_lib_ui.contracts.queries import Availability, ContractError, ErrorCode, ViewContext
from ai_agent_lib_ui.contracts.traces import TraceLink, tolerant_trace


class AcceptedAnswer(DiagnosticValue):
    """An answer only projected from a successful terminal invocation envelope."""

    answer: str
    thread_id: Identifier
    request_id: Identifier
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model_calls: Annotated[int, Field(strict=True, ge=0)] | None = None
    artifacts: tuple[ScopedArtifact, ...] = ()
    artifact_status: Availability = Availability.UNSUPPORTED
    artifact_warning: Annotated[str, Field(max_length=128)] | None = None
    trace: TraceLink | None = None


class QuestionAnswerAdapter:
    """Pure question-answer/v1 mapping; the HTTP adapter owns transport and auth."""

    def __init__(self, manifest: PresentationManifest) -> None:
        if manifest.adapter != "question-answer/v1":
            raise ContractError(ErrorCode.UNSUPPORTED_ADAPTER)
        self.manifest = manifest

    def request(self, view: ViewContext, turn: TurnAdmission) -> dict[str, object]:
        """Send one new question; the agent restores its own checkpoint context."""
        if turn.scope != view.scope or turn.scope.application != self.manifest.agent_id:
            raise ContractError(ErrorCode.NOT_FOUND)
        if (
            not turn.question.strip()
            or len(turn.question.encode("utf-8")) > self.manifest.max_question_bytes
        ):
            raise ContractError(ErrorCode.INVALID_INPUT)
        return {"input": {"question": turn.question}, "thread_id": turn.thread_id}

    def response(
        self,
        view: ViewContext,
        turn: TurnAdmission,
        body: bytes,
        *,
        status_code: int = 200,
        request_id: str | None = None,
    ) -> AcceptedAnswer:
        """Accept a successful terminal envelope; malformed optional artifacts degrade.

        The HTTP adapter bounds bytes while reading, before this projection.
        Graph progress updates and raw model responses must never call this method.
        """
        self.request(view, turn)
        if status_code != 200 or len(body) > self.manifest.max_response_bytes:
            raise ContractError(ErrorCode.INVALID_RESPONSE)
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or value.get("thread_id") != turn.thread_id:
                raise ValueError("invalid envelope")
            if request_id is not None and value.get("request_id") != request_id:
                raise ValueError("uncorrelated response")
            output = value.get("output")
            if not isinstance(output, dict) or not isinstance(output.get("answer"), str):
                raise ValueError("missing answer")
            answer = AcceptedAnswer(
                answer=output["answer"],
                thread_id=value["thread_id"],
                request_id=value["request_id"],
            )
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise ContractError(ErrorCode.INVALID_RESPONSE) from error
        # Usage is optional: malformed metrics cannot replace an accepted answer.
        with suppress(ValidationError):
            answer = AcceptedAnswer(
                **answer.model_dump(exclude={"usage", "model_calls"}),
                usage=TokenUsage.model_validate(value.get("usage", {})),
                model_calls=value.get("model_calls"),
            )
        # Trace links are service metadata, never extracted from model answer text.
        # Optional malformed or retired-provider diagnostics must not discard an accepted answer.
        answer = answer.model_copy(update={"trace": tolerant_trace(output.get("trace"))})
        return self._artifacts(view, output, answer)

    def _artifacts(
        self,
        view: ViewContext,
        output: Mapping[str, object],
        answer: AcceptedAnswer,
    ) -> AcceptedAnswer:
        if self.manifest.capability("artifacts") != Availability.AVAILABLE:
            return answer
        try:
            raw = output.get("artifacts", [])
            if not isinstance(raw, list) or len(raw) > 20:
                raise ValueError("invalid artifact collection")
            artifacts = tuple(self._artifact(item, view, answer) for item in raw)
            if len({item.artifact.id for item in artifacts}) != len(artifacts):
                raise ValueError("duplicate artifacts")
        except (ValueError, ValidationError):
            return answer.model_copy(
                update={
                    "artifact_status": Availability.UNAVAILABLE,
                    "artifact_warning": "invalid_artifacts",
                }
            )
        return answer.model_copy(
            update={"artifacts": artifacts, "artifact_status": Availability.AVAILABLE}
        )

    def _artifact(self, value: object, view: ViewContext, answer: AcceptedAnswer) -> ScopedArtifact:
        if not isinstance(value, dict) or not is_schema(
            value.get("schema"), "agentcraft2.artifact/v1"
        ):
            raise ValueError("unsupported artifact schema")
        artifact = Artifact.model_validate(value)
        if artifact.thread_id != answer.thread_id or artifact.request_id != answer.request_id:
            raise ValueError("artifact belongs to another invocation")
        if (
            artifact.content is not None
            and len(artifact.content.encode("utf-8")) > self.manifest.max_artifact_bytes
        ):
            raise ValueError("artifact exceeds configured size")
        return ScopedArtifact(scope=view.scope, artifact=artifact)

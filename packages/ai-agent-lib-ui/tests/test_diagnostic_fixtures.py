"""Synthetic serialized evidence distinguishes spend, acceptance and completeness."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_agent_lib_core.contracts.diagnostics import (
    DiagnosticEvent,
    ModelAttempt,
    OperationDecision,
    OperationSummary,
)

FIXTURE = Path(__file__).parent / "fixtures/diagnostics.json"


def test_rejected_draft_and_failed_sdk_call_remain_separate_from_accepted_output() -> None:
    adapter = TypeAdapter(list[DiagnosticEvent])
    events = adapter.validate_json(FIXTURE.read_bytes())
    assert json.loads(adapter.dump_json(events, by_alias=True)) == json.loads(FIXTURE.read_bytes())
    draft, rejection, failure, terminal = events
    assert isinstance(draft, ModelAttempt)
    assert isinstance(rejection, OperationDecision)
    assert isinstance(failure, ModelAttempt)
    assert isinstance(terminal, OperationSummary)
    assert draft.usage.input_tokens == 100
    assert draft.usage.output_tokens == 20
    assert rejection.ref == draft.ref
    assert rejection.outcome == "rejected"
    assert failure.usage.input_tokens is None
    assert terminal.accepted_output is None
    # All records arrived; provider token completeness is a separate question.
    assert terminal.capture_complete
    assert terminal.expected_attempts == (draft.ref, failure.ref)


def test_unknown_diagnostic_schema_cannot_be_silently_read_as_v1() -> None:
    value = json.loads(FIXTURE.read_bytes())[0]
    value["schema"] = "agentlib.model-usage/v2"
    with pytest.raises(ValidationError):
        TypeAdapter(DiagnosticEvent).validate_python(value)

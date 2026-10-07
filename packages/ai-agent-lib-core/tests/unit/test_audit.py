"""The audit record is immutable and metadata-only."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ai_agent_lib_core.contracts import AUDIT_SCHEMA, AuditOutcome, AuditRecord


def _record(**overrides: Any) -> AuditRecord:
    values: dict[str, Any] = {
        "record_id": "rec-1",
        "timestamp": datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        "event": "model.call",
        "outcome": AuditOutcome.SUCCESS,
        "request_id": "r-1",
        "thread_id": "th-1",
        "tenant": "t-9",
        "subject": "u-1",
        "application": "accounts-agent",
        "attributes": {"model_alias": "default", "tokens": 12, "cached": False},
    }
    values.update(overrides)
    return AuditRecord(**values)


def test_record_is_immutable_including_its_attributes() -> None:
    record = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.event = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        record.attributes["tokens"] = 99  # type: ignore[index]


def test_record_keeps_a_copy_of_the_attributes_it_was_given() -> None:
    attributes: dict[str, Any] = {"tokens": 1}
    record = _record(attributes=attributes)
    attributes["tokens"] = 2
    assert record.attributes["tokens"] == 1


@pytest.mark.parametrize("content", [{"nested": "dict"}, ["a", "list"], b"bytes", object()])
def test_attributes_must_be_scalars_so_content_cannot_be_recorded(content: object) -> None:
    with pytest.raises(TypeError, match="scalar"):
        _record(attributes={"payload": content})


def test_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(timestamp=datetime(2026, 10, 7))  # noqa: DTZ001 - the naive value is the point


def test_to_dict_is_json_serializable_and_versioned() -> None:
    payload = _record(outcome="denied", error_type="PolicyDenied").to_dict()
    assert payload["schema"] == AUDIT_SCHEMA
    assert payload["outcome"] == "denied"
    assert payload["timestamp"] == "2026-10-07T12:00:00+00:00"
    assert json.loads(json.dumps(payload)) == payload

"""The audit record: metadata-only evidence of what the library did."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

__all__ = ["AUDIT_SCHEMA", "AuditOutcome", "AuditRecord", "AuditValue"]

AUDIT_SCHEMA = "agentlib.audit/v1"

AuditValue = str | int | float | bool | None
"""Scalar types for audit metadata; callers must exclude content and credentials."""


class AuditOutcome(enum.StrEnum):
    """How the audited operation ended."""

    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One audited event.

    Attributes hold metadata such as names, counts and durations. Scalar
    validation rejects nested payloads but cannot recognize sensitive strings.
    Callers must keep prompts, tool results, and credentials out of attributes.

    Attributes:
        record_id: Unique identifier of this record.
        timestamp: When the event finished, as an aware datetime.
        event: What happened, for example ``model.call`` or ``tool.call``.
        outcome: How it ended.
        request_id: The request the event belongs to.
        thread_id: The conversation or workflow the event belongs to.
        tenant: The tenant of the caller.
        subject: The caller.
        application: The agent or MCP server that performed the event.
        attributes: Scalar metadata about the event.
        error_type: The exception class name when the outcome is not success.
    """

    record_id: str
    timestamp: datetime
    event: str
    outcome: AuditOutcome
    request_id: str
    thread_id: str
    tenant: str
    subject: str
    application: str
    attributes: Mapping[str, AuditValue] = field(default_factory=dict)
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        for key, value in self.attributes.items():
            if not isinstance(key, str):
                raise TypeError("audit attribute names must be strings")
            if value is not None and not isinstance(value, str | int | float | bool):
                raise TypeError(
                    f"audit attribute {key!r} must be a scalar, not {type(value).__name__}"
                )
        object.__setattr__(self, "outcome", AuditOutcome(self.outcome))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def to_dict(self) -> dict[str, object]:
        """Return the record as a JSON-serializable dictionary."""
        return {
            "schema": AUDIT_SCHEMA,
            "record_id": self.record_id,
            "timestamp": self.timestamp.isoformat(),
            "event": self.event,
            "outcome": self.outcome.value,
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "tenant": self.tenant,
            "subject": self.subject,
            "application": self.application,
            "attributes": dict(self.attributes),
            "error_type": self.error_type,
        }

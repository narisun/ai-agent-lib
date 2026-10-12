"""Scope, bounded pagination, permissions and safe frontend error semantics."""

from enum import StrEnum
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import Field, model_validator

from ai_agent_lib_core.contracts import Principal, Scope
from ai_agent_lib_core.contracts.diagnostics import DiagnosticValue, Identifier


class Permission(StrEnum):
    """Separate policy actions; catalog membership alone grants none of them."""

    CATALOG_READ = "ui.catalog.read"
    AGENT_INVOKE = "ui.agent.invoke"
    CONVERSATION_READ = "ui.conversation.read"
    CONVERSATION_WRITE = "ui.conversation.write"
    ARTIFACT_READ = "ui.artifact.read"
    DIAGNOSTIC_METADATA = "ui.diagnostic.metadata.read"
    DIAGNOSTIC_CONTENT = "ui.diagnostic.content.read"
    SERVICE_LOG_READ = "ui.service-log.read"


class ViewContext(DiagnosticValue):
    """Verified caller and target agent scope, bound by server composition code.

    This checks ownership consistency, not authentication or policy. Adapters must
    still authorize the specific action. Cross-user support access is not enabled
    by this contract; service logs need a separate authorized reader.
    """

    principal: Principal
    scope: Scope

    @model_validator(mode="after")
    def check_owner(self) -> Self:
        """Prevent a caller's identifiers being combined with another owner."""
        if (self.principal.tenant, self.principal.subject) != (
            self.scope.tenant,
            self.scope.subject,
        ):
            raise ValueError("caller does not own this scope")
        return self


class PageRequest(DiagnosticValue):
    """Opaque cursors must be verified against the same scope and query filters."""

    limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 50
    cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class QueryBinding(DiagnosticValue):
    """Persist/sign this binding with a cursor; IDs alone never establish access."""

    scope: Scope
    resource: Literal["threads", "checkpoints", "diagnostics", "artifacts"]
    thread_id: Identifier | None = None
    request_id: Identifier | None = None
    filters: tuple[tuple[Identifier, str], ...] = ()

    def matches(self, other: "QueryBinding") -> bool:
        """Compare the entire query, including scope, without relaxing filters."""
        return self == other


Item = TypeVar("Item")


class Page(DiagnosticValue, Generic[Item]):
    """A bounded summary page; large diagnostic content is fetched separately."""

    items: Annotated[tuple[Item, ...], Field(max_length=200)] = ()
    next_cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class Availability(StrEnum):
    """Visible capability/capture status; absence must never look like zero usage."""

    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"
    PENDING = "pending"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


class ErrorCode(StrEnum):
    """Stable errors for UI adapters; no exception text is exposed to the browser."""

    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNSUPPORTED_ADAPTER = "unsupported_adapter"
    STALE_BINDING = "stale_binding"
    DESTINATION_DENIED = "destination_denied"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


class ErrorResponse(DiagnosticValue):
    """Return not_found for both a missing object and an unauthorized object ID."""

    code: ErrorCode
    request_id: Identifier


class ContractError(ValueError):
    """A safe contract failure whose message is only a stable error code."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class ContentPreview(DiagnosticValue):
    """A decoded text preview bounded to 64 KiB, with explicit truncation."""

    text: str
    truncated: bool
    availability: Availability = Availability.AVAILABLE

    @model_validator(mode="after")
    def check_bytes(self) -> Self:
        """Bound UTF-8 bytes, including multibyte characters."""
        if len(self.text.encode("utf-8")) > 65_536:
            raise ValueError("preview exceeds 64 KiB")
        return self

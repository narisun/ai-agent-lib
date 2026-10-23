"""Immutable document versions, separate from the accepted chat transcript."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BeforeValidator, Field, model_validator

from agentcraft2_core.contracts import Scope, current_schema
from agentcraft2_core.contracts.diagnostics import DiagnosticValue, Identifier


class Artifact(DiagnosticValue):
    """Agent output declaration; ownership comes from the verified invocation.

    content_ref is an opaque store key, never a URL or filesystem path. A reader
    must authorize it within scope. Renderers treat Markdown as untrusted content.
    """

    schema_version: Annotated[
        Literal["agentcraft2.artifact/v1"], BeforeValidator(current_schema)
    ] = Field(default="agentcraft2.artifact/v1", alias="schema")
    id: Identifier
    document_id: Identifier
    version: Annotated[int, Field(strict=True, ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    media_type: Literal["text/markdown", "text/plain"]
    created_at: AwareDatetime
    request_id: Identifier
    thread_id: Identifier
    content: str | None = None
    content_ref: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,256}$")] | None = None

    @model_validator(mode="after")
    def check_content(self) -> Self:
        """Require one content source and impose an absolute 1 MiB inline ceiling."""
        if (self.content is None) == (self.content_ref is None):
            raise ValueError("provide exactly one content source")
        if self.content is not None and len(self.content.encode("utf-8")) > 1_048_576:
            raise ValueError("artifact exceeds 1 MiB")
        return self


class ScopedArtifact(DiagnosticValue):
    """An immutable version after binding to its verified agent invocation."""

    scope: Scope
    artifact: Artifact

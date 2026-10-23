"""Bounded, decoded diagnostics for one authenticated agent turn."""

from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from ai_agent_lib_core.contracts.diagnostics import DiagnosticValue, RequestRecord
from ai_agent_lib_core.contracts.identity import RequestContext

__all__ = ["InspectionSection", "TurnInspection", "TurnInspector"]


class InspectionSection(DiagnosticValue):
    """A text preview; truncation and missing capture are always explicit."""

    availability: Literal["available", "disabled", "unavailable", "unsupported"] = "unavailable"
    text: str = ""
    truncated: bool = False
    record_count: Annotated[int, Field(strict=True, ge=0)] = 0

    @model_validator(mode="after")
    def bounded_text(self) -> Self:
        """Limit each section to 64 KiB of decoded UTF-8."""
        if len(self.text.encode("utf-8")) > 65_536:
            raise ValueError("inspection preview exceeds 64 KiB")
        return self


class TurnInspection(RequestRecord):
    """Scope and turn identity accompany every diagnostic response."""

    schema_version: Literal["agentlib.turn-inspection/v1"] = Field(
        default="agentlib.turn-inspection/v1", alias="schema"
    )
    model_inputs: InspectionSection = Field(default_factory=InspectionSection)
    model_outputs: InspectionSection = Field(default_factory=InspectionSection)
    mcp: InspectionSection = Field(default_factory=InspectionSection)
    checkpoints: InspectionSection = Field(default_factory=InspectionSection)


class TurnInspector(Protocol):
    """Authorize content access and read one turn without executing any agent work."""

    async def read(self, context: RequestContext, request_id: str) -> TurnInspection:
        """Check diagnostic permissions and return only this scope/thread/request."""
        ...

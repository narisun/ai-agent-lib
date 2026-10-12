"""Presentation manifests accompany the registry without changing its schema."""

from typing import Annotated, Literal

from pydantic import Field

from ai_agent_lib_core.contracts.diagnostics import DiagnosticValue, Identifier
from ai_agent_lib_ui.contracts.queries import Availability

KNOWN_CAPABILITIES = frozenset(
    {"progress", "artifacts", "checkpoints", "model_traces", "run_status"}
)


class PresentationManifest(DiagnosticValue):
    """Server-owned chat binding pinned to an agent version and registry revision.

    Optional unknown capabilities are retained but displayed as unsupported. The
    transport adapter and manifest schema must be known before invoking an agent.
    Endpoints are taken from the registry, never supplied by browser input here.
    """

    schema_version: Literal["agentlib.presentation/v1"] = Field(
        default="agentlib.presentation/v1", alias="schema"
    )
    agent_id: Identifier
    agent_version: Identifier
    registry_revision: Identifier
    title: Annotated[str, Field(min_length=1, max_length=256)]
    adapter: Annotated[str, Field(min_length=1, max_length=128)] = "question-answer/v1"
    api_audience: Annotated[str, Field(min_length=1, max_length=512)]
    required_scopes: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = ()
    capabilities: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = ()
    max_question_bytes: Annotated[int, Field(strict=True, ge=1, le=1_048_576)] = 65_536
    max_response_bytes: Annotated[int, Field(strict=True, ge=1, le=8_388_608)] = 1_048_576
    max_artifact_bytes: Annotated[int, Field(strict=True, ge=1, le=1_048_576)] = 262_144

    def capability(self, name: str) -> Availability:
        """Return a clear status without making chat depend on optional features."""
        return (
            Availability.AVAILABLE
            if name in KNOWN_CAPABILITIES and name in self.capabilities
            else Availability.UNSUPPORTED
        )


class AgentBinding(DiagnosticValue):
    """An endpoint after matching server configuration and a registry snapshot."""

    manifest: PresentationManifest
    base_url: str


class McpSummary(DiagnosticValue):
    """Catalog metadata only; the UI never executes arbitrary MCP tools."""

    id: Identifier
    owner: str
    tool_names: tuple[str, ...]

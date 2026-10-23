"""Presentation manifests accompany the registry without changing its schema."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import Field, model_validator

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


AWS_REGION = r"^[a-z]{2}(-[a-z]+)+-\d$"
RUNTIME_ARN = (
    r"^arn:aws[a-z-]*:bedrock-agentcore:"
    + AWS_REGION[1:-1]
    + r":\d{12}:runtime/[A-Za-z0-9_-]{1,128}$"
)


class AgentCoreRuntime(DiagnosticValue):
    """An agent hosted on AgentCore Runtime, reached through the service endpoint.

    The invocation URL is derived from the ARN, never typed by an operator, and
    the dispatcher appends the qualifier as the only query parameter. In
    ``bearer`` mode the operator keeps a token file whose first line is the
    bearer; it is read for every request and never copied into settings or logs.
    """

    arn: Annotated[str, Field(pattern=RUNTIME_ARN, max_length=512)]
    region: Annotated[str, Field(pattern=AWS_REGION, max_length=32)] | None = None
    qualifier: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = "DEFAULT"
    auth: Literal["iam", "bearer"] = "iam"
    token_file: Path | None = None

    @model_validator(mode="after")
    def check_runtime(self) -> "AgentCoreRuntime":
        """Keep the region the ARN names and require a token source for bearer auth."""
        if self.region is not None and self.region != self.arn.split(":")[3]:
            raise ValueError("region must match the runtime ARN")
        if (self.auth == "bearer") != (self.token_file is not None):
            raise ValueError("token_file is required for bearer auth and only then")
        return self

    @property
    def aws_region(self) -> str:
        """The region the runtime lives in; configured or read from the ARN."""
        return self.region or self.arn.split(":")[3]

    @property
    def invocation_url(self) -> str:
        """The service endpoint for this runtime, without the qualifier query."""
        escaped = quote(self.arn, safe="")
        return f"https://bedrock-agentcore.{self.aws_region}.amazonaws.com/runtimes/{escaped}/invocations"


class AgentBinding(DiagnosticValue):
    """An endpoint after matching server configuration and a registry snapshot.

    A binding with a ``runtime`` reaches the agent through AgentCore Runtime; its
    ``base_url`` is then the runtime's derived invocation URL.
    """

    manifest: PresentationManifest
    base_url: str
    runtime: AgentCoreRuntime | None = None


class McpSummary(DiagnosticValue):
    """Catalog metadata only; the UI never executes arbitrary MCP tools."""

    id: Identifier
    owner: str
    tool_names: tuple[str, ...]
